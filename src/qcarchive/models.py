"""数据模型。"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(frozen=True)
class PageRef:
    """manifest 中登记的一页：覆盖半开槽区间 [slot_start, slot_end)。"""

    page_id: str
    path: str
    slot_start: int
    slot_end: int
    sha256: str

    def covers(self, slot: int) -> bool:
        return self.slot_start <= slot < self.slot_end

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Structure:
    """两层父子边界（半开区间）。

    component_ranges[e]  = 设备 e 包含的部件槽区间
    inspection_ranges[c] = 部件 c 包含的检查槽区间
    """

    component_ranges: tuple[tuple[int, int], ...]
    inspection_ranges: tuple[tuple[int, int], ...]

    @property
    def equipment_count(self) -> int:
        return len(self.component_ranges)

    @property
    def component_count(self) -> int:
        return len(self.inspection_ranges)

    @property
    def inspection_count(self) -> int:
        return self.inspection_ranges[-1][1] if self.inspection_ranges else 0

    def equipment_of_component(self, component_slot: int) -> int:
        """返回部件槽所属的设备槽。"""
        import bisect

        ends = [end for _, end in self.component_ranges]
        idx = bisect.bisect_right(ends, component_slot)
        if idx >= len(self.component_ranges):
            raise ValueError(f"component slot {component_slot} 超出结构边界")
        start, end = self.component_ranges[idx]
        if not (start <= component_slot < end):
            raise ValueError(f"component slot {component_slot} 超出结构边界")
        return idx

    def component_of_inspection(self, inspection_slot: int) -> int:
        """返回检查槽所属的部件槽。"""
        import bisect

        ends = [end for _, end in self.inspection_ranges]
        idx = bisect.bisect_right(ends, inspection_slot)
        if idx >= len(self.inspection_ranges):
            raise ValueError(f"inspection slot {inspection_slot} 超出结构边界")
        start, end = self.inspection_ranges[idx]
        if not (start <= inspection_slot < end):
            return idx  # 空部件区间不会被调用，防御性返回
        return idx


@dataclass
class ReadRecord:
    """一次实际载荷读页的可核查记录。"""

    phase: str            # "predicate" | "projection"
    column: str
    page_id: str
    path: str
    slot_start: int
    slot_end: int
    reason: str
    status: str = "ok"    # ok | missing | checksum_mismatch | malformed_json | invalid_page
    bytes_read: int = 0
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Hit:
    """一次查询命中（或命中但不可交付）。"""

    equipment_ordinal: int
    component_ordinal_in_equipment: int
    inspection_ordinal_in_component: int
    equipment_slot: int
    component_slot: int
    inspection_slot: int
    category: Any
    result: Any
    detail: Any = None
    component_remark: Any = None
    status: str = "delivered"          # delivered | undeliverable
    missing_pages: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class QueryReport:
    """一次查询的完整交付报告（含可核查读页记录）。"""

    category: str
    result: str
    archive_path: str
    hits: list[Hit]
    read_records: list[ReadRecord]
    archive_error: str | None = None

    @property
    def delivered_count(self) -> int:
        return sum(1 for h in self.hits if h.status == "delivered")

    @property
    def undeliverable_count(self) -> int:
        return sum(1 for h in self.hits if h.status == "undeliverable")

    def to_dict(self) -> dict[str, Any]:
        return {
            "category": self.category,
            "result": self.result,
            "archive_path": self.archive_path,
            "archive_error": self.archive_error,
            "delivered_count": self.delivered_count,
            "undeliverable_count": self.undeliverable_count,
            "hits": [h.to_dict() for h in self.hits],
            "payload_reads": [r.to_dict() for r in self.read_records],
        }
