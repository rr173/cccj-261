"""两份 ``qc-columnar-archive v1`` 快照的离线结构/载荷对比。

对比语义（与查询入口相互独立，查询行为不变）：

- 两份档案的设备、部件、检查结构边界必须完全一致；结构不同直接报档案错误，
  不做任何槽位对齐，也不产生/写出差异。
- 槽身份由逻辑槽号决定；两份档案各列的分页边界可以不同，按槽逐一对齐。
- ``null`` 是实际值：``null == null`` 视为未变化，``null`` 与任何非空值不同。
- 检查的类别/结果/明细任一变化，报告该检查坐标及三项的前后值；部件备注变化
  单独报告部件坐标及备注前后值，不重复报告该部件下未变化的检查。
- 输出按设备、部件槽顺序；同一部件先备注变化、后检查变化；检查按原槽位顺序。
- 四列的每一页都要读取并核验（SHA-256、JSON、页头、values 长度）。任一页失败
  即整体失败，不交付部分差异。
- 每列使用一个流式游标，任一时刻最多驻留每档案每列一页；差异边生成边写出，
  内存不随全部检查明细增长。
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import tempfile
import textwrap
from pathlib import Path
from typing import Any, Iterator

from .errors import ArchiveError
from .manifest import ALL_COLUMNS, load_manifest, load_structure
from .models import PageRef, Structure

COMPONENT_COLUMN = "component_remark"
INSPECTION_VALUE_COLUMNS = (
    "inspection_category",
    "inspection_result",
    "inspection_detail",
)
DIFF_FORMAT = "qc-archive-diff"
DIFF_VERSION = 1


# ---------------------------------------------------------------------------
# JSON 值比较
# ---------------------------------------------------------------------------

def json_values_equal(left: Any, right: Any) -> bool:
    """按 JSON 实际值比较，避免 Python 的 ``True == 1`` 等跨类型相等。"""
    if left is None or right is None:
        return left is None and right is None
    if isinstance(left, bool) or isinstance(right, bool):
        return (isinstance(left, bool) and isinstance(right, bool)
                and left == right)
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        return left == right
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        if set(left) != set(right):
            return False
        return all(json_values_equal(left[k], right[k]) for k in left)
    if isinstance(left, list):
        return (len(left) == len(right)
                and all(json_values_equal(a, b) for a, b in zip(left, right)))
    return left == right


# ---------------------------------------------------------------------------
# 页读取：每列一个单调游标，最多驻留一页
# ---------------------------------------------------------------------------

class _Page:
    __slots__ = ("ref", "values")

    def __init__(self, ref: PageRef, values: list[Any]):
        self.ref = ref
        self.values = values

    def get(self, slot: int) -> Any:
        return self.values[slot - self.ref.slot_start]


class _Side:
    """一份档案的已校验清单 + 按列懒加载页。"""

    def __init__(self, directory: str | os.PathLike[str]):
        self.directory = Path(directory)
        self.structure = load_structure(self.directory)
        self.columns = load_manifest(self.directory, self.structure)
        self.page_counts: dict[str, int] = {name: 0 for name in ALL_COLUMNS}
        self.live_pages = 0
        self.max_live_pages = 0

    # -- 页加载与完整性核验 ---------------------------------------------------

    def load_page(self, column: str, ref: PageRef) -> _Page:
        path = self.directory / ref.path
        try:
            raw = path.read_bytes()
        except OSError as exc:
            raise ArchiveError(
                f"载荷页不可读：档案 {self.directory}，列 {column}，"
                f"页 {ref.page_id}（{ref.path}）：{exc}") from exc

        digest = hashlib.sha256(raw).hexdigest()
        if digest.lower() != ref.sha256.lower():
            raise ArchiveError(
                f"载荷页校验和不匹配：档案 {self.directory}，列 {column}，"
                f"页 {ref.page_id}（{ref.path}）：期望 {ref.sha256}，实际 {digest}")

        try:
            data = json.loads(raw.decode("utf-8"),
                              parse_constant=self._reject_constant)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError,
                RecursionError) as exc:
            raise ArchiveError(
                f"载荷页不是合法 JSON：档案 {self.directory}，列 {column}，"
                f"页 {ref.page_id}（{ref.path}）：{exc}") from exc

        values = self._validate_payload(column, ref, data)
        self.page_counts[column] += 1
        self.live_pages += 1
        self.max_live_pages = max(self.max_live_pages, self.live_pages)
        return _Page(ref, values)

    @staticmethod
    def _reject_constant(name: str) -> Any:
        """NaN / Infinity 不是合法 JSON 值，按页不可解释处理。"""
        raise ValueError(f"不允许的 JSON 常量 {name}")

    def _validate_payload(self, column: str, ref: PageRef,
                          data: Any) -> list[Any]:
        def fail(message: str) -> None:
            raise ArchiveError(
                f"载荷页结构非法：档案 {self.directory}，列 {column}，"
                f"页 {ref.page_id}（{ref.path}）：{message}")

        if not isinstance(data, dict):
            fail("页顶层不是对象")
        if data.get("page_id") != ref.page_id:
            fail(f"页头 page_id={data.get('page_id')!r} 与清单登记 {ref.page_id!r} 不一致")
        if data.get("column") != column:
            fail(f"页头 column={data.get('column')!r} 与清单登记 {column!r} 不一致")
        for key, expected in (("slot_start", ref.slot_start),
                              ("slot_end", ref.slot_end)):
            actual = data.get(key)
            if (not isinstance(actual, int) or isinstance(actual, bool)
                    or actual != expected):
                fail(f"页头 {key}={actual!r} 与清单登记 {expected!r} 不一致")
        values = data.get("values")
        if not isinstance(values, list):
            fail("values 必须是数组")
        expected_len = ref.slot_end - ref.slot_start
        if len(values) != expected_len:
            fail(f"values 长度 {len(values)} 与槽区间长度 {expected_len} 不一致")
        return values

    def close_page(self) -> None:
        self.live_pages -= 1


class _ColumnCursor:
    """按绝对槽单调读取一列；翻页时立即释放上一页。"""

    def __init__(self, side: _Side, column: str):
        self.side = side
        self.column = column
        self.refs = side.columns[column]
        self.index = 0
        self.page: _Page | None = None

    def get(self, slot: int) -> Any:
        page = self.page
        if page is not None and page.ref.covers(slot):
            return page.get(slot)

        if page is not None:
            if slot < page.ref.slot_start:
                raise ArchiveError(
                    f"列 {self.column} 的槽访问顺序非法：槽 {slot} 早于当前页 "
                    f"[{page.ref.slot_start},{page.ref.slot_end})")
            self._close_page()

        while True:
            if self.index >= len(self.refs):
                raise ArchiveError(
                    f"列 {self.column} 的分页无法覆盖槽 {slot}（分页提前耗尽）")
            ref = self.refs[self.index]
            if slot < ref.slot_start:
                raise ArchiveError(
                    f"列 {self.column} 的分页在槽 {slot} 处存在缺口")
            if slot < ref.slot_end:
                self.page = self.side.load_page(self.column, ref)
                return self.page.get(slot)
            self.index += 1

    def _close_page(self) -> None:
        if self.page is not None:
            self.page = None
            self.side.close_page()

    def close(self) -> None:
        self._close_page()


# ---------------------------------------------------------------------------
# 对比会话
# ---------------------------------------------------------------------------

class Comparison:
    """一次槽位对齐对比；差异以生成器形式产出，不缓存全部明细。"""

    def __init__(self, before: str | os.PathLike[str],
                 after: str | os.PathLike[str]):
        self.before = _Side(before)
        self.after = _Side(after)
        self._check_structures()
        self.structure: Structure = self.before.structure
        self._before_cursors = {
            name: _ColumnCursor(self.before, name) for name in ALL_COLUMNS}
        self._after_cursors = {
            name: _ColumnCursor(self.after, name) for name in ALL_COLUMNS}
        self.component_remark_changes = 0
        self.inspection_changes = 0
        self._started = False
        self._closed = False

    # -- 结构一致性 -----------------------------------------------------------

    def _check_structures(self) -> None:
        for side in (self.before, self.after):
            self._check_structure_sane(side)
        before = self.before.structure
        after = self.after.structure
        if (before.component_ranges == after.component_ranges
                and before.inspection_ranges == after.inspection_ranges):
            return

        if before.component_ranges != after.component_ranges:
            if len(before.component_ranges) != len(after.component_ranges):
                detail = (f"设备数不同：前 {len(before.component_ranges)}，"
                          f"后 {len(after.component_ranges)}")
            else:
                for i, (a, b) in enumerate(zip(before.component_ranges,
                                               after.component_ranges)):
                    if a != b:
                        detail = (f"component_ranges[{i}] 前 {list(a)}，"
                                  f"后 {list(b)}")
                        break
        else:
            for i, (a, b) in enumerate(zip(before.inspection_ranges,
                                           after.inspection_ranges)):
                if a != b:
                    detail = (f"inspection_ranges[{i}] 前 {list(a)}，"
                              f"后 {list(b)}")
                    break
            else:  # pragma: no cover - 理论上已被上方相等判断排除
                detail = "检查/部件区间长度不同"
        raise ArchiveError(
            f"两份档案结构边界不一致，无法按逻辑槽对齐对比：{detail}。"
            "设备、部件、检查的结构边界必须完全一致。")

    @staticmethod
    def _check_structure_sane(side: _Side) -> None:
        """单份档案的两层区间必须互相覆盖（连续性已由 load_structure 校验）。"""
        structure = side.structure
        component_slots = (structure.component_ranges[-1][1]
                           if structure.component_ranges else 0)
        if component_slots != structure.component_count:
            raise ArchiveError(
                f"档案 {side.directory} 结构不自洽：设备覆盖 {component_slots} 个"
                f"部件槽，但 inspection_ranges 只登记了 "
                f"{structure.component_count} 个部件")

    # -- 差异生成 -------------------------------------------------------------

    def differences(self) -> Iterator[dict[str, Any]]:
        """按设备→部件→检查顺序产出差异；生成器结束即完成全量校验。"""
        if self._started:
            raise RuntimeError("同一次 Comparison 的 differences() 只能迭代一次")
        self._started = True

        structure = self.structure
        comp_ranges = structure.component_ranges
        equip_slot = 0
        equip_start, equip_end = (comp_ranges[0] if comp_ranges else (0, 0))

        for comp_slot in range(structure.component_count):
            while comp_slot >= equip_end:
                equip_slot += 1
                if equip_slot >= len(comp_ranges):  # pragma: no cover
                    raise ArchiveError("部件槽超出设备结构边界")
                equip_start, equip_end = comp_ranges[equip_slot]

            component_coord = {
                "equipment_slot": equip_slot,
                "equipment_ordinal": equip_slot + 1,
                "component_slot": comp_slot,
                "component_ordinal_in_equipment": comp_slot - equip_start + 1,
            }

            remark_before = self._before_cursors[COMPONENT_COLUMN].get(comp_slot)
            remark_after = self._after_cursors[COMPONENT_COLUMN].get(comp_slot)
            if not json_values_equal(remark_before, remark_after):
                self.component_remark_changes += 1
                yield {
                    "type": "component_remark",
                    **component_coord,
                    "remark_before": remark_before,
                    "remark_after": remark_after,
                }

            insp_start, insp_end = structure.inspection_ranges[comp_slot]
            for insp_slot in range(insp_start, insp_end):
                before_values = [
                    self._before_cursors[name].get(insp_slot)
                    for name in INSPECTION_VALUE_COLUMNS]
                after_values = [
                    self._after_cursors[name].get(insp_slot)
                    for name in INSPECTION_VALUE_COLUMNS]
                changed_fields = [
                    name for name, a, b in zip(INSPECTION_VALUE_COLUMNS,
                                               before_values, after_values)
                    if not json_values_equal(a, b)]
                if not changed_fields:
                    continue

                self.inspection_changes += 1
                yield {
                    "type": "inspection",
                    **component_coord,
                    "inspection_slot": insp_slot,
                    "inspection_ordinal_in_component":
                        insp_slot - insp_start + 1,
                    "category_before": before_values[0],
                    "category_after": after_values[0],
                    "result_before": before_values[1],
                    "result_after": after_values[1],
                    "detail_before": before_values[2],
                    "detail_after": after_values[2],
                    "changed_fields": changed_fields,
                }

    # -- 汇总/清理 ------------------------------------------------------------

    @property
    def total_changes(self) -> int:
        return self.component_remark_changes + self.inspection_changes

    def summary(self) -> dict[str, Any]:
        return {
            "component_remark_changes": self.component_remark_changes,
            "inspection_changes": self.inspection_changes,
            "total_changes": self.total_changes,
            "pages_verified": {
                "before": dict(self.before.page_counts),
                "after": dict(self.after.page_counts),
            },
        }

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for cursor in list(self._before_cursors.values()) + list(
                self._after_cursors.values()):
            cursor.close()

    def __enter__(self) -> "Comparison":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()


# ---------------------------------------------------------------------------
# 报告写出
# ---------------------------------------------------------------------------

def _write_stream(stream, comparison: Comparison,
                  before_label: str, after_label: str) -> None:
    stream.write("{\n")
    stream.write(f'  "format": {json.dumps(DIFF_FORMAT)},\n')
    stream.write(f'  "version": {DIFF_VERSION},\n')
    stream.write(f'  "before_archive": '
                 f'{json.dumps(before_label, ensure_ascii=False)},\n')
    stream.write(f'  "after_archive": '
                 f'{json.dumps(after_label, ensure_ascii=False)},\n')
    stream.write('  "structure_identical": true,\n')
    stream.write('  "differences": [')

    first = True
    for difference in comparison.differences():
        if first:
            stream.write("\n")
            first = False
        else:
            stream.write(",\n")
        stream.write(textwrap.indent(
            json.dumps(difference, ensure_ascii=False, indent=2), "    "))
    if not first:
        stream.write("\n  ")
    stream.write("],\n")

    stream.write('  "changed": '
                 f'{"true" if comparison.total_changes else "false"},\n')
    summary_text = json.dumps(comparison.summary(), ensure_ascii=False, indent=2)
    stream.write('  "summary": ' + summary_text.replace("\n", "\n  ") + "\n")
    stream.write("}\n")


def _ensure_output_outside_archives(out_path: Path,
                                    before: Path, after: Path) -> None:
    """输入档案只读：拒绝把目标报告写进任一档案目录。"""
    target = out_path.resolve()
    for archive in (before, after):
        root = archive.resolve()
        if target == root or root in target.parents:
            raise ValueError(
                f"输出报告不能写进输入档案目录：{out_path}（档案 {archive}）")


def _write_atomic(out_path: Path, comparison: Comparison,
                  before_label: str, after_label: str) -> None:
    parent = out_path.parent if str(out_path.parent) else Path(".")
    if not parent.is_dir():
        raise OSError(f"输出目录不存在：{parent}")
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{out_path.name}.", suffix=".tmp", dir=str(parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            _write_stream(fh, comparison, before_label, after_label)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, out_path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def write_comparison_report(before: str | os.PathLike[str],
                            after: str | os.PathLike[str],
                            out_path: str | os.PathLike[str] | None = None) -> dict[str, Any]:
    """执行对比并写出 JSON 报告。

    ``out_path`` 为 ``None`` 时输出到 stdout。目标报告只在全部页校验、全部差异
    生成成功后才原子替换；任何失败都不会创建/替换目标文件。
    """
    before_p, after_p = Path(before), Path(after)
    if out_path is not None:
        _ensure_output_outside_archives(Path(out_path), before_p, after_p)

    with Comparison(before_p, after_p) as comparison:
        if out_path is not None:
            _write_atomic(Path(out_path), comparison, str(before_p),
                          str(after_p))
        else:
            # 先写临时文件，成功后才复制到 stdout，避免失败时泄漏部分差异。
            with tempfile.TemporaryFile(mode="w+", encoding="utf-8",
                                        newline="\n") as buffer:
                _write_stream(buffer, comparison, str(before_p), str(after_p))
                buffer.flush()
                buffer.seek(0)
                shutil.copyfileobj(buffer, sys.stdout)
        return comparison.summary()


def compare_archives(before: str | os.PathLike[str],
                     after: str | os.PathLike[str]) -> dict[str, Any]:
    """程序化入口：返回完整差异报告字典（CLI 使用流式写出）。"""
    with Comparison(before, after) as comparison:
        differences = list(comparison.differences())
        return {
            "format": DIFF_FORMAT,
            "version": DIFF_VERSION,
            "before_archive": str(before),
            "after_archive": str(after),
            "structure_identical": True,
            "changed": bool(differences),
            "differences": differences,
            "summary": comparison.summary(),
        }
