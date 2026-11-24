"""manifest / structure 加载与档案级校验。"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from .errors import ArchiveError
from .models import PageRef, Structure

PREDICATE_COLUMNS = ("inspection_category", "inspection_result")
PROJECTION_COLUMNS = ("inspection_detail", "component_remark")
ALL_COLUMNS = PREDICATE_COLUMNS + PROJECTION_COLUMNS
COLUMN_LEVELS = {
    "inspection_category": "inspection",
    "inspection_result": "inspection",
    "inspection_detail": "inspection",
    "component_remark": "component",
}
COLUMN_KINDS = {
    "inspection_category": "predicate",
    "inspection_result": "predicate",
    "inspection_detail": "projection",
    "component_remark": "projection",
}
NULLABLE_COLUMNS = PROJECTION_COLUMNS  # 谓词列本身允许 null，仅谓词匹配语义上排除 null


def _safe_join(archive_dir: Path, rel_path: str) -> Path:
    """页路径必须是档案目录内的相对路径，禁止绝对路径与 '..' 逃逸。"""
    if not rel_path or os.path.isabs(rel_path):
        raise ArchiveError(f"非法页路径（必须为相对路径）: {rel_path!r}")
    normalized = os.path.normpath(rel_path)
    if normalized.startswith("..") or normalized.startswith("/"):
        raise ArchiveError(f"页路径越出档案目录: {rel_path!r}")
    full = (archive_dir / normalized).resolve()
    root = archive_dir.resolve()
    if root not in full.parents and full != root:
        raise ArchiveError(f"页路径越出档案目录: {rel_path!r}")
    return full


def load_structure(archive_dir: str | os.PathLike[str]) -> Structure:
    """读取并校验 structure.json。结构无法解释直接抛 ArchiveError。"""
    archive_dir = Path(archive_dir)
    structure_path = archive_dir / "structure.json"
    try:
        raw = structure_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ArchiveError(f"结构文件不可读: {structure_path}: {exc}") from exc
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ArchiveError(f"结构文件不是合法 JSON: {structure_path}: {exc}") from exc

    if not isinstance(data, dict):
        raise ArchiveError("structure.json 顶层必须是对象")
    comp_ranges_raw = data.get("component_ranges")
    insp_ranges_raw = data.get("inspection_ranges")
    if not isinstance(comp_ranges_raw, list) or not isinstance(insp_ranges_raw, list):
        raise ArchiveError("structure.json 必须包含 component_ranges / inspection_ranges 两个数组")

    comp_ranges = _validate_ranges(comp_ranges_raw, "component_ranges")
    insp_ranges = _validate_ranges(insp_ranges_raw, "inspection_ranges")
    return Structure(component_ranges=comp_ranges, inspection_ranges=insp_ranges)


def _validate_ranges(raw: list, name: str) -> tuple[tuple[int, int], ...]:
    ranges: list[tuple[int, int]] = []
    cursor = 0
    for i, item in enumerate(raw):
        if not (isinstance(item, list) and len(item) == 2
                and all(isinstance(x, int) and not isinstance(x, bool) for x in item)):
            raise ArchiveError(f"{name}[{i}] 必须是 [start, end] 整数对")
        start, end = item
        if start < 0 or end < start:
            raise ArchiveError(f"{name}[{i}] 区间非法: {item}")
        if start != cursor:
            raise ArchiveError(
                f"{name}[{i}] 区间不连续: 期望起点 {cursor}，实际 {start}（不允许缺口/重叠）")
        ranges.append((start, end))
        cursor = end
    return tuple(ranges)


def load_manifest(archive_dir: str | os.PathLike[str],
                  structure: Structure) -> dict[str, list[PageRef]]:
    """读取并校验 manifest.json，返回 {列名: [PageRef, ...]}。"""
    archive_dir = Path(archive_dir)
    manifest_path = archive_dir / "manifest.json"
    try:
        raw = manifest_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ArchiveError(f"清单文件不可读: {manifest_path}: {exc}") from exc
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ArchiveError(f"清单文件不是合法 JSON: {manifest_path}: {exc}") from exc

    if not isinstance(data, dict):
        raise ArchiveError("manifest.json 顶层必须是对象")
    if data.get("format") != "qc-columnar-archive" or data.get("version") != 1:
        raise ArchiveError("manifest 格式标识/版本不受支持（仅支持 qc-columnar-archive v1）")

    counts = data.get("logical_slot_counts")
    if not isinstance(counts, dict):
        raise ArchiveError("manifest 缺少 logical_slot_counts")
    expected = {
        "equipment": structure.equipment_count,
        "component": structure.component_count,
        "inspection": structure.inspection_count,
    }
    for key, want in expected.items():
        got = counts.get(key)
        if not isinstance(got, int) or isinstance(got, bool) or got != want:
            raise ArchiveError(
                f"logical_slot_counts.{key}={got!r} 与 structure.json 推导值 {want} 不一致")

    columns = data.get("columns")
    if not isinstance(columns, dict):
        raise ArchiveError("manifest 缺少 columns 对象")
    if set(columns) != set(ALL_COLUMNS):
        raise ArchiveError(
            f"列集合必须恰好为 {sorted(ALL_COLUMNS)}，实际为 {sorted(columns)}")

    result: dict[str, list[PageRef]] = {}
    for name in ALL_COLUMNS:
        result[name] = _validate_column(name, columns[name], archive_dir,
                                        expected_slot_count=expected[COLUMN_LEVELS[name]])
    return result


def _validate_column(name: str, spec: Any, archive_dir: Path,
                     expected_slot_count: int) -> list[PageRef]:
    if not isinstance(spec, dict):
        raise ArchiveError(f"列 {name} 的定义必须是对象")
    if spec.get("level") != COLUMN_LEVELS[name]:
        raise ArchiveError(f"列 {name} 的 level 必须为 {COLUMN_LEVELS[name]}")
    if spec.get("kind") != COLUMN_KINDS[name]:
        raise ArchiveError(f"列 {name} 的 kind 必须为 {COLUMN_KINDS[name]}")
    pages_raw = spec.get("pages")
    if not isinstance(pages_raw, list):
        raise ArchiveError(f"列 {name} 缺少 pages 数组")

    refs: list[PageRef] = []
    cursor = 0
    for i, item in enumerate(pages_raw):
        if not isinstance(item, dict):
            raise ArchiveError(f"列 {name} 的 pages[{i}] 必须是对象")
        try:
            ref = PageRef(
                page_id=str(item["page_id"]),
                path=str(item["path"]),
                slot_start=int(item["slot_start"]),
                slot_end=int(item["slot_end"]),
                sha256=str(item["sha256"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ArchiveError(f"列 {name} 的 pages[{i}] 字段不完整或非法: {exc}") from exc
        if ref.slot_start < 0 or ref.slot_end <= ref.slot_start:
            raise ArchiveError(f"列 {name} 的页 {ref.page_id} 槽区间非法")
        if ref.slot_start != cursor:
            raise ArchiveError(
                f"列 {name} 的页区间不连续: 页 {ref.page_id} 起点 {ref.slot_start}，期望 {cursor}")
        if len(ref.sha256) != 64:
            raise ArchiveError(f"列 {name} 的页 {ref.page_id} sha256 必须为 64 位十六进制")
        _safe_join(archive_dir, ref.path)  # 提前拦截路径逃逸
        refs.append(ref)
        cursor = ref.slot_end

    if cursor != expected_slot_count:
        raise ArchiveError(
            f"列 {name} 分页覆盖 [0,{cursor}) 与结构槽总数 {expected_slot_count} 不一致")
    return refs


def sha256_file(path: Path) -> tuple[str, int]:
    """计算文件摘要与字节数。"""
    h = hashlib.sha256()
    size = 0
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
            size += len(chunk)
    return h.hexdigest(), size
