"""把现场导出的三份 UTF-8 JSONL 离线建档为 qc-columnar-archive v1。

设计要点：

- 逐行读取并校验源文件，不把三份 JSONL 或全部明细值读入内存；
- 标识、源行号和字节偏移放入临时 SQLite 计划库，因此 ID 集合也不随 Python
  对象总量常驻；
- 按“设备行序 → 部件行序 → 检查行序”稳定归组，生成符合 v1 连续区间要求的
  设备—部件—检查层级；
- 分页写出固定大小的 JSON 列页，当前页刷新后即释放；
- 所有产物先写到目标目录旁的临时暂存目录，完整校验通过后才提交。源数据校验
  或建档过程中失败时，已有 ``--out`` 档案完全不会被改动。
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import stat
import tempfile
import uuid
from pathlib import Path
from typing import Any, Iterator

from .compare import _Side
from .manifest import ALL_COLUMNS, COLUMN_KINDS, COLUMN_LEVELS, sha256_file
from .models import PageRef

EQUIPMENT_FILE = "equipment.jsonl"
COMPONENT_FILE = "component.jsonl"
INSPECTION_FILE = "inspection.jsonl"
REQUIRED_FILES = (EQUIPMENT_FILE, COMPONENT_FILE, INSPECTION_FILE)

DEFAULT_PAGE_SIZE = 1000


class ArchiveImportError(ValueError):
    """JSONL 输入、归属关系或档案提交失败。

    同时继承 ``ValueError``，方便调用方按参数/输入值错误捕获；命令行仍将其
    归类为建档失败（退出码 2），而不是普通参数错误（退出码 3）。
    """

    def __init__(self, message: str, *, source: str | Path | None = None,
                 line: int | None = None):
        self.source = Path(source).name if source is not None else None
        self.line = line
        if self.source is not None and line is not None:
            self.location = f"{self.source}:{line}"
            full = (
                f"{self.location}: {message}"
                f"（{self.source} 第{line}行 / line {line}）"
            )
        elif self.source is not None:
            self.location = self.source
            full = f"{self.source}: {message}"
        else:
            self.location = None
            full = message
        super().__init__(full)


def _reject_json_constant(name: str) -> Any:
    """NaN / Infinity 不是规范 JSON，必须按格式错误处理。"""
    raise ValueError(f"不允许的 JSON 常量 {name}")


def _is_nonempty_string(value: Any) -> bool:
    return isinstance(value, str) and value != ""


def _decode_jsonl_line(raw: bytes, *, source: str, line_no: int) -> dict[str, Any]:
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ArchiveImportError(
            f"不是合法 UTF-8 文本：{exc}", source=source, line=line_no) from exc

    if not text.strip():
        raise ArchiveImportError(
            "空行不是合法 JSON 对象（每行必须是一个 JSON 对象）",
            source=source, line=line_no)

    try:
        obj = json.loads(text, parse_constant=_reject_json_constant)
    except (json.JSONDecodeError, ValueError, RecursionError) as exc:
        raise ArchiveImportError(
            f"JSON 格式错误：{exc}", source=source, line=line_no) from exc

    if not isinstance(obj, dict):
        raise ArchiveImportError(
            "每行必须是 JSON 对象", source=source, line=line_no)
    return obj


def _iter_jsonl_objects(path: Path) -> Iterator[tuple[int, int, dict[str, Any]]]:
    """yield ``(行号, 字节偏移, 对象)``，逐行流式解析。"""
    try:
        fh = path.open("rb")
    except OSError as exc:
        raise ArchiveImportError(f"无法读取输入文件：{exc}", source=path.name) from exc

    offset = 0
    with fh:
        for line_no, raw in enumerate(fh, start=1):
            start = offset
            offset += len(raw)
            obj = _decode_jsonl_line(raw, source=path.name, line_no=line_no)
            yield line_no, start, obj


def _read_planned_record(path: Path, line_no: int,
                         byte_offset: int) -> dict[str, Any]:
    """按计划库中记录的字节偏移重读一行，避免缓存全部值。"""
    try:
        with path.open("rb") as fh:
            fh.seek(byte_offset)
            raw = fh.readline()
    except OSError as exc:
        raise ArchiveImportError(f"无法重读源记录：{exc}",
                                 source=path.name, line=line_no) from exc
    if not raw:
        raise ArchiveImportError("源记录在行偏移处缺失，输入文件可能已被改动",
                                 source=path.name, line=line_no)
    return _decode_jsonl_line(raw, source=path.name, line_no=line_no)


def _check_input_dir(input_dir: str | Path) -> Path:
    src = Path(input_dir)
    if not src.exists():
        raise ArchiveImportError(f"输入目录不存在：{src}")
    if not src.is_dir():
        raise ArchiveImportError(f"输入路径不是目录：{src}")

    missing = [name for name in REQUIRED_FILES if not (src / name).is_file()]
    if missing:
        raise ArchiveImportError(
            "输入目录必须包含 " + "、".join(REQUIRED_FILES)
            + f"；缺少或不是普通文件：{', '.join(missing)}；目录：{src}")
    return src


def _check_output_path(src: Path, out: Path) -> None:
    """避免输出提交操作反过来移动/删除正在读取的输入目录。"""
    src_resolved = src.resolve()
    out_resolved = out.resolve()
    if out_resolved == src_resolved:
        raise ValueError("--out 不能直接等于 JSONL 输入目录，请使用独立档案目录")
    try:
        src_resolved.relative_to(out_resolved)
    except ValueError:
        pass
    else:
        raise ValueError(
            "--out 不能是输入目录的上级目录；提交已有输出档案时可能影响输入文件")


def _connect_plan_db(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA temp_store=FILE")
    conn.execute("PRAGMA journal_mode=OFF")
    conn.execute("PRAGMA synchronous=OFF")
    conn.executescript(
        """
        CREATE TABLE equipment (
            line_no INTEGER PRIMARY KEY,
            equipment_id TEXT NOT NULL UNIQUE,
            byte_offset INTEGER NOT NULL
        );
        CREATE TABLE component (
            line_no INTEGER PRIMARY KEY,
            component_id TEXT NOT NULL UNIQUE,
            equipment_id TEXT NOT NULL,
            byte_offset INTEGER NOT NULL
        );
        CREATE TABLE inspection (
            line_no INTEGER PRIMARY KEY,
            inspection_id TEXT NOT NULL UNIQUE,
            component_id TEXT NOT NULL,
            byte_offset INTEGER NOT NULL
        );
        """
    )
    return conn


def _insert_unique(conn: sqlite3.Connection, table: str, id_column: str,
                   record_id: str, line_no: int, byte_offset: int,
                   extra_columns: dict[str, str] | None = None) -> None:
    columns = ["line_no", id_column, "byte_offset"]
    values: list[Any] = [line_no, record_id, byte_offset]
    if extra_columns:
        columns.extend(extra_columns)
        values.extend(extra_columns.values())
    placeholders = ", ".join("?" for _ in columns)
    try:
        conn.execute(
            f"INSERT INTO {table} ({', '.join(columns)}) "  # noqa: S608
            f"VALUES ({placeholders})",
            values,
        )
    except sqlite3.IntegrityError as exc:
        row = conn.execute(
            f"SELECT line_no FROM {table} WHERE {id_column} = ?",  # noqa: S608
            (record_id,),
        ).fetchone()
        first_line = row["line_no"] if row is not None else "?"
        source = {
            "equipment": EQUIPMENT_FILE,
            "component": COMPONENT_FILE,
            "inspection": INSPECTION_FILE,
        }[table]
        raise ArchiveImportError(
            f"{id_column} {json.dumps(record_id, ensure_ascii=False)} 重复；"
            f"当前记录与 {source} 第 {first_line} 行中的标识相同",
            source=source, line=line_no,
        ) from exc


def _parse_sources(src: Path, conn: sqlite3.Connection) -> None:
    # 设备
    for line_no, offset, obj in _iter_jsonl_objects(src / EQUIPMENT_FILE):
        equipment_id = obj.get("equipment_id")
        if not _is_nonempty_string(equipment_id):
            raise ArchiveImportError(
                "equipment_id 必须是非空字符串",
                source=EQUIPMENT_FILE, line=line_no)
        _insert_unique(conn, "equipment", "equipment_id",
                       equipment_id, line_no, offset)

    # 部件
    for line_no, offset, obj in _iter_jsonl_objects(src / COMPONENT_FILE):
        component_id = obj.get("component_id")
        if not _is_nonempty_string(component_id):
            raise ArchiveImportError(
                "component_id 必须是非空字符串",
                source=COMPONENT_FILE, line=line_no)

        equipment_id = obj.get("equipment_id")
        if not _is_nonempty_string(equipment_id):
            raise ArchiveImportError(
                "equipment_id 必须是指向已有设备的非空字符串，所属关系无法解析",
                source=COMPONENT_FILE, line=line_no)

        if "remark" not in obj:
            raise ArchiveImportError(
                "缺少 remark 字段；没有备注时必须显式给出 \"remark\": null",
                source=COMPONENT_FILE, line=line_no)

        _insert_unique(
            conn, "component", "component_id", component_id, line_no, offset,
            extra_columns={"equipment_id": equipment_id})

    # 检查
    for line_no, offset, obj in _iter_jsonl_objects(src / INSPECTION_FILE):
        inspection_id = obj.get("inspection_id")
        if not _is_nonempty_string(inspection_id):
            raise ArchiveImportError(
                "inspection_id 必须是非空字符串",
                source=INSPECTION_FILE, line=line_no)

        component_id = obj.get("component_id")
        if not _is_nonempty_string(component_id):
            raise ArchiveImportError(
                "component_id 必须是指向已有部件的非空字符串，所属关系无法解析",
                source=INSPECTION_FILE, line=line_no)

        for key in ("category", "result", "detail"):
            if key not in obj:
                raise ArchiveImportError(
                    f"缺少 {key} 字段；空值必须显式写成 null",
                    source=INSPECTION_FILE, line=line_no)

        _insert_unique(
            conn, "inspection", "inspection_id", inspection_id, line_no, offset,
            extra_columns={"component_id": component_id})

    conn.commit()


def _validate_references(conn: sqlite3.Connection) -> None:
    row = conn.execute(
        """
        SELECT c.line_no AS line_no, c.component_id AS component_id,
               c.equipment_id AS owner_id
        FROM component c
        LEFT JOIN equipment e ON e.equipment_id = c.equipment_id
        WHERE e.equipment_id IS NULL
        ORDER BY c.line_no
        LIMIT 1
        """
    ).fetchone()
    if row is not None:
        raise ArchiveImportError(
            f"component_id {json.dumps(row['component_id'], ensure_ascii=False)} "
            f"所属 equipment_id {json.dumps(row['owner_id'], ensure_ascii=False)} "
            "在 equipment.jsonl 中不存在，无法解析所属关系",
            source=COMPONENT_FILE, line=row["line_no"])

    row = conn.execute(
        """
        SELECT i.line_no AS line_no, i.inspection_id AS inspection_id,
               i.component_id AS owner_id
        FROM inspection i
        LEFT JOIN component c ON c.component_id = i.component_id
        WHERE c.component_id IS NULL
        ORDER BY i.line_no
        LIMIT 1
        """
    ).fetchone()
    if row is not None:
        raise ArchiveImportError(
            f"inspection_id {json.dumps(row['inspection_id'], ensure_ascii=False)} "
            f"所属 component_id {json.dumps(row['owner_id'], ensure_ascii=False)} "
            "在 component.jsonl 中不存在，无法解析所属关系",
            source=INSPECTION_FILE, line=row["line_no"])


def _create_plans(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE component_plan (
            component_slot INTEGER PRIMARY KEY,
            component_id TEXT NOT NULL UNIQUE,
            component_line INTEGER NOT NULL,
            byte_offset INTEGER NOT NULL,
            equipment_slot INTEGER NOT NULL
        );

        INSERT INTO component_plan
        SELECT
            ROW_NUMBER() OVER (ORDER BY e.line_no, c.line_no) - 1,
            c.component_id,
            c.line_no,
            c.byte_offset,
            e.line_no - 1
        FROM component c
        JOIN equipment e ON e.equipment_id = c.equipment_id;

        CREATE TABLE inspection_plan (
            inspection_slot INTEGER PRIMARY KEY,
            inspection_id TEXT NOT NULL UNIQUE,
            inspection_line INTEGER NOT NULL,
            byte_offset INTEGER NOT NULL,
            component_slot INTEGER NOT NULL,
            equipment_slot INTEGER NOT NULL
        );

        INSERT INTO inspection_plan
        SELECT
            ROW_NUMBER() OVER (
                ORDER BY cp.component_slot, i.line_no
            ) - 1,
            i.inspection_id,
            i.line_no,
            i.byte_offset,
            cp.component_slot,
            cp.equipment_slot
        FROM inspection i
        JOIN component_plan cp ON cp.component_id = i.component_id;
        """
    )
    conn.commit()


class _ColumnPageWriter:
    """收集固定数量的槽值并流式写出一页，刷新后释放本页值。"""

    def __init__(self, stage_dir: Path, column: str, page_size: int):
        if not isinstance(page_size, int) or isinstance(page_size, bool) \
                or page_size <= 0:
            raise ValueError("page_size 必须是正整数")
        self.stage_dir = stage_dir
        self.column = column
        self.page_size = page_size
        self.column_dir = stage_dir / "pages" / column
        self.column_dir.mkdir(parents=True, exist_ok=True)
        self.refs: list[PageRef] = []
        self._values: list[Any] = []
        self._slot_start = 0
        self._next_slot = 0
        self._page_index = 0

    def add(self, value: Any) -> None:
        if not self._values:
            self._slot_start = self._next_slot
        self._values.append(value)
        self._next_slot += 1
        if len(self._values) >= self.page_size:
            self.flush()

    def flush(self) -> None:
        if not self._values:
            return
        slot_end = self._slot_start + len(self._values)
        page_id = f"p{self._page_index}"
        rel_path = f"pages/{self.column}/{page_id}.json"
        path = self.stage_dir / rel_path
        payload = {
            "page_id": page_id,
            "column": self.column,
            "slot_start": self._slot_start,
            "slot_end": slot_end,
            "values": self._values,
        }
        with path.open("w", encoding="utf-8", newline="\n") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2,
                      allow_nan=False)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())

        digest, _size = sha256_file(path)
        self.refs.append(PageRef(
            page_id=page_id, path=rel_path,
            slot_start=self._slot_start, slot_end=slot_end,
            sha256=digest))

        self._values.clear()
        self._page_index += 1
        self._slot_start = self._next_slot

    def close(self) -> list[PageRef]:
        self.flush()
        return self.refs


def _write_json(path: Path, payload: Any) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2, allow_nan=False)
        fh.write("\n")
        fh.flush()
        os.fsync(fh.fileno())


def _build_ranges(conn: sqlite3.Connection) -> tuple[list[list[int]], list[list[int]]]:
    component_ranges: list[list[int]] = []
    cursor = 0
    rows = conn.execute(
        """
        SELECT e.line_no - 1 AS equipment_slot,
               COUNT(cp.component_slot) AS component_count
        FROM equipment e
        LEFT JOIN component_plan cp ON cp.equipment_slot = e.line_no - 1
        GROUP BY e.line_no
        ORDER BY e.line_no
        """
    )
    for expected_slot, row in enumerate(rows):
        if row["equipment_slot"] != expected_slot:  # pragma: no cover - 防御
            raise ArchiveImportError("设备槽计划不连续，无法生成 structure.json")
        count = int(row["component_count"])
        component_ranges.append([cursor, cursor + count])
        cursor += count

    inspection_ranges: list[list[int]] = []
    cursor = 0
    rows = conn.execute(
        """
        SELECT cp.component_slot AS component_slot,
               COUNT(ip.inspection_slot) AS inspection_count
        FROM component_plan cp
        LEFT JOIN inspection_plan ip ON ip.component_slot = cp.component_slot
        GROUP BY cp.component_slot
        ORDER BY cp.component_slot
        """
    )
    for expected_slot, row in enumerate(rows):
        if row["component_slot"] != expected_slot:  # pragma: no cover - 防御
            raise ArchiveImportError("部件槽计划不连续，无法生成 structure.json")
        count = int(row["inspection_count"])
        inspection_ranges.append([cursor, cursor + count])
        cursor += count

    return component_ranges, inspection_ranges


def _write_archive(src: Path, stage: Path, conn: sqlite3.Connection,
                   page_size: int) -> tuple[int, int, int]:
    component_ranges, inspection_ranges = _build_ranges(conn)

    equipment_count = conn.execute(
        "SELECT COUNT(*) AS n FROM equipment").fetchone()["n"]
    component_count = conn.execute(
        "SELECT COUNT(*) AS n FROM component_plan").fetchone()["n"]
    inspection_count = conn.execute(
        "SELECT COUNT(*) AS n FROM inspection_plan").fetchone()["n"]

    # 部件备注：输出槽按 设备行序 + 部件行序 归组。
    remark_writer = _ColumnPageWriter(stage, "component_remark", page_size)
    component_path = src / COMPONENT_FILE
    for row in conn.execute(
            """
            SELECT component_line AS line_no, byte_offset
            FROM component_plan
            ORDER BY component_slot
            """):
        obj = _read_planned_record(
            component_path, row["line_no"], row["byte_offset"])
        remark_writer.add(obj["remark"])
    remark_refs = remark_writer.close()

    # 三项检查列：同一次检查在同一次源行读取中写入，天然同槽对齐。
    writers = {
        name: _ColumnPageWriter(stage, name, page_size)
        for name in (
            "inspection_category",
            "inspection_result",
            "inspection_detail",
        )
    }
    inspection_path = src / INSPECTION_FILE
    for row in conn.execute(
            """
            SELECT inspection_line AS line_no, byte_offset
            FROM inspection_plan
            ORDER BY inspection_slot
            """):
        obj = _read_planned_record(
            inspection_path, row["line_no"], row["byte_offset"])
        writers["inspection_category"].add(obj["category"])
        writers["inspection_result"].add(obj["result"])
        writers["inspection_detail"].add(obj["detail"])

    column_refs = {name: writer.close() for name, writer in writers.items()}
    column_refs["component_remark"] = remark_refs

    structure = {
        "component_ranges": component_ranges,
        "inspection_ranges": inspection_ranges,
    }
    _write_json(stage / "structure.json", structure)

    columns_manifest: dict[str, Any] = {}
    for name in ALL_COLUMNS:
        columns_manifest[name] = {
            "level": COLUMN_LEVELS[name],
            "kind": COLUMN_KINDS[name],
            "nullable": True,
            "pages": [ref.to_dict() for ref in column_refs[name]],
        }

    manifest = {
        "format": "qc-columnar-archive",
        "version": 1,
        "logical_slot_counts": {
            "equipment": int(equipment_count),
            "component": int(component_count),
            "inspection": int(inspection_count),
        },
        "columns": columns_manifest,
    }
    _write_json(stage / "manifest.json", manifest)

    return int(equipment_count), int(component_count), int(inspection_count)


def _verify_staged_archive(stage: Path) -> None:
    """用现有 compare 读取器核验结构、清单和全部四列页。

    逐页加载会执行 SHA-256、UTF-8/JSON、页头和 values 长度校验；一次只保留
    一页，避免建档自检本身随档案总量增长。查询/对比引擎的行为由现有测试保证。
    """
    try:
        side = _Side(stage)
        for column in ALL_COLUMNS:
            for ref in side.columns[column]:
                page = side.load_page(column, ref)
                # _Side.load_page 已校验长度；显式访问一次槽数组以尽早暴露异常。
                if len(page.values) != ref.slot_end - ref.slot_start:  # pragma: no cover
                    raise ArchiveImportError(
                        f"页 {ref.page_id} 的 values 长度与清单不一致")
                side.close_page()
                del page
    except ArchiveImportError:
        raise
    except Exception as exc:
        raise ArchiveImportError(
            f"生成后的档案未通过 qc-columnar-archive v1 校验：{exc}") from exc


def _fsync_directory(path: Path) -> None:
    if not path.is_dir():
        return
    try:
        fd = os.open(str(path), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _retry_remove_readonly(func: Any, path: str,
                           exc_info: tuple[type[BaseException], BaseException, Any]) -> None:
    """shutil.rmtree 回调：给只读文件/目录补写权限后重试一次。"""
    try:
        os.chmod(path, stat.S_IRWXU)
        func(path)
    except OSError:
        parent = os.path.dirname(path)
        if parent:
            os.chmod(parent, stat.S_IRWXU)
        func(path)


def _commit_stage(stage: Path, out: Path, *, replace_existing: bool) -> None:
    """把完整暂存目录原子提交到 out；已存在目录先改名备份再切换。"""
    if out.exists() or out.is_symlink():
        if not replace_existing:
            raise ArchiveImportError(f"目标档案目录已存在，拒绝覆盖：{out}")
        if out.is_symlink() and not out.is_dir():
            raise ArchiveImportError(
                f"目标路径是符号链接但不指向目录，拒绝替换：{out}")
        if not out.is_dir():
            raise ArchiveImportError(
                f"目标路径已存在且不是档案目录，拒绝替换：{out}")

        backup = out.parent / f".{out.name}.old-{os.getpid()}-{uuid.uuid4().hex}"
        old_moved = False
        new_committed = False
        try:
            os.rename(out, backup)
            old_moved = True
            os.rename(stage, out)
            new_committed = True
        except OSError as exc:
            if old_moved and not new_committed:
                try:
                    os.rename(backup, out)
                except OSError as restore_exc:
                    raise ArchiveImportError(
                        f"提交新档案失败且无法恢复旧档案：{exc}; "
                        f"恢复失败：{restore_exc}; 旧档案备份位于：{backup}"
                    ) from exc
            raise ArchiveImportError(
                f"提交档案失败，已有目标保持原状：{exc}") from exc
        except BaseException:
            # 收到中断等异常时，只要新目录尚未就位就尽量恢复旧档案。
            if old_moved and not new_committed:
                try:
                    os.rename(backup, out)
                except OSError:
                    pass
            raise

        try:
            if backup.is_symlink() or backup.is_file():
                backup.unlink()
            else:
                shutil.rmtree(backup, onerror=_retry_remove_readonly)
        except OSError:
            # 新档案已经成功就位。旧档案备份清理失败不应把成功建档误报为失败；
            # 备份是完整旧档案，不会产生半个新档案。
            pass
        _fsync_directory(out.parent)
        return

    try:
        os.rename(stage, out)
    except OSError as exc:
        raise ArchiveImportError(f"无法提交档案目录到 {out}：{exc}") from exc
    _fsync_directory(out.parent)


def import_jsonl_archive(input_dir: str | Path,
                         out_dir: str | Path,
                         *,
                         page_size: int = DEFAULT_PAGE_SIZE,
                         replace_existing: bool = True) -> Path:
    """导入三份 JSONL，并返回生成的档案目录路径。"""
    if not isinstance(page_size, int) or isinstance(page_size, bool) \
            or page_size <= 0:
        raise ValueError("page_size 必须是正整数")

    src = _check_input_dir(input_dir)
    out = Path(out_dir)
    _check_output_path(src, out)

    out.parent.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(
        prefix=f".{out.name or 'archive'}.import-work-", dir=out.parent))
    conn: sqlite3.Connection | None = None
    try:
        db_path = work / "plan.sqlite3"
        conn = _connect_plan_db(db_path)
        _parse_sources(src, conn)
        _validate_references(conn)
        _create_plans(conn)

        stage = work / "stage"
        stage.mkdir()
        _write_archive(src, stage, conn, page_size)

        conn.close()
        conn = None

        _verify_staged_archive(stage)
        _commit_stage(stage, out, replace_existing=replace_existing)
        return out
    finally:
        try:
            if conn is not None:
                conn.close()
        finally:
            shutil.rmtree(work, ignore_errors=True)


# 语义化别名，方便不同调用方/测试发现此入口。
import_archive = import_jsonl_archive
import_directory = import_jsonl_archive
import_jsonl_directory = import_jsonl_archive
build_archive_from_jsonl = import_jsonl_archive
build_archive_from_jsonl_directory = import_jsonl_archive
