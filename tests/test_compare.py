"""端到端测试：两份快照的结构/载荷差异、排序、空值、分页、失败原子性。"""

from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from qcarchive import (  # noqa: E402
    ArchiveBuilder,
    ArchiveError,
    compare_archives,
    write_comparison_report,
)
from qcarchive.cli import main as cli_main  # noqa: E402


def _hash_tree(root: Path) -> dict[str, str]:
    return {
        str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(root.rglob("*")) if p.is_file()
    }


def build_pair_archives(root: Path,
                        before_sizes: dict[str, int] | None = None,
                        after_sizes: dict[str, int] | None = None) -> tuple[Path, Path]:
    """同一结构、不同内容的两次快照：

    设备1
      部件0（备注 A -> A2，变化）
        检查0: 耐压试验/合格/明细0                    -> 不变
        检查1: 外观检查/不合格/明细1                   -> 类别、结果、明细都变
        检查2: 耐压试验/不合格/明细2                   -> 仅明细变
        检查3: 耐压试验/null/明细3                     -> 仅结果 null -> 合格
      部件1（备注 null -> null，不变）
        检查4: 耐压试验/不合格/明细4                   -> 不变
      部件2（备注 备注C -> null，变化；其下检查均不变）
        检查5: 绝缘测试/合格/明细5                     -> 不变
    设备2
      部件3（备注 备注D -> 备注D，不变）
        检查6: 耐压试验/不合格/明细6                   -> 明细 dict 值变化
        检查7: 耐压试验/不合格/明细7                   -> 不变
    """
    def make(name: str, sizes, changed: bool) -> Path:
        b = ArchiveBuilder()
        e0 = b.add_equipment()
        c0 = b.add_component(e0, remark="A2" if changed else "A")
        b.add_inspection(c0, "耐压试验", "合格", "明细0")
        if changed:
            b.add_inspection(c0, "绝缘测试", "合格", "明细1-新")
        else:
            b.add_inspection(c0, "外观检查", "不合格", "明细1")
        b.add_inspection(c0, "耐压试验", "不合格", "明细2-新" if changed else "明细2")
        b.add_inspection(c0, "耐压试验", "合格" if changed else None, "明细3")
        c1 = b.add_component(e0, remark=None)
        b.add_inspection(c1, "耐压试验", "不合格", "明细4")
        c2 = b.add_component(e0, remark=None if changed else "备注C")
        b.add_inspection(c2, "绝缘测试", "合格", "明细5")
        e1 = b.add_equipment()
        c3 = b.add_component(e1, remark="备注D")
        b.add_inspection(c3, "耐压试验", "不合格",
                         {"v": 2} if changed else {"v": 1})
        b.add_inspection(c3, "耐压试验", "不合格", "明细7")
        return b.build(root / name, page_sizes=sizes)

    before = make("before", before_sizes, changed=False)
    after = make("after", after_sizes, changed=True)
    return before, after


class CompareSemanticsTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_diff_order_values_and_no_unchanged(self) -> None:
        before, after = build_pair_archives(self.tmp)
        report = compare_archives(before, after)
        diffs = report["differences"]
        self.assertTrue(report["changed"])
        self.assertEqual(len(diffs), 6)  # 备注 c0、c2 + 检查 1、2、3、6

        # 排序：设备→部件；同部件备注在前；检查按槽序。
        expected = [
            ("component_remark", 0, None),
            ("inspection", 0, 1),
            ("inspection", 0, 2),
            ("inspection", 0, 3),
            ("component_remark", 2, None),
            ("inspection", 3, 6),
        ]
        actual = [(d["type"], d["component_slot"], d.get("inspection_slot"))
                  for d in diffs]
        self.assertEqual(actual, expected)

        d0 = diffs[0]
        self.assertEqual(d0["equipment_ordinal"], 1)
        self.assertEqual(d0["component_ordinal_in_equipment"], 1)
        self.assertEqual((d0["remark_before"], d0["remark_after"]), ("A", "A2"))

        insp1 = diffs[1]
        self.assertEqual(insp1["inspection_ordinal_in_component"], 2)
        self.assertEqual((insp1["category_before"], insp1["category_after"]),
                         ("外观检查", "绝缘测试"))
        self.assertEqual((insp1["result_before"], insp1["result_after"]),
                         ("不合格", "合格"))
        self.assertEqual((insp1["detail_before"], insp1["detail_after"]),
                         ("明细1", "明细1-新"))
        self.assertEqual(insp1["changed_fields"],
                         ["inspection_category", "inspection_result",
                          "inspection_detail"])

        insp2 = diffs[2]
        self.assertEqual(insp2["changed_fields"], ["inspection_detail"])

        insp3 = diffs[3]
        self.assertEqual((insp3["result_before"], insp3["result_after"]),
                         (None, "合格"))
        self.assertEqual(insp3["changed_fields"], ["inspection_result"])

        remark_c2 = diffs[4]
        self.assertEqual(remark_c2["component_slot"], 2)
        self.assertEqual((remark_c2["remark_before"],
                          remark_c2["remark_after"]), ("备注C", None))
        # 该部件下未变化的检查 5 不重复报告
        self.assertNotIn(5, [d.get("inspection_slot") for d in diffs])

        insp6 = diffs[5]
        self.assertEqual(insp6["equipment_ordinal"], 2)
        self.assertEqual((insp6["detail_before"], insp6["detail_after"]),
                         ({"v": 1}, {"v": 2}))
        self.assertEqual(insp6["changed_fields"], ["inspection_detail"])

        summary = report["summary"]
        self.assertEqual(summary["component_remark_changes"], 2)
        self.assertEqual(summary["inspection_changes"], 4)
        self.assertEqual(summary["total_changes"], 6)

    def test_identical_null_values_not_reported(self) -> None:
        before, _ = build_pair_archives(self.tmp)
        report = compare_archives(before, before)
        self.assertFalse(report["changed"])
        self.assertEqual(report["differences"], [])
        self.assertEqual(report["summary"]["total_changes"], 0)

    def test_type_sensitive_json_comparison(self) -> None:
        # 1 与 true 在 Python 里相等，但作为 JSON 实际值不同，必须报告。
        b = ArchiveBuilder()
        e = b.add_equipment()
        c = b.add_component(e, remark=1)
        b.add_inspection(c, "x", "y", 1)
        before = b.build(self.tmp / "b1")

        b2 = ArchiveBuilder()
        e2 = b2.add_equipment()
        c2 = b2.add_component(e2, remark=True)
        b2.add_inspection(c2, "x", "y", True)
        after = b2.build(self.tmp / "b2")

        report = compare_archives(before, after)
        self.assertEqual(len(report["differences"]), 2)
        self.assertEqual(report["differences"][0]["type"], "component_remark")
        self.assertEqual(report["differences"][1]["changed_fields"],
                         ["inspection_detail"])

    def test_independent_paging_still_aligns_by_slot(self) -> None:
        before, after = build_pair_archives(
            self.tmp,
            before_sizes={"inspection_category": 1, "inspection_result": 2,
                          "inspection_detail": 3, "component_remark": 2},
            after_sizes={"inspection_category": 5, "inspection_result": 1,
                         "inspection_detail": 2, "component_remark": 1})
        report = compare_archives(before, after)
        self.assertEqual(report["summary"]["total_changes"], 6)
        self.assertEqual([d["type"] for d in report["differences"]],
                         ["component_remark", "inspection", "inspection",
                          "inspection", "component_remark", "inspection"])

    def test_all_pages_verified_even_after_last_change(self) -> None:
        before, after = build_pair_archives(
            self.tmp,
            before_sizes={"inspection_category": 2, "inspection_result": 3,
                          "inspection_detail": 1, "component_remark": 1},
            after_sizes={"inspection_category": 3, "inspection_result": 2,
                         "inspection_detail": 2, "component_remark": 3})
        report = compare_archives(before, after)

        for archive, side in ((before, "before"), (after, "after")):
            manifest = json.loads((archive / "manifest.json").read_text("utf-8"))
            for column, spec in manifest["columns"].items():
                self.assertEqual(
                    report["summary"]["pages_verified"][side][column],
                    len(spec["pages"]),
                    f"{side} 档案列 {column} 未覆盖全部页")

    def test_empty_component_and_inspection_spaces(self) -> None:
        b = ArchiveBuilder()
        b.add_equipment()
        b.add_equipment()
        before = b.build(self.tmp / "e1")
        after = b.build(self.tmp / "e2")
        report = compare_archives(before, after)
        self.assertFalse(report["changed"])
        self.assertEqual(report["summary"]["pages_verified"]["before"],
                         {name: 0 for name in (
                             "inspection_category", "inspection_result",
                             "inspection_detail", "component_remark")})

    def test_streaming_page_working_set_is_bounded(self) -> None:
        # 大量分页时，任一时刻每档案驻留页数不超过列数（4），
        # 与档案总页数/明细总量无关。
        b = ArchiveBuilder()
        e = b.add_equipment()
        c = b.add_component(e, remark="R")
        for i in range(200):
            b.add_inspection(c, "耐压试验", "合格", f"d{i}")
        before = b.build(self.tmp / "big1", page_sizes={
            "inspection_category": 7, "inspection_result": 11,
            "inspection_detail": 3, "component_remark": 1})
        after = b.build(self.tmp / "big2", page_sizes={
            "inspection_category": 5, "inspection_result": 13,
            "inspection_detail": 4, "component_remark": 2})

        from qcarchive.compare import Comparison
        with Comparison(before, after) as comparison:
            for _ in comparison.differences():
                self.assertLessEqual(comparison.before.max_live_pages,
                                     len(comparison.before.page_counts))
                self.assertLessEqual(comparison.after.max_live_pages,
                                     len(comparison.after.page_counts))
            self.assertEqual(comparison.before.max_live_pages, 4)
            self.assertEqual(comparison.after.max_live_pages, 4)


class CompareFailureTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.before, self.after = build_pair_archives(self.tmp)
        self.out = self.tmp / "report.json"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _page_path(self, archive: Path, column: str, page_id: str) -> Path:
        manifest = json.loads((archive / "manifest.json").read_text("utf-8"))
        for p in manifest["columns"][column]["pages"]:
            if p["page_id"] == page_id:
                return archive / p["path"]
        raise KeyError(page_id)

    def _assert_failure_preserves_output(self, archive: Path,
                                         column: str, page_id: str,
                                         mutate) -> None:
        mutate(self._page_path(archive, column, page_id))
        self.out.write_text("PREVIOUS REPORT", encoding="utf-8")
        with self.assertRaises(ArchiveError):
            write_comparison_report(self.before, self.after, self.out)
        self.assertEqual(self.out.read_text("utf-8"), "PREVIOUS REPORT")
        self.out.unlink()
        with self.assertRaises(ArchiveError):
            write_comparison_report(self.before, self.after, self.out)
        self.assertFalse(self.out.exists(), "失败时不得创建目标报告")
        self.assertEqual([p.name for p in self.tmp.glob(".*.tmp")], [])

    def test_checksum_mismatch_before(self) -> None:
        self._assert_failure_preserves_output(
            self.before, "inspection_detail", "p0",
            lambda p: p.write_bytes(p.read_bytes() + b" "))

    def test_missing_page_after(self) -> None:
        self._assert_failure_preserves_output(
            self.after, "component_remark", "p0", lambda p: p.unlink())

    def test_malformed_json_page(self) -> None:
        self._assert_failure_preserves_output(
            self.after, "inspection_result", "p0",
            lambda p: p.write_bytes(b"{not json"))

    def test_nonstandard_json_constant_rejected(self) -> None:
        # NaN 在 Python json 里可解析，但不是合法 JSON 值，必须按页不可解释失败。
        archive = self.after
        path = self._page_path(archive, "inspection_result", "p0")
        data = json.loads(path.read_text("utf-8"))
        text = json.dumps(data, ensure_ascii=False)
        text = text.replace('"合格"', "NaN", 1)
        path.write_bytes(text.encode("utf-8"))
        manifest_path = archive / "manifest.json"
        manifest = json.loads(manifest_path.read_text("utf-8"))
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        for p in manifest["columns"]["inspection_result"]["pages"]:
            if p["page_id"] == "p0":
                p["sha256"] = digest
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False),
                                 encoding="utf-8")
        with self.assertRaises(ArchiveError):
            write_comparison_report(self.before, self.after, self.out)
        self.assertFalse(self.out.exists())

    def test_page_header_mismatch(self) -> None:
        # 保持校验和对齐，只改页头，确保失败原因不是校验和。
        archive = self.after
        path = self._page_path(archive, "inspection_category", "p0")
        data = json.loads(path.read_text("utf-8"))
        data["slot_end"] = data["slot_end"] + 1
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        manifest_path = archive / "manifest.json"
        manifest = json.loads(manifest_path.read_text("utf-8"))
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        for p in manifest["columns"]["inspection_category"]["pages"]:
            if p["page_id"] == "p0":
                p["sha256"] = digest
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False),
                                 encoding="utf-8")
        with self.assertRaises(ArchiveError):
            write_comparison_report(self.before, self.after, self.out)
        self.assertFalse(self.out.exists())

    def test_structure_mismatch_no_report(self) -> None:
        b = ArchiveBuilder()
        b.add_equipment()
        extra = b.build(self.tmp / "different_structure")
        with self.assertRaises(ArchiveError):
            write_comparison_report(self.before, extra, self.out)
        self.assertFalse(self.out.exists())

    def test_structure_mismatch_detected_before_any_page_read(self) -> None:
        b = ArchiveBuilder()
        e = b.add_equipment()
        c = b.add_component(e)
        b.add_inspection(c, "a", "b", "d")
        extra = b.build(self.tmp / "different_structure2")
        with self.assertRaises(ArchiveError) as ctx:
            compare_archives(self.before, extra)
        self.assertIn("结构边界不一致", str(ctx.exception))

    def test_output_inside_archive_rejected(self) -> None:
        with self.assertRaises(ValueError):
            write_comparison_report(self.before, self.after,
                                    self.before / "report.json")
        self.assertFalse((self.before / "report.json").exists())

    def test_inputs_not_modified(self) -> None:
        before_hash = _hash_tree(self.before)
        after_hash = _hash_tree(self.after)
        write_comparison_report(self.before, self.after, self.out)
        self.assertEqual(_hash_tree(self.before), before_hash)
        self.assertEqual(_hash_tree(self.after), after_hash)


class CompareCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.before, self.after = build_pair_archives(self.tmp)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_cli_writes_valid_report(self) -> None:
        out = self.tmp / "report.json"
        code = cli_main(["compare", str(self.before), str(self.after),
                         "--out", str(out)])
        self.assertEqual(code, 0)
        report = json.loads(out.read_text("utf-8"))
        self.assertEqual(report["format"], "qc-archive-diff")
        self.assertEqual(report["version"], 1)
        self.assertEqual(report["summary"]["total_changes"], 6)
        self.assertEqual(len(report["differences"]), 6)

    def test_cli_stdout(self) -> None:
        import contextlib
        import io
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = cli_main(["compare", str(self.before), str(self.after)])
        self.assertEqual(code, 0)
        report = json.loads(buffer.getvalue())
        self.assertEqual(report["summary"]["total_changes"], 6)

    def test_cli_failure_exit_code_and_no_output(self) -> None:
        page = self.after / "pages" / "inspection_detail" / "p0.json"
        page.write_bytes(page.read_bytes() + b"x")
        out = self.tmp / "report.json"
        code = cli_main(["compare", str(self.before), str(self.after),
                         "--out", str(out)])
        self.assertEqual(code, 2)
        self.assertFalse(out.exists())

    def test_query_behavior_unchanged(self) -> None:
        # 新增 compare 不改变原有 query 入口的命中语义与退出码。
        out = self.tmp / "query.json"
        code = cli_main(["query", str(self.before), "--category", "耐压试验",
                         "--result", "不合格", "--format", "json",
                         "--out", str(out)])
        self.assertEqual(code, 0)
        report = json.loads(out.read_text("utf-8"))
        self.assertEqual(report["archive_error"], None)
        self.assertEqual([h["inspection_slot"] for h in report["hits"]],
                         [2, 4, 6, 7])


if __name__ == "__main__":
    unittest.main(verbosity=2)
