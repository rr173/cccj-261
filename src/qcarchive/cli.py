"""离线查询 / 对比命令行入口。

用法：
  python -m qcarchive.cli import <JSONL 输入目录> --out <档案目录>
  python -m qcarchive.cli query <档案目录> --category 耐压试验 --result 不合格
  python -m qcarchive.cli query <档案目录> ... --format json --out report.json
  python -m qcarchive.cli compare <前档案> <后档案> --out <report.json>
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .compare import write_comparison_report
from .errors import ArchiveError
from .reader import query_archive


def _render_text(report_dict: dict) -> str:
    lines: list[str] = []
    lines.append(f"档案：{report_dict['archive_path']}")
    lines.append(f"查询：类别={report_dict['category']!r} 且 结果={report_dict['result']!r}")
    if report_dict["archive_error"]:
        lines.append(f"档案错误（无命中交付）：{report_dict['archive_error']}")
        return "\n".join(lines)
    lines.append(
        f"命中 {len(report_dict['hits'])} 条"
        f"（可交付 {report_dict['delivered_count']}，"
        f"不可交付 {report_dict['undeliverable_count']}）")
    for i, h in enumerate(report_dict["hits"], 1):
        coord = (f"设备{h['equipment_ordinal']} / 部件{h['component_ordinal_in_equipment']}"
                 f" / 检查{h['inspection_ordinal_in_component']}"
                 f"（槽 e={h['equipment_slot']}, c={h['component_slot']}, "
                 f"i={h['inspection_slot']}）")
        lines.append(f"  [{i}] {coord}  状态={h['status']}")
        lines.append(f"      类别={h['category']!r}  结果={h['result']!r}")
        if h["status"] == "delivered":
            lines.append(f"      部件备注={h['component_remark']!r}")
            lines.append(f"      检查明细={h['detail']!r}")
        else:
            for mp in h["missing_pages"]:
                lines.append(
                    f"      不可交付载荷：列={mp['column']} 页={mp['page_id']} "
                    f"路径={mp['path']} 槽区间=[{mp['slot_start']},{mp['slot_end']}) "
                    f"原因={mp['reason']}")

    lines.append("")
    lines.append(f"实际载荷读页记录（共 {len(report_dict['payload_reads'])} 次）：")
    for r in report_dict["payload_reads"]:
        lines.append(
            f"  - {r['phase']:<10} 列={r['column']:<20} 页={r['page_id']} "
            f"槽=[{r['slot_start']},{r['slot_end']}) 状态={r['status']} "
            f"字节={r['bytes_read']}")
        lines.append(f"      原因：{r['reason']}")
        if r["error"]:
            lines.append(f"      错误：{r['error']}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="qcarchive", description="qc-columnar-archive 离线查询入口")
    sub = parser.add_subparsers(dest="cmd", required=True)
    q = sub.add_parser("query", help="按类别+结果查询同槽检查命中")
    q.add_argument("archive", help="档案目录")
    q.add_argument("--category", required=True, help="类别非空字面值")
    q.add_argument("--result", required=True, help="结果非空字面值")
    q.add_argument("--format", choices=("text", "json"), default="text")
    q.add_argument("--out", help="输出文件，缺省输出到 stdout")

    import_p = sub.add_parser(
        "import",
        help="把含 equipment/component/inspection 三份 JSONL 的目录建档为 v1 档案")
    import_p.add_argument("input_dir", help="包含三份 UTF-8 JSONL 的输入目录")
    import_p.add_argument("--out", required=True, help="生成的 qc-columnar-archive v1 目录")
    import_p.add_argument(
        "--page-size", type=int, default=None,
        help=argparse.SUPPRESS)
    import_p.add_argument(
        "--no-replace-existing", action="store_true",
        help=argparse.SUPPRESS)

    cmp_p = sub.add_parser(
        "compare", help="对比两份快照档案，生成 JSON 差异报告")
    cmp_p.add_argument("before", help="变更前的档案目录")
    cmp_p.add_argument("after", help="变更后的档案目录")
    cmp_p.add_argument("--out", help="JSON 报告路径；缺省输出到 stdout")

    args = parser.parse_args(argv)
    if args.cmd == "import":
        return _run_import(args)
    if args.cmd == "compare":
        return _run_compare(args)
    if args.cmd != "query":  # pragma: no cover
        parser.error("仅支持 import / query / compare 子命令")

    try:
        report = query_archive(Path(args.archive), args.category, args.result)
        report_dict = report.to_dict()
    except ArchiveError as exc:
        report_dict = {
            "category": args.category,
            "result": args.result,
            "archive_path": str(Path(args.archive)),
            "archive_error": str(exc),
            "delivered_count": 0,
            "undeliverable_count": 0,
            "hits": [],
            "payload_reads": [],
        }
        exit_code = 2
    except ValueError as exc:
        print(f"参数错误：{exc}", file=sys.stderr)
        return 3
    else:
        exit_code = 0

    text = (json.dumps(report_dict, ensure_ascii=False, indent=2)
            if args.format == "json" else _render_text(report_dict))
    if args.out:
        Path(args.out).write_text(text + "\n", encoding="utf-8")
    else:
        print(text)
    return exit_code


def _run_import(args: argparse.Namespace) -> int:
    """import 子命令：校验 JSONL 并原子生成 v1 档案。"""
    from .importer import (
        DEFAULT_PAGE_SIZE,
        ArchiveImportError,
        import_jsonl_archive,
    )

    try:
        import_jsonl_archive(
            args.input_dir,
            args.out,
            page_size=(DEFAULT_PAGE_SIZE if args.page_size is None
                       else args.page_size),
            replace_existing=not args.no_replace_existing,
        )
    except ArchiveImportError as exc:
        print(f"建档失败：{exc}", file=sys.stderr)
        return 2
    except ValueError as exc:
        print(f"参数错误：{exc}", file=sys.stderr)
        return 3
    except OSError as exc:
        print(f"建档输出失败：{exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # 防御：任何未预期错误也不输出 traceback 到 stdout
        print(f"建档失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    return 0


def _run_compare(args: argparse.Namespace) -> int:
    """compare 子命令：任何页不可用即退出码 2，且不创建/替换目标报告。"""
    try:
        write_comparison_report(args.before, args.after, args.out)
    except ValueError as exc:
        print(f"参数错误：{exc}", file=sys.stderr)
        return 3
    except ArchiveError as exc:
        print(f"档案错误（对比失败，未生成差异报告）：{exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"输出失败（对比失败，未生成差异报告）：{exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
