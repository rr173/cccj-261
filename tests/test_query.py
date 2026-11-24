"""端到端测试：命中语义、空值、分页、读取纪律、部分交付、档案错误。"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from qcarchive import ArchiveBuilder, ArchiveError, query_archive  # noqa: E402


def build_standard_archive(root: Path, page_sizes: dict[str, int] | None = None) -> Path:
    """构造含关键场景的三设备档案：

    设备1：部件A（备注“备注A”）
      检查0: 耐压试验/合格
      检查1: 外观检查/不合格     <- 类别结果分属两次，不能与检查2凑命中
      检查2: 耐压试验/不合格     <- 命中，明细“明细2”
      检查3: 耐压试验/null       <- 空结果，不命中
    设备1：部件B（备注 null）
      检查4: 耐压试验/不合格     <- 命中，备注投影为 null 须保留
    设备2：部件C（备注“备注C”）
      检查5: 绝缘测试/合格
      检查6: 耐压试验/不合格     <- 命中，明细 null 须保留
    设备3：空设备（无部件）
    另含一个空部件：设备2 部件D（无检查）
    """
    b = ArchiveBuilder()
    e0 = b.add_equipment()
    cA = b.add_component(e0, remark="备注A")
    b.add_inspection(cA, "耐压试验", "合格", "明细0")
    b.add_inspection(cA, "外观检查", "不合格", "明细1")
    b.add_inspection(cA, "耐压试验", "不合格", "明细2")
    b.add_inspection(cA, "耐压试验", None, "明细3")
    cB = b.add_component(e0, remark=None)
    b.add_inspection(cB, "耐压试验", "不合格", "明细4")

    e1 = b.add_equipment()
    cC = b.add_component(e1, remark="备注C")
    b.add_inspection(cC, "绝缘测试", "合格", "明细5")
    b.add_inspection(cC, "耐压试验", "不合格", None)
    cD = b.add_component(e1, remark="备注D-空部件")  # 空部件，无检查

    b.add_equipment()  # e2 空设备
    return b.build(root, page_sizes=page_sizes)


class QuerySemanticsTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.archive = build_standard_archive(self.tmp / "arc")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_hit_ordering_and_coordinates(self) -> None:
        report = query_archive(self.archive, "耐压试验", "不合格")
        self.assertIsNone(report.archive_error)
        slots = [h.inspection_slot for h in report.hits]
        self.assertEqual(slots, [2, 4, 6])  # 按原检查录入顺序
        h0 = report.hits[0]
        self.assertEqual((h0.equipment_ordinal,
                          h0.component_ordinal_in_equipment,
                          h0.inspection_ordinal_in_component), (1, 1, 3))
        self.assertEqual((h0.equipment_slot, h0.component_slot,
                          h0.inspection_slot), (0, 0, 2))
        self.assertEqual(h0.detail, "明细2")
        self.assertEqual(h0.component_remark, "备注A")
        self.assertEqual(h0.status, "delivered")
        # 第二个命中在部件B，备注 null 必须保留
        self.assertIsNone(report.hits[1].component_remark)
        # 第三个命中明细 null 必须保留
        self.assertIsNone(report.hits[2].detail)
        self.assertEqual(report.hits[2].component_remark, "备注C")

    def test_no_cross_inspection_match(self) -> None:
        # 检查1 是 外观/不合格，检查2 是 耐压/不合格；
        # 查询 外观检查+合格 不存在同槽组合，必须 0 命中。
        report = query_archive(self.archive, "外观检查", "合格")
        self.assertEqual(report.hits, [])
        # 检查1 单独外观+不合格 可以命中
        report2 = query_archive(self.archive, "外观检查", "不合格")
        self.assertEqual([h.inspection_slot for h in report2.hits], [1])

    def test_null_predicate_never_matches(self) -> None:
        report = query_archive(self.archive, "耐压试验", "不合格")
        self.assertNotIn(3, [h.inspection_slot for h in report.hits])
        # 空结果槽（检查3）即便类别相同也不命中
        report_none = query_archive(self.archive, "耐压试验", "合格")
        self.assertEqual([h.inspection_slot for h in report_none.hits], [0])

    def test_empty_literals_rejected(self) -> None:
        with self.assertRaises(ValueError):
            query_archive(self.archive, "", "不合格")
        with self.assertRaises(ValueError):
            query_archive(self.archive, "耐压试验", "")

    def test_empty_archive_allowed(self) -> None:
        b = ArchiveBuilder()
        b.add_equipment()  # 空设备
        arc = b.build(self.tmp / "empty")
        report = query_archive(arc, "耐压试验", "不合格")
        self.assertEqual(report.hits, [])
        # 空槽空间没有任何载荷页被打开
        self.assertEqual(report.read_records, [])


class PagingAndGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        # 各列独立分页、不同分界
        self.archive = build_standard_archive(
            self.tmp / "arc",
            page_sizes={
                "inspection_category": 3,
                "inspection_result": 5,
                "inspection_detail": 2,
                "component_remark": 1,
            })

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_projection_pages_pruned(self) -> None:
        report = query_archive(self.archive, "耐压试验", "不合格")
        hit_i = {2, 4, 6}
        hit_c = {0, 1, 2}
        detail_reads = [r for r in report.read_records
                        if r.phase == "projection" and r.column == "inspection_detail"]
        remark_reads = [r for r in report.read_records
                        if r.phase == "projection" and r.column == "component_remark"]

        # 明细页槽区间（页大小2，7槽）：[0,2) [2,4) [4,6) [6,7)
        # 命中槽 2,4,6 -> 只允许 [2,4) [4,6) [6,7)；[0,2) 绝不能打开
        opened_detail_ranges = [(r.slot_start, r.slot_end) for r in detail_reads]
        self.assertEqual(opened_detail_ranges, [(2, 4), (4, 6), (6, 7)])
        for r in detail_reads:
            self.assertTrue(
                any(r.slot_start <= s < r.slot_end for s in hit_i),
                f"明细页 {r.page_id} 不含任何命中槽，违反读取纪律")
        # 备注页每部件一页：命中部件槽 0,1,2 -> 三页；部件3（空部件D）不得打开
        opened_remark = [r.slot_start for r in remark_reads]
        self.assertEqual(sorted(opened_remark), [0, 1, 2])
        self.assertEqual(len(remark_reads), 3)

        # 所有投影读记录都带闸门原因
        for r in detail_reads + remark_reads:
            self.assertTrue(r.reason.startswith("投影按需"))
            self.assertEqual(r.status, "ok")
            self.assertGreater(r.bytes_read, 0)

    def test_predicate_pages_all_scanned_but_streaming(self) -> None:
        report = query_archive(self.archive, "耐压试验", "不合格")
        cat_reads = [r for r in report.read_records
                     if r.column == "inspection_category"]
        res_reads = [r for r in report.read_records
                     if r.column == "inspection_result"]
        # 类别页大小3，7槽 -> 3页；结果页大小5 -> 2页，全部谓词页都读过
        self.assertEqual([(r.slot_start, r.slot_end) for r in cat_reads],
                         [(0, 3), (3, 6), (6, 7)])
        self.assertEqual([(r.slot_start, r.slot_end) for r in res_reads],
                         [(0, 5), (5, 7)])

    def test_memory_does_not_grow_with_full_payload(self) -> None:
        # 再建一个明细列页很小、总量很大的档案；驻留页数应保持常数。
        b = ArchiveBuilder()
        e = b.add_equipment()
        c = b.add_component(e, remark="R")
        for i in range(50):
            b.add_inspection(c, "耐压试验", "合格" if i % 2 else "不合格",
                             detail=f"d{i}")
        arc = b.build(self.tmp / "big", page_sizes={
            "inspection_category": 7, "inspection_result": 11,
            "inspection_detail": 3, "component_remark": 2})
        report = query_archive(arc, "耐压试验", "不合格")
        # 25 个命中，明细列共 17 页，按命中槽逐个区间页打开；
        # 同时驻留页数始终为 1（逐页打开、逐页关闭），由内部测试精确断言。
        self.assertEqual(report.delivered_count, 25)
        detail_reads = [r for r in report.read_records
                        if r.phase == "projection" and r.column == "inspection_detail"]
        self.assertEqual(len(detail_reads), 17)


class ReaderInternalsTests(unittest.TestCase):
    def test_live_page_counter_bounded(self) -> None:
        from qcarchive.reader import GatedPageReader, _ScanState
        with tempfile.TemporaryDirectory() as d:
            archive = build_standard_archive(
                Path(d) / "arc",
                page_sizes={"inspection_category": 3, "inspection_result": 5,
                            "inspection_detail": 2, "component_remark": 1})
            from qcarchive.manifest import load_manifest, load_structure
            structure = load_structure(archive)
            columns = load_manifest(archive, structure)
            reader = GatedPageReader(archive, columns, {2, 4, 6}, {0, 1, 2})
            st = _ScanState(columns["inspection_category"])
            try:
                for slot in range(structure.inspection_count):
                    h = st.ensure_page(slot, reader, "inspection_category")
                    self.assertIsNotNone(h.get(slot))
                    self.assertLessEqual(reader.live_page_count, 1)
                self.assertEqual(reader.max_live_page_count, 1)
            finally:
                st.close()
            self.assertEqual(reader.live_page_count, 0)

    def test_gate_blocks_unselected_projection_page(self) -> None:
        from qcarchive.reader import GatedPageReader
        with tempfile.TemporaryDirectory() as d:
            archive = build_standard_archive(Path(d) / "arc")
            from qcarchive.manifest import load_manifest, load_structure
            structure = load_structure(archive)
            columns = load_manifest(archive, structure)
            reader = GatedPageReader(archive, columns, selected_inspection_slots={2},
                                     selected_component_slots=set())
            # 单页覆盖全部 7 槽但不含选中槽？构造一个假 ref 模拟无交集页
            from qcarchive import PageRef
            fake = PageRef("px", "pages/inspection_detail/p0.json", 100, 200,
                           "0" * 64)
            with self.assertRaises(PermissionError):
                reader.open_projection_page("inspection_detail", fake)
            # 被拦截不产生读页记录、无 I/O
            self.assertEqual(reader.read_records, [])


class PartialDeliveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.archive = build_standard_archive(
            self.tmp / "arc",
            page_sizes={"inspection_category": 3, "inspection_result": 5,
                        "inspection_detail": 2, "component_remark": 1})

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _page_path(self, column: str, page_id: str) -> Path:
        manifest = json.loads((self.archive / "manifest.json").read_text("utf-8"))
        for p in manifest["columns"][column]["pages"]:
            if p["page_id"] == page_id:
                return self.archive / p["path"]
        raise KeyError(page_id)

    def test_corrupt_detail_page_only_affects_its_hits(self) -> None:
        # 明细页 [2,4)（含命中槽2）损坏 -> 命中2不可交付；命中4、6照常
        path = self._page_path("inspection_detail", "p1")
        path.write_bytes(path.read_bytes() + b"!!corrupt!!")
        report = query_archive(self.archive, "耐压试验", "不合格")
        statuses = {h.inspection_slot: h.status for h in report.hits}
        self.assertEqual(statuses[2], "undeliverable")
        self.assertEqual(statuses[4], "delivered")
        self.assertEqual(statuses[6], "delivered")
        bad = next(h for h in report.hits if h.inspection_slot == 2)
        self.assertEqual(len(bad.missing_pages), 1)
        mp = bad.missing_pages[0]
        self.assertEqual(mp["column"], "inspection_detail")
        self.assertEqual(mp["page_id"], "p1")
        self.assertEqual(mp["reason"], "checksum_mismatch")
        rec = next(r for r in report.read_records
                   if r.column == "inspection_detail" and r.page_id == "p1")
        self.assertEqual(rec.status, "checksum_mismatch")

    def test_missing_remark_page_isolated(self) -> None:
        # 备注部件槽2 的页删除 -> 仅命中6不可交付；备注读页记录定位 missing
        path = self._page_path("component_remark", "p2")
        path.unlink()
        report = query_archive(self.archive, "耐压试验", "不合格")
        by_slot = {h.inspection_slot: h for h in report.hits}
        self.assertEqual(by_slot[6].status, "undeliverable")
        self.assertEqual(by_slot[2].status, "delivered")
        self.assertEqual(by_slot[4].status, "delivered")
        rec = next(r for r in report.read_records
                   if r.column == "component_remark" and r.page_id == "p2")
        self.assertEqual(rec.status, "missing")

    def test_predicate_failure_is_archive_error(self) -> None:
        path = self._page_path("inspection_category", "p0")
        path.write_bytes(b"{not json")
        with self.assertRaises(ArchiveError):
            query_archive(self.archive, "耐压试验", "不合格")


class ArchiveValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.archive = build_standard_archive(self.tmp / "arc")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_broken_structure_ranges_archive_error(self) -> None:
        sp = self.archive / "structure.json"
        data = json.loads(sp.read_text("utf-8"))
        data["inspection_ranges"][1] = [0, 1]  # 制造重叠/不连续
        sp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        with self.assertRaises(ArchiveError):
            query_archive(self.archive, "耐压试验", "不合格")

    def test_manifest_slot_count_mismatch_archive_error(self) -> None:
        mp = self.archive / "manifest.json"
        data = json.loads(mp.read_text("utf-8"))
        data["logical_slot_counts"]["inspection"] = 99
        mp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        with self.assertRaises(ArchiveError):
            query_archive(self.archive, "耐压试验", "不合格")

    def test_path_traversal_rejected(self) -> None:
        mp = self.archive / "manifest.json"
        data = json.loads(mp.read_text("utf-8"))
        data["columns"]["inspection_detail"]["pages"][0]["path"] = "../../etc/passwd"
        mp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        with self.assertRaises(ArchiveError):
            query_archive(self.archive, "耐压试验", "不合格")

    def test_missing_manifest_archive_error(self) -> None:
        (self.archive / "manifest.json").unlink()
        with self.assertRaises(ArchiveError):
            query_archive(self.archive, "耐压试验", "不合格")


if __name__ == "__main__":
    unittest.main(verbosity=2)
