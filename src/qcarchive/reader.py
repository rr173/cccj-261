"""带闸门的页读取器与离线查询引擎。

读取纪律（见 docs/format.md 第 6 节）：
- 谓词阶段：类别/结果两列按槽对齐、双游标流式扫描，任一时刻每列只保留当前页；
- 投影阶段：明细/备注页只有覆盖“命中检查槽”或“命中检查所属部件槽”才允许打开；
- 每次实际读页都写入 ReadRecord，可核查；
- 投影页损坏只令相关命中不可交付；结构/谓词页损坏整体报档案错误。
"""

from __future__ import annotations

import bisect
import json
from pathlib import Path
from typing import Any

from .errors import ArchiveError, ProjectionPageError
from .manifest import load_manifest, load_structure, sha256_file
from .models import Hit, PageRef, QueryReport, ReadRecord, Structure

PHASE_PREDICATE = "predicate"
PHASE_PROJECTION = "projection"


class PageHandle:
    """一页的只读句柄，关闭后释放整页 values，不随档案总量驻留内存。"""

    def __init__(self, ref: PageRef, values: list[Any],
                 on_close: "callable | None" = None):
        self.ref = ref
        self._values = values
        self._on_close = on_close
        self._closed = False

    def get(self, slot: int) -> Any:
        """按绝对逻辑槽取值；槽身份即结构位置，空值返回 None（显式 null）。"""
        if self._closed:
            raise RuntimeError(f"页 {self.ref.page_id} 已关闭，不能再取值")
        if not self.ref.covers(slot):
            raise KeyError(f"槽 {slot} 不在页 {self.ref.page_id} 覆盖范围内")
        return self._values[slot - self.ref.slot_start]

    def close(self) -> None:
        if self._closed:
            return
        self._values = None  # type: ignore[assignment]
        self._closed = True
        if self._on_close is not None:
            self._on_close()


class _ScanState:
    """单列流式扫描游标：单调翻页，同一时刻只驻留一页。"""

    def __init__(self, refs: list[PageRef]):
        self.refs = refs
        self.page_idx = 0
        self.handle: PageHandle | None = None

    def close(self) -> None:
        if self.handle is not None:
            self.handle.close()
            self.handle = None

    def ensure_page(self, slot: int, reader: "GatedPageReader", column: str) -> PageHandle:
        """推进到覆盖 slot 的页（槽单调递增，顺序翻页即可）。"""
        while self.page_idx < len(self.refs) and self.refs[self.page_idx].slot_end <= slot:
            self.close()
            self.page_idx += 1
        if self.page_idx >= len(self.refs):
            raise ArchiveError("谓词列分页提前耗尽，无法覆盖全部检查槽")
        ref = self.refs[self.page_idx]
        if self.handle is None or self.handle.ref is not ref:
            self.close()
            self.handle = reader.open_predicate_page(column, ref)
        return self.handle


class GatedPageReader:
    """强制执行投影读取纪律的读取器，并保留全部读页记录。"""

    def __init__(self, archive_dir: str | Path,
                 columns: dict[str, list[PageRef]],
                 selected_inspection_slots: set[int],
                 selected_component_slots: set[int]):
        self.archive_dir = Path(archive_dir)
        self.columns = columns
        self.selected_inspection_slots = selected_inspection_slots
        self.selected_component_slots = selected_component_slots
        self.read_records: list[ReadRecord] = []
        self.live_page_count = 0       # 当前驻留内存的载荷页数（内存纪律自检用）
        self.max_live_page_count = 0

    # ---- 闸门 ----------------------------------------------------------------

    def _gate_reason(self, phase: str, column: str, ref: PageRef) -> str | None:
        """返回允许打开的原因；不允许则返回 None。谓词页始终允许。"""
        if phase == PHASE_PREDICATE:
            return "谓词扫描：允许读取全部谓词页"
        if column == "inspection_detail":
            hit = _first_slot_in_range(ref.slot_start, ref.slot_end,
                                       self.selected_inspection_slots)
            if hit is not None:
                return f"投影按需：页内检查槽 {hit} 是命中槽"
            return None
        if column == "component_remark":
            hit = _first_slot_in_range(ref.slot_start, ref.slot_end,
                                       self.selected_component_slots)
            if hit is not None:
                return f"投影按需：页内部件槽 {hit} 属于命中检查所属部件"
            return None
        return None

    # ---- 实际读页 ------------------------------------------------------------

    def open_predicate_page(self, column: str, ref: PageRef) -> PageHandle:
        return self._open(PHASE_PREDICATE, column, ref)

    def open_projection_page(self, column: str, ref: PageRef) -> PageHandle:
        reason = self._gate_reason(PHASE_PROJECTION, column, ref)
        if reason is None:
            # 闸门拦截：不打开、不发生 I/O，直接失败。
            raise PermissionError(
                f"读取纪律拦截：投影阶段不允许打开列 {column} 的页 {ref.page_id} "
                f"（覆盖 [{ref.slot_start},{ref.slot_end})，不含任何已选槽）")
        return self._open(PHASE_PROJECTION, column, ref)

    def _open(self, phase: str, column: str, ref: PageRef) -> PageHandle:
        full_path = self.archive_dir / ref.path
        record = ReadRecord(
            phase=phase, column=column, page_id=ref.page_id, path=ref.path,
            slot_start=ref.slot_start, slot_end=ref.slot_end,
            reason=self._gate_reason(phase, column, ref) or "")
        try:
            try:
                digest, size = sha256_file(full_path)
                record.bytes_read = size
            except OSError as exc:
                record.status = "missing"
                record.error = str(exc)
                self.read_records.append(record)
                self._raise_load_error(phase, column, ref, record, "页文件缺失或不可读")
            if digest.lower() != ref.sha256.lower():
                record.status = "checksum_mismatch"
                record.error = f"期望 {ref.sha256}，实际 {digest}"
                self.read_records.append(record)
                self._raise_load_error(phase, column, ref, record, "页校验和不匹配（页已损坏）")
            try:
                data = json.loads(full_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                record.status = "malformed_json"
                record.error = str(exc)
                self.read_records.append(record)
                self._raise_load_error(phase, column, ref, record, "页不是合法 JSON（页已损坏）")

            values = self._validate_payload(column, ref, data, record, phase)
        except (ProjectionPageError, ArchiveError):
            raise
        except Exception as exc:  # 防御：任何意外解析失败按页损坏处理
            record.status = "invalid_page"
            record.error = str(exc)
            if record not in self.read_records:
                self.read_records.append(record)
            self._raise_load_error(phase, column, ref, record, "页解析异常")

        self.read_records.append(record)
        self.live_page_count += 1
        self.max_live_page_count = max(self.max_live_page_count, self.live_page_count)
        return PageHandle(ref, values, on_close=self._on_handle_close)

    def _on_handle_close(self) -> None:
        self.live_page_count -= 1

    def _raise_load_error(self, phase: str, column: str, ref: PageRef,
                          record: ReadRecord, message: str) -> None:
        if phase == PHASE_PREDICATE:
            raise ArchiveError(
                f"谓词页不可用，查询无法保证正确性：列 {column} 页 {ref.page_id}：{message}")
        raise ProjectionPageError(
            f"投影页不可交付：列 {column} 页 {ref.page_id}：{message}",
            page_id=ref.page_id, path=ref.path, reason=record.status)

    def _validate_payload(self, column: str, ref: PageRef, data: Any,
                          record: ReadRecord, phase: str) -> list[Any]:
        if not isinstance(data, dict):
            record.status = "invalid_page"
            record.error = "页顶层不是对象"
            self._raise_load_error(phase, column, ref, record, "页结构非法")
        if data.get("page_id") != ref.page_id or data.get("column") != column:
            record.status = "invalid_page"
            record.error = "页头 page_id/column 与 manifest 登记不一致"
            self._raise_load_error(phase, column, ref, record, "页头不一致")
        if data.get("slot_start") != ref.slot_start or data.get("slot_end") != ref.slot_end:
            record.status = "invalid_page"
            record.error = "页头槽范围与 manifest 登记不一致"
            self._raise_load_error(phase, column, ref, record, "页头槽范围不一致")
        values = data.get("values")
        if not isinstance(values, list):
            record.status = "invalid_page"
            record.error = "values 必须是数组"
            self._raise_load_error(phase, column, ref, record, "values 非法")
        if len(values) != ref.slot_end - ref.slot_start:
            record.status = "invalid_page"
            record.error = (f"values 长度 {len(values)} 与槽区间长度 "
                            f"{ref.slot_end - ref.slot_start} 不一致")
            self._raise_load_error(phase, column, ref, record, "values 长度不一致")
        return values


def _first_slot_in_range(start: int, end: int, slots: set[int]) -> int | None:
    """集合中落在半开区间 [start, end) 内的最小槽；无则 None。"""
    hit: int | None = None
    for s in slots:
        if start <= s < end and (hit is None or s < hit):
            hit = s
    return hit


def find_ref(refs: list[PageRef], slot: int) -> PageRef:
    """按绝对槽定位登记页（分页连续无缺口，二分即可）。"""
    ends = [r.slot_end for r in refs]
    idx = bisect.bisect_right(ends, slot)
    if idx >= len(refs) or not refs[idx].covers(slot):
        raise ArchiveError(f"槽 {slot} 在清单分页中找不到对应页（档案分页不连续）")
    return refs[idx]


# ---------------------------------------------------------------------------
# 查询主流程
# ---------------------------------------------------------------------------

def query_archive(archive_dir: str | Path, category: str, result: str) -> QueryReport:
    """离线查询入口：类别=category 且 结果=result 的同槽检查命中。"""
    archive_dir = Path(archive_dir)
    if not isinstance(category, str) or not category:
        raise ValueError("category 必须是非空字符串字面值")
    if not isinstance(result, str) or not result:
        raise ValueError("result 必须是非空字符串字面值")

    structure = load_structure(archive_dir)
    columns = load_manifest(archive_dir, structure)

    # 先以空选中集合构造读取器；谓词阶段闸门对谓词页始终放行。
    reader = GatedPageReader(archive_dir, columns, set(), set())

    # 阶段一：谓词双游标流式扫描，只收集命中槽，不物化整棵树。
    hits = _scan_predicates(reader, columns, structure, category, result)

    # 阶段二：把选中集合装入闸门，仅按需打开投影页。
    reader.selected_inspection_slots = {h["inspection_slot"] for h in hits}
    reader.selected_component_slots = {h["component_slot"] for h in hits}

    detail_col = columns["inspection_detail"]
    remark_col = columns["component_remark"]
    for h in hits:
        h["detail_ref"] = find_ref(detail_col, h["inspection_slot"])
        h["remark_ref"] = find_ref(remark_col, h["component_slot"])

    _load_projection(reader, hits, "inspection_detail", "detail_ref", "detail")
    _load_projection(reader, hits, "component_remark", "remark_ref",
                     "component_remark", component_mode=True)

    # 检查槽号即全档案检查录入顺序，直接按槽排序交付。
    hits.sort(key=lambda h: h["inspection_slot"])
    report = QueryReport(
        category=category, result=result, archive_path=str(archive_dir),
        hits=[], read_records=reader.read_records)
    for h in hits:
        report.hits.append(Hit(
            equipment_ordinal=h["equipment_ordinal"],
            component_ordinal_in_equipment=h["component_ordinal"],
            inspection_ordinal_in_component=h["inspection_ordinal"],
            equipment_slot=h["equipment_slot"],
            component_slot=h["component_slot"],
            inspection_slot=h["inspection_slot"],
            category=category,
            result=result,
            detail=h.get("detail"),
            component_remark=h.get("component_remark"),
            status="undeliverable" if h["missing_pages"] else "delivered",
            missing_pages=h["missing_pages"],
        ))
    return report


def _scan_predicates(reader: GatedPageReader, columns: dict[str, list[PageRef]],
                     structure: Structure, category: str,
                     result: str) -> list[dict[str, Any]]:
    total = structure.inspection_count
    cat_state = _ScanState(columns["inspection_category"])
    res_state = _ScanState(columns["inspection_result"])
    hits: list[dict[str, Any]] = []
    try:
        for slot in range(total):
            cat_handle = cat_state.ensure_page(slot, reader, "inspection_category")
            res_handle = res_state.ensure_page(slot, reader, "inspection_result")

            cat_value = cat_handle.get(slot)
            res_value = res_handle.get(slot)
            # 同槽两列同时满足才算命中；null 不等于任何字面值，
            # 同一部件两次检查各中一列也不会凑成命中。
            if cat_value == category and res_value == result:
                comp_slot = structure.component_of_inspection(slot)
                equip_slot = structure.equipment_of_component(comp_slot)
                comp_start = structure.component_ranges[equip_slot][0]
                insp_start = structure.inspection_ranges[comp_slot][0]
                hits.append({
                    "equipment_slot": equip_slot,
                    "component_slot": comp_slot,
                    "inspection_slot": slot,
                    "equipment_ordinal": equip_slot + 1,
                    "component_ordinal": comp_slot - comp_start + 1,
                    "inspection_ordinal": slot - insp_start + 1,
                    "missing_pages": [],
                })
    finally:
        cat_state.close()
        res_state.close()
    return hits


def _load_projection(reader: GatedPageReader, hits: list[dict[str, Any]],
                     column: str, ref_key: str, value_key: str,
                     component_mode: bool = False) -> None:
    """按需打开投影页：同一页只打开一次，页内服务多个命中。"""
    refs_in_order: list[PageRef] = []
    seen: set[str] = set()
    for h in hits:
        ref = h[ref_key]
        if ref.page_id not in seen:
            seen.add(ref.page_id)
            refs_in_order.append(ref)

    handles: dict[str, PageHandle] = {}
    failed: dict[str, ProjectionPageError] = {}
    try:
        for ref in refs_in_order:
            try:
                handles[ref.page_id] = reader.open_projection_page(column, ref)
            except ProjectionPageError as exc:
                failed[ref.page_id] = exc
        for h in hits:
            ref = h[ref_key]
            if ref.page_id in failed:
                exc = failed[ref.page_id]
                h["missing_pages"].append({
                    "column": column,
                    "page_id": ref.page_id,
                    "path": ref.path,
                    "slot_start": ref.slot_start,
                    "slot_end": ref.slot_end,
                    "reason": exc.reason or "unavailable",
                })
                continue
            slot = h["component_slot"] if component_mode else h["inspection_slot"]
            h[value_key] = handles[ref.page_id].get(slot)  # 显式 null 原样保留
    finally:
        for handle in handles.values():
            handle.close()
