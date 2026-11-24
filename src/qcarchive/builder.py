"""档案构建器：按录入顺序收集数据并写出 qc-columnar-archive v1 列档。

仅供建库、测试与演示使用；查询入口本身只读取，不依赖本模块。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from .manifest import ALL_COLUMNS, COLUMN_LEVELS
from .models import PageRef


class ArchiveBuilder:
    def __init__(self) -> None:
        # 每个部件: {"remark": ..., "inspections": [{"category","result","detail"}, ...]}
        self._equipment: list[list[dict[str, Any]]] = []

    def add_equipment(self) -> int:
        self._equipment.append([])
        return len(self._equipment) - 1

    def add_component(self, equipment_slot: int, remark: Any = None) -> int:
        if not (0 <= equipment_slot < len(self._equipment)):
            raise IndexError("equipment_slot 越界")
        self._equipment[equipment_slot].append({"remark": remark, "inspections": []})
        return sum(len(cs) for cs in self._equipment[:equipment_slot]) \
            + len(self._equipment[equipment_slot]) - 1

    def add_inspection(self, component_slot: int, category: Any,
                       result: Any, detail: Any = None) -> int:
        flat = [c for cs in self._equipment for c in cs]
        if not (0 <= component_slot < len(flat)):
            raise IndexError("component_slot 越界")
        flat[component_slot]["inspections"].append(
            {"category": category, "result": result, "detail": detail})
        return sum(len(c["inspections"]) for c in flat[:component_slot]) \
            + len(flat[component_slot]["inspections"]) - 1

    # ---- 写出 ----------------------------------------------------------------

    def build(self, archive_dir: str | Path,
              page_sizes: dict[str, int] | None = None) -> Path:
        """page_sizes: 每列每页最多承载的槽数；缺省每列一页。"""
        archive_dir = Path(archive_dir)
        (archive_dir / "pages").mkdir(parents=True, exist_ok=True)

        component_ranges: list[list[int]] = []
        inspection_ranges: list[list[int]] = []
        cursor_c = 0
        cursor_i = 0
        for comps in self._equipment:
            component_ranges.append([cursor_c, cursor_c + len(comps)])
            for comp in comps:
                inspection_ranges.append([cursor_i, cursor_i + len(comp["inspections"])])
                cursor_i += len(comp["inspections"])
            cursor_c += len(comps)

        structure = {
            "component_ranges": component_ranges,
            "inspection_ranges": inspection_ranges,
        }
        (archive_dir / "structure.json").write_text(
            json.dumps(structure, ensure_ascii=False, indent=2), encoding="utf-8")

        flat_components = [c for cs in self._equipment for c in cs]
        flat_inspections = [i for c in flat_components for i in c["inspections"]]

        column_values = {
            "inspection_category": [i["category"] for i in flat_inspections],
            "inspection_result": [i["result"] for i in flat_inspections],
            "inspection_detail": [i["detail"] for i in flat_inspections],
            "component_remark": [c["remark"] for c in flat_components],
        }
        slot_totals = {
            "inspection": len(flat_inspections),
            "component": len(flat_components),
        }

        columns_manifest: dict[str, Any] = {}
        for name in ALL_COLUMNS:
            level = COLUMN_LEVELS[name]
            total = slot_totals[level]
            size = (page_sizes or {}).get(name, max(total, 1))
            refs = self._write_column(archive_dir, name, column_values[name], size)
            columns_manifest[name] = {
                "level": level,
                "kind": "predicate" if name in ("inspection_category", "inspection_result")
                else "projection",
                "nullable": True,
                "pages": [self._ref_dict(r) for r in refs],
            }

        manifest = {
            "format": "qc-columnar-archive",
            "version": 1,
            "logical_slot_counts": {
                "equipment": len(self._equipment),
                "component": len(flat_components),
                "inspection": len(flat_inspections),
            },
            "columns": columns_manifest,
        }
        (archive_dir / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        return archive_dir

    def _write_column(self, archive_dir: Path, column: str,
                      values: list[Any], page_size: int) -> list[PageRef]:
        if page_size <= 0:
            raise ValueError("page_size 必须为正整数")
        col_dir = archive_dir / "pages" / column
        col_dir.mkdir(parents=True, exist_ok=True)
        refs: list[PageRef] = []
        if not values:
            return refs
        for idx, start in enumerate(range(0, len(values), page_size)):
            chunk = values[start:start + page_size]
            end = start + len(chunk)
            page_id = f"p{idx}"
            rel_path = f"pages/{column}/{page_id}.json"
            payload = {
                "page_id": page_id,
                "column": column,
                "slot_start": start,
                "slot_end": end,
                "values": chunk,
            }
            full = archive_dir / rel_path
            full.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                            encoding="utf-8")
            digest = hashlib.sha256(full.read_bytes()).hexdigest()
            refs.append(PageRef(page_id=page_id, path=rel_path,
                                slot_start=start, slot_end=end, sha256=digest))
        return refs

    @staticmethod
    def _ref_dict(ref: PageRef) -> dict[str, Any]:
        return {
            "page_id": ref.page_id,
            "path": ref.path,
            "slot_start": ref.slot_start,
            "slot_end": ref.slot_end,
            "sha256": ref.sha256,
        }
