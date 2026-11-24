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
  compare.py              # 两份快照的槽对齐对比 + 原子写出 JSON 差异报告
  builder.py              # 建库器（测试/演示用）
  cli.py                  # 命令行实现
scripts/
  run_query.py            # 查询/对比入口（支持 python -I 直接运行）
  make_demo_archives.py   # 生成正常/损坏两个演示档案
tests/test_query.py       # 17 项查询端到端测试
tests/test_compare.py     # 20 项对比端到端测试
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

# 对比同一设备档案的前后两次快照，生成 JSON 差异报告
python -I scripts/run_query.py compare demo_data/good_archive demo_data/good_archive \
    --out diff_report.json
```

退出码（query）：`0` 正常（含部分不可交付）；`2` 档案错误（结构/清单/谓词页不可用，
无命中交付）；`3` 查询参数错误。

退出码（compare）：`0` 对比完成并写出报告；`2` 对比失败（结构不一致或任一页
不可读/校验失败/无法解释，未创建或替换报告）；`3` 参数错误（如把报告写进输入档案）。

## 对比语义与实现

| 要求 | 实现位置 |
|---|---|
| 两份档案结构边界必须完全一致 | `compare.Comparison._check_structures`，不一致直接 `ArchiveError`，不读载荷、不写报告 |
| 分页边界可不同，按逻辑槽对齐 | 每列一个 `_ColumnCursor`，各自按 manifest 分页单调翻页 |
| `null` 按实际值比较，类型敏感 | `json_values_equal`：`null==null` 不变；`1` 与 `true` 视为不同 |
| 检查三项任一变化才报告，给前后值 | `differences()` 逐槽比较类别/结果/明细，附 `changed_fields` |
| 部件备注变化单独报告，不重复其下未变检查 | 先按部件输出备注差异，再输出该部件内变化的检查 |
| 设备→部件、备注先于检查、检查按槽序 | 单一顺序生成器，不排序、不缓存 |
| 全部页都要读并核验 | 四列 cursor 在槽空间中走到底；报告 `summary.pages_verified` 给出各档案各列实读页数 |
| 任一页失败整体失败，不交付部分差异 | 校验和/JSON/页头/values 长度任一不符即 `ArchiveError`；报告先写临时文件，成功后才 `os.replace` |
| 内存不随明细总量增长 | 边读边比边写，每档案每列最多驻留 1 页；差异直接流式写入临时文件 |
| 输入档案只读、查询行为不变 | 对比只读打开；输出路径落在输入档案内直接拒绝；`query` 代码未改动 |

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
python -I tests/test_compare.py
```

覆盖：命中顺序与坐标、跨检查防凑、空谓词不命中、投影空值保留、空列表实体、
各列独立分页下的投影剪枝、驻留页数有界、闸门拦截、校验和损坏/文件缺失的
部分交付隔离、谓词页损坏的档案错误、结构区间断裂/槽数不符/路径逃逸等。
