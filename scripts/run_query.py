#!/usr/bin/env python3
"""命令行入口脚本（支持 python -I 直接运行）。

用法：
  python -I scripts/run_query.py <档案目录> --category 耐压试验 --result 不合格
  python -I scripts/run_query.py <档案目录> ... --format json --out report.json
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from qcarchive.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
