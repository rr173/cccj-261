"""生成两个演示档案：正常档案与含损坏投影页的档案。

运行：python -I scripts/make_demo_archives.py <输出目录>
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from qcarchive import ArchiveBuilder  # noqa: E402

PAGE_SIZES = {
    "inspection_category": 3,
    "inspection_result": 5,
    "inspection_detail": 2,
    "component_remark": 1,
}


def build_good(root: Path, name: str = "good_archive") -> Path:
    b = ArchiveBuilder()

    e0 = b.add_equipment()  # 设备槽 0：1号机组
    cA = b.add_component(e0, remark="主泵-上次检修后振动偏高")
    b.add_inspection(cA, "耐压试验", "合格", "壳体保压30分钟无压降")
    b.add_inspection(cA, "外观检查", "不合格", "法兰面存在锈蚀面积约12cm2")
    b.add_inspection(cA, "耐压试验", "不合格", "保压18分钟压力下降0.4MPa")
    b.add_inspection(cA, "耐压试验", None, "记录表漏填结果")
    cB = b.add_component(e0, remark=None)  # 备注空值，须保留 null
    b.add_inspection(cB, "耐压试验", "不合格", "接头处可见渗漏")

    e1 = b.add_equipment()  # 设备槽 1：2号机组
    cC = b.add_component(e1, remark="换热器-备用列")
    b.add_inspection(cC, "绝缘测试", "合格", "绝缘电阻1200MΩ")
    b.add_inspection(cC, "耐压试验", "不合格", None)  # 明细空值须保留
    b.add_component(e1, remark="空部件-待安装")       # 空部件，无检查

    b.add_equipment()  # 设备槽 2：3号机组（空设备，无部件）
    return b.build(root / name, page_sizes=PAGE_SIZES)


def build_corrupt(root: Path) -> Path:
    arc = build_good(root, name="corrupt_archive")
    # 损坏明细页 p1（槽 [2,4)，含命中槽 2），并删除备注页 p2（部件槽 2，含命中槽 6）
    detail = arc / "pages" / "inspection_detail" / "p1.json"
    detail.write_bytes(detail.read_bytes() + b"\n<<tampered>>")
    (arc / "pages" / "component_remark" / "p2.json").unlink()
    return arc


def main() -> int:
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "demo_data")
    out.mkdir(parents=True, exist_ok=True)
    good = build_good(out)
    corrupt = build_corrupt(out)
    print(f"正常档案: {good}")
    print(f"损坏档案: {corrupt}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
