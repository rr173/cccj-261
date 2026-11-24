"""离线备份库：在同一个库目录里保留多个命名版本的 qc-columnar-archive v1 档案。

设计要点（查询/对比/建档行为不变，源档案只读）：

- **只收录 v1 档案的组成文件**：``manifest.json``、``structure.json`` 以及清单
  登记的全部页；源档案旁边的其他文件一概不碰。保存前复用现有清单/结构校验与
  compare 的逐页核验（SHA-256、JSON、页头、values 长度），校验不通过的档案
  不会成为版本。
- **库内载荷按内容寻址去重**：每个文件先流式写入库内暂存区并计算 SHA-256，
  再以 ``objects/<sha256[:2]>/<sha256>`` 发布。字节完全相同的文件只保留一份，
  与来源版本、相对路径无关；同一路径的不同内容各得一个对象，恢复时按所选
  版本的清单逐路径取回。
- **版本记录是唯一登记点**：``versions/v-<id>.json`` 记录 ``{版本名: {版本内
  相对路径: 对象摘要}}``，先写临时文件再 ``os.replace``。保存时对象全部发布
  且落盘后才写记录，因此中断后版本要么完整存在、要么完全不存在。
- **删除与回收**：``forget`` 只删版本记录；随后扫描 ``objects/``，删除既不被
  任何保留版本引用、也不被任何在途恢复租约引用的对象。空间可被后续备份复用。

并行与恢复快照：

- 一次恢复开始时，在短暂的库锁内选定版本记录、读取其清单（仅元数据），并为
  清单中每个对象写一个租约（``leases/lease-<id>/objects.json``）。之后立即释放锁
  并逐对象校验、复制。复制期间其他进程的保存/删除不必等待。
- 租约目录带 ``owner.json``（PID 与进程启动时刻）。回收时只清理**所属进程已
  退出**（或启动时刻不符）的租约；缺少 owner 标记的目录可能是另一进程正在
  创建，暂不回收。仍在运行的恢复其对象绝不会被回收。恢复正常结束时在
  ``finally`` 中删除自己的租约并回收；若进程被终止，下一次成功的 ``save``/
  ``forget`` 会发现 owner 已死并清理。宁可暂缓回收，也不误删在途恢复仍要读
  的对象。
- 恢复只读取对象内容并核对 ``sha256(bytes) == 对象摘要``；任一对象缺失、损坏
  或恢复出的档案未通过 v1 校验都整体失败。已写出的载荷全部删除，绝不留下半个
  档案，也绝不改动已有目标目录。

锁与崩溃一致性：

- 库级 ``vault.lock``（POSIX ``flock``，进程退出自动释放）只串行化元数据：
  版本记录的创建/删除、对象发布、垃圾回收扫描。复制源档案与恢复载荷都在锁外
  进行，因此其他进程不必等一次大复制完成。
- 保存的载荷先复制到库内暂存目录并在锁内发布，全部对象落盘后才写版本记录；
  中断只会留下无引用的对象或暂存文件（后续成功操作回收），不会出现“已登记却
  恢复不出来”的半套版本。暂存目录同样带 owner 标记，不会被并发进程误删。
"""

from __future__ import annotations

import errno
import fcntl
import hashlib
import json
import os
import re
import shutil
import uuid
from pathlib import Path
from typing import Any

from .errors import ArchiveError
from .manifest import ALL_COLUMNS, load_manifest, load_structure

VAULT_FORMAT = "qc-archive-vault"
VAULT_VERSION = 1

VAULT_FILE = "vault.json"
LOCK_FILE = "vault.lock"
VERSIONS_DIR = "versions"
OBJECTS_DIR = "objects"
LEASES_DIR = "leases"
TEMP_DIR = "tmp"
OWNER_FILE = "owner.json"
LEASE_OBJECTS_FILE = "objects.json"

VERSION_FILE_RE = re.compile(r"^v-[0-9a-f]{32}\.json$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
VERSION_NAME_MAX = 255

# 保存/恢复时逐块读写，不在内存中累积整份档案载荷。
_IO_CHUNK = 1 << 20


class VaultError(ValueError):
    """备份库操作失败（版本名冲突、档案校验不通过、IO 失败等）。

    命令行将它归类为操作失败（退出码 2）；仅参数形态错误（如空版本名）
    使用普通 ``ValueError``（退出码 3）。
    """


class VaultCorruptError(ArchiveError):
    """库内数据缺失/损坏，或版本记录不可解释，无法安全完成操作。"""


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------

def _normalize_name(name: Any) -> str:
    if not isinstance(name, str) or not name:
        raise ValueError("版本名必须是非空字符串")
    if "/" in name or "\x00" in name:
        raise ValueError("版本名不能包含 '/' 或 NUL 字符")
    if len(name.encode("utf-8")) > VERSION_NAME_MAX:
        raise ValueError(f"版本名过长（最多 {VERSION_NAME_MAX} 字节）")
    if name in (".", ".."):
        raise ValueError("版本名不能是 '.' 或 '..'")
    return name


def _safe_version_relpath(rel_path: Any) -> str:
    """版本内相对路径必须落在恢复目标内；返回规范化后的路径。"""
    if not isinstance(rel_path, str) or not rel_path:
        raise VaultCorruptError(f"版本清单包含非法相对路径: {rel_path!r}")
    if os.path.isabs(rel_path):
        raise VaultCorruptError(f"版本清单包含绝对路径: {rel_path!r}")
    normalized = os.path.normpath(rel_path)
    if normalized in (".", "..") or normalized.startswith(".." + os.sep) \
            or normalized.startswith("/"):
        raise VaultCorruptError(f"版本清单路径越出档案目录: {rel_path!r}")
    return normalized


def _sha256_stream(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(_IO_CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


def _copy_hashing(src: Path, dst: Path) -> str:
    """流式复制 src -> dst 并返回摘要。"""
    h = hashlib.sha256()
    with src.open("rb") as fin, dst.open("xb") as fout:
        for chunk in iter(lambda: fin.read(_IO_CHUNK), b""):
            h.update(chunk)
            fout.write(chunk)
        fout.flush()
        os.fsync(fout.fileno())
    return h.hexdigest()


def _fsync_directory(path: Path) -> None:
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


def _write_json_atomic(path: Path, payload: Any) -> None:
    tmp = path.parent / f".{path.name}.tmp-{uuid.uuid4().hex}"
    try:
        with tmp.open("x", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2, sort_keys=True)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
    _fsync_directory(path.parent)


def _object_relpath(digest: str) -> Path:
    return Path(OBJECTS_DIR) / digest[:2] / digest


def _tmp_name_re(name: str) -> bool:
    """``_write_json_atomic`` 的临时文件名：``.<目标名>.tmp-<hex>``。"""
    return (name.startswith(".") and ".tmp-" in name
            and not name.endswith(".json"))


def _remove_stale_json_tmp(directory: Path) -> None:
    """删除目录下遗留的原子写临时文件。

    只有在持有库锁时调用才安全：所有版本/库标识写入都在锁内完成，持锁时
    看到的临时文件一定来自已经退出的进程。
    """
    try:
        entries = list(directory.iterdir())
    except OSError:
        return
    for entry in entries:
        if entry.is_file() and _tmp_name_re(entry.name):
            try:
                entry.unlink()
            except OSError:
                pass


# ---------------------------------------------------------------------------
# 进程存活判断（用于租约/暂存目录归属）
# ---------------------------------------------------------------------------

def _process_start_ticks(pid: int) -> str | None:
    try:
        raw = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except OSError:
        return None
    # comm 字段可含空格/括号，取最后一个 ')' 之后的字段；
    # 其后第一个字段是 state(field 3)，starttime 是 field 22 -> 下标 19。
    end = raw.rfind(")")
    if end < 0:
        return None
    fields = raw[end + 2:].split()
    if len(fields) < 20:
        return None
    return fields[19]


def _owner_is_alive(owner_path: Path) -> bool:
    """owner.json 记录的进程是否仍然存活（PID 与启动时刻都匹配）。"""
    try:
        data = json.loads(owner_path.read_text(encoding="utf-8"))
        pid = int(data["pid"])
        start = data["start"]
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        return False
    if pid <= 0:
        return False
    actual_start = _process_start_ticks(pid)
    if actual_start is None:
        # 无法读取 /proc：退化为按 PID 是否存在判断（os.kill(pid, 0)）。
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError:
            return False
        return True
    return start == actual_start


def _owner_is_dead(dir_path: Path) -> bool:
    """只有明确带有 owner 标记且所属进程已退出时才视为可回收。

    缺少 owner 文件可能意味着另一进程正在创建该目录，宁可留下也不误删。
    """
    owner = dir_path / OWNER_FILE
    return owner.is_file() and not _owner_is_alive(owner)


def _write_owner(dir_path: Path) -> None:
    pid = os.getpid()
    _write_json_atomic(dir_path / OWNER_FILE, {
        "pid": pid,
        "start": _process_start_ticks(pid),
    })


def _make_owned_dir(parent: Path, prefix: str) -> Path:
    """创建带 owner 标记的目录：先写 owner 再原子改名，最终名不会缺标记。"""
    parent.mkdir(parents=True, exist_ok=True)
    tmp = parent / f"{prefix}new-{uuid.uuid4().hex}"
    tmp.mkdir()
    try:
        _write_owner(tmp)
        final = parent / f"{prefix}{uuid.uuid4().hex}"
        os.rename(tmp, final)
        return final
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise


def _prune_dead_dirs(parent: Path, prefix: str) -> None:
    """删除 parent 下名为 prefix*、且所属进程已退出的目录。"""
    try:
        entries = list(parent.iterdir())
    except OSError:
        return
    for entry in entries:
        if not entry.name.startswith(prefix) or not entry.is_dir():
            continue
        if _owner_is_dead(entry):
            shutil.rmtree(entry, ignore_errors=True)


# ---------------------------------------------------------------------------
# 源档案校验
# ---------------------------------------------------------------------------

def _archive_entries(source: Path) -> list[tuple[str, Path, str | None]]:
    """校验源 v1 档案，返回 [(版本内相对路径, 源文件路径, 页登记摘要), ...]。

    只包含 manifest.json、structure.json 与清单登记的全部页；非页文件的摘要为
    ``None``。校验不通过抛 ``VaultError``。
    """
    if not source.is_dir():
        raise VaultError(f"档案目录不存在或不是目录：{source}")
    try:
        structure = load_structure(source)
        columns = load_manifest(source, structure)
    except ArchiveError as exc:
        raise VaultError(f"档案校验不通过，不能保存：{exc}") from exc

    entries: list[tuple[str, Path, str | None]] = [
        ("manifest.json", source / "manifest.json", None),
        ("structure.json", source / "structure.json", None),
    ]
    seen: set[str] = {"manifest.json", "structure.json"}
    for name in ALL_COLUMNS:
        for ref in columns[name]:
            rel = _safe_version_relpath(ref.path)
            if rel in seen:
                raise VaultError(
                    f"档案清单中多个页登记了同一路径 {rel!r}，无法作为版本保存")
            seen.add(rel)
            entries.append((rel, source / rel, ref.sha256.lower()))

    # 用 compare 的逐页读取核验全部页（SHA-256/JSON/页头/values 长度）。
    _validate_archive_bytes(source)
    return entries


def _validate_archive_bytes(archive_dir: Path) -> None:
    """用现有读取器核验一份完整 v1 档案目录。"""
    from .compare import _Side

    try:
        side = _Side(archive_dir)
        for name in ALL_COLUMNS:
            for ref in side.columns[name]:
                side.load_page(name, ref)
                side.close_page()
    except ArchiveError as exc:
        raise VaultError(f"档案校验不通过：{exc}") from exc


# ---------------------------------------------------------------------------
# 备份库
# ---------------------------------------------------------------------------

class Vault:
    """一个备份库目录。所有公开操作以库根目录为参数。"""

    def __init__(self, root: str | os.PathLike[str]):
        self.root = Path(root)

    # -- 目录与记录 -----------------------------------------------------------

    def _marker_is_valid(self) -> bool:
        marker = self.root / VAULT_FILE
        try:
            data = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise VaultCorruptError(f"库标识文件损坏：{marker}: {exc}") from exc
        return (isinstance(data, dict) and data.get("format") == VAULT_FORMAT
                and data.get("version") == VAULT_VERSION)

    def _precheck_for_init(self) -> None:
        """首次保存前的快速检查：存在但不是库的目录直接拒绝。

        真正的“空目录/已初始化库”判断在库锁内由 ``_init_root_locked`` 完成，
        避免并发首次保存把对方刚建好的骨架误判为非库目录。这里只做无锁的
        粗筛，且把锁文件与库骨架目录视为“可能是并发初始化”。
        """
        if not self.root.exists():
            return
        if not self.root.is_dir():
            raise VaultError(f"库目录路径已存在但不是目录：{self.root}")
        marker = self.root / VAULT_FILE
        if marker.is_file():
            if not self._marker_is_valid():
                raise VaultCorruptError(f"目录不是受支持的备份库：{marker}")
            return
        allowed = {LOCK_FILE, VERSIONS_DIR, OBJECTS_DIR, LEASES_DIR, TEMP_DIR}
        try:
            leftovers = [e.name for e in self.root.iterdir()
                         if e.name not in allowed and not _tmp_name_re(e.name)]
        except OSError as exc:
            raise VaultError(f"无法读取库目录：{exc}") from exc
        if leftovers:
            raise VaultError(
                f"库目录非空且不是备份库，拒绝在其中创建：{self.root}")

    def _init_root_locked(self) -> None:
        """持锁后创建/补齐库骨架；已有版本与对象不受影响。

        首次保存的初始化完全在库锁内完成：并发进程谁先拿到锁谁创建骨架，
        其余进程拿到锁后直接复用，不会把对方的初始化误判为非备份库目录。
        """
        if self.root.exists() and not self.root.is_dir():
            raise VaultError(f"库目录路径已存在但不是目录：{self.root}")
        self.root.mkdir(parents=True, exist_ok=True)
        marker = self.root / VAULT_FILE
        if marker.exists():
            if not self._marker_is_valid():
                raise VaultCorruptError(
                    f"目录不是受支持的备份库：{marker}")
        else:
            leftovers = [e.name for e in self.root.iterdir()
                         if e.name != LOCK_FILE and not _tmp_name_re(e.name)]
            if leftovers:
                raise VaultError(
                    f"库目录非空且不是备份库，拒绝在其中创建：{self.root}")
            _remove_stale_json_tmp(self.root)
            _write_json_atomic(marker, {"format": VAULT_FORMAT,
                                        "version": VAULT_VERSION})
        for sub in (VERSIONS_DIR, OBJECTS_DIR, LEASES_DIR, TEMP_DIR):
            (self.root / sub).mkdir(exist_ok=True)

    def _check_existing_root(self) -> None:
        if not self.root.is_dir():
            raise VaultError(f"备份库不存在或不是目录：{self.root}")
        if not (self.root / VAULT_FILE).is_file():
            raise VaultError(f"目录不是备份库（缺少 {VAULT_FILE}）：{self.root}")
        if not self._marker_is_valid():
            raise VaultCorruptError(f"目录不是受支持的备份库：{self.root}")

    def _version_index(self) -> dict[str, Path]:
        """扫描版本记录，返回 {版本名: 记录路径}；重复名视为库损坏。"""
        index: dict[str, Path] = {}
        vdir = self.root / VERSIONS_DIR
        if not vdir.is_dir():
            return index
        for entry in sorted(vdir.iterdir()):
            if not entry.is_file() or not VERSION_FILE_RE.match(entry.name):
                continue
            record = self._read_version_record(entry)
            name = record["name"]
            if name in index:
                raise VaultCorruptError(
                    f"备份库中存在重复版本名 {name!r}：{index[name]} 与 {entry}")
            index[name] = entry
        return index

    def _read_version_record(self, path: Path) -> dict[str, Any]:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise VaultCorruptError(f"版本记录不可读或损坏：{path}: {exc}") from exc
        if not isinstance(data, dict) or data.get("format") != VAULT_FORMAT \
                or data.get("version") != VAULT_VERSION:
            raise VaultCorruptError(f"版本记录格式不受支持：{path}")
        name = data.get("name")
        if not isinstance(name, str) or not name:
            raise VaultCorruptError(f"版本记录缺少版本名：{path}")
        files = data.get("files")
        if not isinstance(files, dict) or not files:
            raise VaultCorruptError(f"版本记录缺少文件清单：{path}")
        for rel, digest in files.items():
            _safe_version_relpath(rel)
            if not isinstance(digest, str) or not SHA256_RE.match(digest):
                raise VaultCorruptError(
                    f"版本 {name!r} 的路径 {rel!r} 摘要非法：{digest!r}")
        return data

    # -- 锁 ------------------------------------------------------------------

    def _lock(self):
        return _VaultLock(self.root / LOCK_FILE)

    # -- 载荷对象 ------------------------------------------------------------

    def _publish_staged(self, staging: Path, digest: str) -> None:
        """把摘要为 digest 的暂存文件发布为对象；已有同内容对象则复用。"""
        dest = self.root / _object_relpath(digest)
        if dest.exists():
            if not dest.is_file():
                raise VaultError(f"载荷对象路径被非文件占用，无法保存：{dest}")
            try:
                existing = _sha256_stream(dest)
            except OSError:
                existing = None
            if existing == digest:
                try:
                    staging.unlink()
                except OSError:
                    pass
                return
            # 同名对象内容已损坏：用本次暂存内容原子替换。
            try:
                os.replace(staging, dest)
            except OSError as exc:
                raise VaultError(f"修复损坏载荷对象失败：{exc}") from exc
            return
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            if staging.stat().st_dev == dest.parent.stat().st_dev:
                os.link(staging, dest)
                staging.unlink()
            else:  # pragma: no cover - 同一库目录内通常同设备
                os.replace(staging, dest)
        except OSError as exc:
            if exc.errno == errno.EEXIST and dest.is_file() \
                    and _sha256_stream(dest) == digest:
                try:
                    staging.unlink()
                except OSError:
                    pass
                return
            raise VaultError(f"无法把载荷写入备份库：{exc}") from exc

    def _object_paths(self) -> list[Path]:
        obj_root = self.root / OBJECTS_DIR
        if not obj_root.is_dir():
            return []
        found: list[Path] = []
        for sub in obj_root.iterdir():
            if not sub.is_dir():
                continue
            for entry in sub.iterdir():
                if entry.is_file() and SHA256_RE.match(entry.name):
                    found.append(entry)
        return found

    # -- 租约与回收 -----------------------------------------------------------

    def _prune_dead_leases(self) -> None:
        """清理所属进程已退出的租约目录；活着的在途恢复一律保留。"""
        lease_root = self.root / LEASES_DIR
        if not lease_root.is_dir():
            return
        for token_dir in list(lease_root.iterdir()):
            if not token_dir.is_dir():
                continue
            if _owner_is_dead(token_dir):
                shutil.rmtree(token_dir, ignore_errors=True)

    def _prune_dead_tmp(self) -> None:
        tmp_root = self.root / TEMP_DIR
        if not tmp_root.is_dir():
            return
        for entry in list(tmp_root.iterdir()):
            if _owner_is_dead(entry):
                if entry.is_dir():
                    shutil.rmtree(entry, ignore_errors=True)
                else:
                    try:
                        entry.unlink()
                    except OSError:
                        pass

    def _referenced_objects(self, index: dict[str, Path] | None = None) -> set[str]:
        """所有保留版本引用的对象摘要 + 全部在途租约引用的摘要。"""
        if index is None:
            index = self._version_index()
        referenced: set[str] = set()
        for record_path in index.values():
            referenced.update(self._read_version_record(record_path)["files"].values())

        lease_root = self.root / LEASES_DIR
        if lease_root.is_dir():
            for lease_dir in lease_root.iterdir():
                if not lease_dir.is_dir():
                    continue
                try:
                    data = json.loads(
                        (lease_dir / LEASE_OBJECTS_FILE).read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    # 没有完整的租约清单（可能正在创建或被中断）：不据此回收。
                    # 创建租约在库锁内完成，因此这里不会看到“半份但即将生效”的
                    # 新租约；缺失只会出现在初始化失败/进程死亡之后。
                    continue
                digests = data.get("objects") if isinstance(data, dict) else None
                if not isinstance(digests, list):
                    continue
                referenced.update(d for d in digests
                                  if isinstance(d, str) and SHA256_RE.match(d))
        return referenced

    def _collect_garbage(self) -> int:
        """删除既无版本引用也无租约的对象，返回删除数量。

        版本记录不可解释时（库已损坏）跳过回收：宁可暂不释放空间，也不能
        把可能仍被某个版本引用的对象删掉。调用方的操作结果不受影响。
        """
        try:
            referenced = self._referenced_objects()
        except VaultCorruptError:
            return 0
        removed = 0
        for path in self._object_paths():
            if path.name in referenced:
                continue
            try:
                path.unlink()
                removed += 1
            except OSError:
                pass
        obj_root = self.root / OBJECTS_DIR
        if obj_root.is_dir():
            for sub in obj_root.iterdir():
                if sub.is_dir():
                    try:
                        sub.rmdir()
                    except OSError:
                        pass
        self._prune_dead_tmp()
        _remove_stale_json_tmp(self.root / VERSIONS_DIR)
        return removed

    # -- 保存 ----------------------------------------------------------------

    def save(self, version_name: str, archive_dir: str | os.PathLike[str]) -> Path:
        """把源 v1 档案保存为新版本，返回版本记录路径。"""
        name = _normalize_name(version_name)
        source = Path(archive_dir)
        entries = _archive_entries(source)  # 先校验，失败时不创建库

        self._precheck_for_init()
        with self._lock():
            self._init_root_locked()
            if name in self._version_index():
                raise VaultError(
                    f"版本名 {name!r} 已存在，不能覆盖；如需重存请先 forget")

        # 校验已通过：在锁外逐文件流式复制到库内暂存目录并算摘要。
        work = _make_owned_dir(self.root / TEMP_DIR, "save-")
        try:
            staged: list[tuple[Path, str]] = []
            files: dict[str, str] = {}
            try:
                for i, (rel, src_path, listed_digest) in enumerate(entries):
                    blob = work / f"{i:08d}.blob"
                    digest = _copy_hashing(src_path, blob)
                    # 源在保存期间视为不变；这里的比较只是防御明显的不一致。
                    if listed_digest is not None and listed_digest != digest:
                        raise VaultError(
                            f"源档案页 {rel!r} 在保存期间发生变化，已中止保存")
                    files[rel] = digest
                    staged.append((blob, digest))
            except OSError as exc:
                raise VaultError(f"读取源档案失败：{exc}") from exc

            # 锁内只做对象发布与版本记录，不复制载荷。
            with self._lock():
                self._init_root_locked()
                if name in self._version_index():
                    raise VaultError(
                        f"版本名 {name!r} 已存在，不能覆盖；如需重存请先 forget")
                for blob, digest in staged:
                    self._publish_staged(blob, digest)
                record = {
                    "format": VAULT_FORMAT,
                    "version": VAULT_VERSION,
                    "name": name,
                    # 键即版本内相对路径；值是该路径的原始字节摘要。
                    "files": files,
                }
                record_path = self.root / VERSIONS_DIR / f"v-{uuid.uuid4().hex}.json"
                _write_json_atomic(record_path, record)
                _fsync_directory(self.root / VERSIONS_DIR)

                # 成功保存后清理已退出进程留下的租约/暂存与无引用对象。
                self._prune_dead_leases()
                self._collect_garbage()
            return record_path
        finally:
            shutil.rmtree(work, ignore_errors=True)

    # -- 删除 ----------------------------------------------------------------

    def forget(self, version_name: str) -> None:
        """删除一个命名版本；不影响其他版本，并回收无引用的载荷。"""
        name = _normalize_name(version_name)
        self._check_existing_root()
        with self._lock():
            self._check_existing_root()
            index = self._version_index()
            if name not in index:
                raise VaultError(f"版本 {name!r} 不存在")
            try:
                index[name].unlink()
            except OSError as exc:
                raise VaultError(f"删除版本记录失败：{exc}") from exc
            _fsync_directory(self.root / VERSIONS_DIR)
            self._prune_dead_leases()
            self._collect_garbage()

    # -- 恢复 ----------------------------------------------------------------

    def restore(self, version_name: str, out_dir: str | os.PathLike[str]) -> Path:
        """把版本完整恢复到新档案目录；失败不留半个档案、不动已有目标。"""
        name = _normalize_name(version_name)
        out = Path(out_dir)
        if out.exists() or out.is_symlink():
            raise VaultError(f"恢复目标已存在，拒绝覆盖：{out}")
        self._check_existing_root()

        # 锁内：选定版本快照、确认对象存在、写租约。载荷一律不在锁内复制。
        lease_dir: Path | None = None
        with self._lock():
            self._check_existing_root()
            index = self._version_index()
            if name not in index:
                raise VaultError(f"版本 {name!r} 不存在")
            record = self._read_version_record(index[name])
            files: dict[str, str] = dict(record["files"])
            missing = [d for d in set(files.values())
                       if not (self.root / _object_relpath(d)).is_file()]
            if missing:
                raise VaultCorruptError(
                    f"版本 {name!r} 需要的载荷对象缺失：{sorted(missing)}")

            lease_dir = _make_owned_dir(self.root / LEASES_DIR, "lease-")
            try:
                _write_json_atomic(lease_dir / LEASE_OBJECTS_FILE,
                                   {"version": name,
                                    "objects": sorted(set(files.values()))})
            except BaseException:
                shutil.rmtree(lease_dir, ignore_errors=True)
                raise

        try:
            return self._restore_payloads(name, files, out)
        finally:
            shutil.rmtree(lease_dir, ignore_errors=True)
            with self._lock():
                self._prune_dead_leases()
                self._collect_garbage()

    def _restore_payloads(self, name: str, files: dict[str, str],
                          out: Path) -> Path:
        parent = out.parent
        try:
            parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise VaultError(f"无法创建恢复目标父目录：{exc}") from exc

        # 同一目标此前被终止的恢复暂存目录（所属进程已退出）可以安全清理。
        _prune_dead_dirs(parent, f".{out.name}.restore-")

        stage = _make_owned_dir(parent, f".{out.name}.restore-")
        try:
            for rel, digest in files.items():
                rel_path = _safe_version_relpath(rel)
                dest = stage / rel_path
                dest.parent.mkdir(parents=True, exist_ok=True)
                obj = self.root / _object_relpath(digest)
                try:
                    _copy_object_verified(obj, dest, digest)
                except FileNotFoundError as exc:
                    raise VaultCorruptError(
                        f"库内载荷缺失（版本 {name!r}，{rel!r}）：{obj}") from exc
                except OSError as exc:
                    raise VaultCorruptError(
                        f"读取库内载荷失败（版本 {name!r}，{rel!r}）：{exc}") from exc
                except _ObjectMismatch as exc:
                    raise VaultCorruptError(
                        f"库内载荷损坏（版本 {name!r}，{rel!r}）：{exc}") from exc

            try:
                _validate_archive_bytes(stage)
            except VaultError as exc:
                raise VaultCorruptError(
                    f"恢复出的档案未通过校验（版本 {name!r}）：{exc}") from exc

            if out.exists() or out.is_symlink():
                raise VaultError(f"恢复目标已存在，拒绝覆盖：{out}")
            # 恢复出的档案必须与源档案组成一致，不把租约/归属辅助文件带进去。
            try:
                (stage / OWNER_FILE).unlink()
            except FileNotFoundError:
                pass
            try:
                os.rename(stage, out)
            except OSError as exc:
                raise VaultError(f"无法提交恢复出的档案：{exc}") from exc
            _fsync_directory(parent)
            return out
        except BaseException:
            shutil.rmtree(stage, ignore_errors=True)
            raise


class _ObjectMismatch(Exception):
    pass


def _copy_object_verified(src: Path, dst: Path, expected: str) -> None:
    """流式复制对象并核对摘要；不匹配抛 _ObjectMismatch。"""
    h = hashlib.sha256()
    with src.open("rb") as fin, dst.open("xb") as fout:
        for chunk in iter(lambda: fin.read(_IO_CHUNK), b""):
            h.update(chunk)
            fout.write(chunk)
        fout.flush()
        os.fsync(fout.fileno())
    digest = h.hexdigest()
    if digest != expected:
        try:
            dst.unlink()
        except OSError:
            pass
        raise _ObjectMismatch(f"期望 {expected}，实际 {digest}")


class _VaultLock:
    """库级排他锁（POSIX flock），进程退出后由内核自动释放。"""

    def __init__(self, path: Path):
        self.path = path
        self._fh = None

    def __enter__(self) -> "_VaultLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        while True:
            self._fh = self.path.open("a+b")
            fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX)
            # 有人可能在此期间把库目录改名/删除并重建；确认锁文件仍是同一
            # inode，否则重新打开重锁，避免两个进程各锁一个旧 inode。
            try:
                same = os.stat(self.path).st_ino == os.fstat(self._fh.fileno()).st_ino
            except FileNotFoundError:
                same = False
            if same:
                return self
            self._fh.close()
            self._fh = None

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._fh is not None:
            try:
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
            finally:
                self._fh.close()
                self._fh = None


# ---------------------------------------------------------------------------
# 模块级入口（与 CLI 对应）
# ---------------------------------------------------------------------------

def save_version(vault_dir: str | os.PathLike[str], version_name: str,
                 archive_dir: str | os.PathLike[str]) -> Path:
    return Vault(vault_dir).save(version_name, archive_dir)


def restore_version(vault_dir: str | os.PathLike[str], version_name: str,
                    out_dir: str | os.PathLike[str]) -> Path:
    return Vault(vault_dir).restore(version_name, out_dir)


def forget_version(vault_dir: str | os.PathLike[str], version_name: str) -> None:
    Vault(vault_dir).forget(version_name)
