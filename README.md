# 设备检查列档离线查询工具

针对“某类检查里判为某结果”的复核取数需求，提供一个**只读的离线查询入口**。
只支持本仓库公开说明的一种简化列档格式（见 [`docs/format.md`](docs/format.md)），
不兼容第三方文件格式。

## 解决的问题

- 整台解包会读出大量无需披露的明细；本工具只打开真正服务于命中行的投影载荷页。
- 整台解包会把同一部件内两次不同检查（各满足一个谓词）凑成命中；本工具按
  **同一次检查（同一 inspection 槽）的两列同时满足**判定，杜绝跨检查凑命中。
- 列槽身份按结构位置决定，空值显式保留，不会跳到邻槽取内容。

## 目录布局

```
docs/format.md            # 列档格式 v1 完整规范（公开说明）
src/qcarchive/
  errors.py               # ArchiveError（档案错误）/ ProjectionPageError（单页不可交付）
  models.py               # PageRef / Structure / ReadRecord / Hit / QueryReport
  manifest.py             # 清单与结构加载、连续性/槽数/路径逃逸校验、SHA-256
  reader.py               # 带闸门读取器 + 流式谓词扫描 + 投影按需读取（查询引擎）
  builder.py              # 建库器（测试/演示用）
  cli.py                  # 命令行实现
scripts/
  run_query.py            # 查询入口（支持 python -I 直接运行）
  make_demo_archives.py   # 生成正常/损坏两个演示档案
tests/test_query.py       # 17 项端到端测试
```

## 使用

```bash
# 建演示档案（输出到独立数据目录，与代码目录分离）
python -I scripts/make_demo_archives.py demo_data

# 查询（文本报告，含完整读页记录）
python -I scripts/run_query.py query demo_data/good_archive \
    --category 耐压试验 --result 不合格

# JSON 报告（供下游系统消费）
python -I scripts/run_query.py query demo_data/good_archive \
    --category 耐压试验 --result 不合格 --format json --out report.json
```

退出码：`0` 正常（含部分不可交付）；`2` 档案错误（结构/清单/谓词页不可用，
无命中交付）；`3` 查询参数错误。

## 查询语义与读取纪律如何落地

| 要求 | 实现位置 |
|---|---|
| 类别、结果是同次检查的两列，同槽同时满足才命中 | `reader._scan_predicates` 双游标按槽对齐 |
| 空值保留、不跳邻槽 | 槽号即数组下标，`null` 显式存储，`PageHandle.get(slot)` |
| 允许读全部结构与谓词数据 | `structure.json`/`manifest.json` 全读；谓词页顺序流式扫描 |
| 不还原整棵树、内存不随明细总量增长 | 谓词每列同时只驻留 1 页；投影同页只开 1 次，读完即关；
  只保存命中列表（`max_live_page_count` 可核，测试断言有界） |
| 不打开不含已选槽的投影页 | `GatedPageReader._gate_reason` 闸门，违例直接 `PermissionError`，
  且不产生 I/O、不写读页记录 |
| 可核查的实际读页记录 | 每次读页一条 `ReadRecord`：阶段、列、页、槽范围、原因、
  状态、字节数、错误信息 |
| 投影页缺/损坏只影响相关命中 | 该页引用者标 `undeliverable` 并在 `missing_pages` 定位页；
  其他命中照常交付 |
| 未命中页不预检 | 闸门按已选槽集合决定打开集合，零命中时投影读取次数为 0 |
| 结构无法解释时报档案错误 | `manifest.py` 校验区间连续性、槽数一致、列集合、路径逃逸 |

## 测试

```bash
python -I tests/test_query.py
```

覆盖：命中顺序与坐标、跨检查防凑、空谓词不命中、投影空值保留、空列表实体、
各列独立分页下的投影剪枝、驻留页数有界、闸门拦截、校验和损坏/文件缺失的
部分交付隔离、谓词页损坏的档案错误、结构区间断裂/槽数不符/路径逃逸等。
