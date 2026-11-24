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
  importer.py             # JSONL 离线建档入口（流式校验/归组/原子提交）
  vault.py                # 离线备份库：多命名版本、内容寻址去重、租约回收
  cli.py                  # 命令行实现
scripts/
  run_query.py            # 查询/对比/备份库入口（支持 python -I 直接运行）
  make_demo_archives.py   # 生成正常/损坏两个演示档案
tests/test_query.py       # 17 项查询端到端测试
tests/test_compare.py     # 20 项对比端到端测试
tests/test_vault.py       # 20 项备份库端到端测试
```

## 使用

```bash
# 从现场导出的三份 JSONL 离线建档
python -I scripts/run_query.py import <JSONL 输入目录> --out <档案目录>

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

# 备份库：多命名版本保存、恢复、删除（首次 save 自动创建库）
python -I scripts/run_query.py vault save <库目录> 2025-01 <档案目录>
python -I scripts/run_query.py vault restore <库目录> 2025-01 --out <新档案目录>
python -I scripts/run_query.py vault forget <库目录> 2025-01
```

`import` 的输入目录必须包含 `equipment.jsonl`、`component.jsonl` 和
`inspection.jsonl`。标识在各自文件内必须唯一，`component.equipment_id` 与
`inspection.component_id` 必须能解析到已有记录。导入按“设备行序 → 部件行序 →
检查行序”稳定归组：同一设备的部件按部件文件行号排列，同一部件的检查按检查文件
行号排列；因此部件改挂设备后，检查值仍跟随原部件，不会与其他记录错位。显式
`null` 原样写入列槽。

导入逐行流式校验，标识和行偏移使用临时 SQLite 计划库，列页按固定页数写出；
产物先写入目标旁的暂存目录并复用现有对比引擎校验全部页，成功后才提交。源数据
错误会指出文件名和行号，且不会在 `--out` 生成半成品；若 `--out` 已是档案，
校验/建档阶段失败时原档案保持不变。

退出码（query）：`0` 正常（含部分不可交付）；`2` 档案错误（结构/清单/谓词页不可用，
无命中交付）；`3` 查询参数错误。

退出码（import）：`0` 建档成功；`2` 输入数据、所属关系或输出/档案校验失败；
`3` 命令参数错误。

退出码（compare）：`0` 对比完成并写出报告；`2` 对比失败（结构不一致或任一页
不可读/校验失败/无法解释，未创建或替换报告）；`3` 参数错误（如把报告写进输入档案）。

退出码（vault）：`0` 保存/恢复/删除成功；`2` 档案校验失败、版本不存在或重名、
库内数据缺失/损坏、恢复目标已存在、I/O 失败；`3` 版本名等参数形态错误。

## 备份库（vault）

一个备份库目录可保留多个命名版本（月份）。首次 `save` 创建库骨架，之后重名
`save` 一律拒绝；`forget` 删除版本记录后允许同名重存。只保存 v1 档案的
`manifest.json`、`structure.json` 与清单登记的全部页，源档案旁的其他文件不收录。
保存前用现有清单/结构校验与逐页核验确认档案可恢复，校验不通过的档案不会成为
版本。

| 要求 | 实现位置 |
|---|---|
| 库内字节相同的载荷只留一份，与版本/相对路径无关 | `Vault._publish_staged`：对象名为内容 SHA-256；先流式写暂存文件再 `link`/`replace` 发布 |
| 同一路径的不同内容不互相替代 | 版本记录 `files: {相对路径: 对象摘要}`，恢复逐路径取对象 |
| 恢复得到原相对路径、原始字节、可独立使用的档案 | `Vault._restore_payloads`：按清单流式复制 + SHA-256 核对，再复用 compare 读取器整体校验 |
| 缺失/损坏库内数据不算成功、不留半档、不动已有目标 | 对象缺失/摘要不符/恢复自检失败即抛错；载荷写在目标旁暂存目录，成功后一次 `os.rename` |
| 备份/恢复不全载荷入内存 | 逐文件、逐块（1 MiB）流式复制；只保留版本清单元数据 |
| 删除一个版本不损坏其他版本 | 版本记录独立；`forget` 只删自己的记录 |
| 无引用载荷被回收并可复用 | `_collect_garbage`：删除既无版本引用也无租约引用的对象 |
| 在途恢复不因并发删除而失败 | 恢复开始时在库锁内写租约 `leases/lease-<id>/objects.json`，回收尊重租约 |
| 恢复期间其他进程可删除/重写版本 | 恢复只在锁内取版本快照与写租约，复制载荷在锁外进行 |
| 版本名删除后重用，恢复仍得到开始时选定版本 | 版本记录内容即快照；租约固定其对象摘要，新版本对象不被混入 |
| 已死进程的租约在下次成功 save/forget 时清理 | `owner.json` 记录 PID 与进程启动时刻，`_prune_dead_leases` 判断进程存活 |
| 中断后版本呈现操作前/后完整状态 | 版本记录 `os.replace` 原子提交；保存载荷全部落盘后才登记，残留仅为无引用对象 |

库布局：

```
<库目录>/
├── vault.json            # 格式标识
├── vault.lock            # 库级 flock（进程退出自动释放）
├── versions/v-<id>.json  # 版本记录：名称 + {相对路径: 内容摘要}
├── objects/<前2位>/<sha256>  # 去重后的文件载荷
├── leases/lease-<id>/    # 在途恢复的租约（owner.json + objects.json）
└── tmp/                  # 保存暂存区（带 owner.json）
```

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
python -I tests/test_vault.py
```

覆盖：命中顺序与坐标、跨检查防凑、空谓词不命中、投影空值保留、空列表实体、
各列独立分页下的投影剪枝、驻留页数有界、闸门拦截、校验和损坏/文件缺失的
部分交付隔离、谓词页损坏的档案错误、结构区间断裂/槽数不符/路径逃逸等。
