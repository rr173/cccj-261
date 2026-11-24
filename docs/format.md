# 简化设备检查列档格式规范（qc-columnar-archive v1）

本格式是本工具唯一支持的公开说明的简化列档，不兼容任何第三方文件格式。
全部文件为 UTF-8 JSON 文本，档案是一个目录，结构如下：

```
<archive_dir>/
├── manifest.json        # 列清单：每列分页覆盖的逻辑槽范围、页文件校验值
├── structure.json       # 两层父子边界（设备→部件、部件→检查）
└── pages/
    ├── inspection_category/   # 检查类别列（谓词列）
    ├── inspection_result/     # 检查结果列（谓词列）
    ├── inspection_detail/     # 检查明细列（投影列，检查级）
    └── component_remark/      # 部件备注列（投影列，部件级）
        ├── p0.json
        ├── p1.json
        └── ...
```

## 1. 逻辑槽与槽身份

档案按原录入顺序包含三层实体，各自占据一层连续的**逻辑槽空间**，槽号从 0 开始：

| 层级 | 槽空间 | 说明 |
|---|---|---|
| 设备 equipment | `[0, E)` | 一台设备一个槽 |
| 部件 component | `[0, C)` | 一个部件一个槽，槽顺序即全档案录入顺序 |
| 检查 inspection | `[0, I)` | 一次检查一个槽，槽顺序即全档案录入顺序 |

**列槽身份完全由结构位置（槽号）决定，不按“第几个非空值”决定。**
取值页的 `values` 数组与槽号一一对应；空槽在数组中显式写 `null`，
读取时不得跳过空槽去邻槽取内容。允许设备没有部件、部件没有检查（空列表）。

## 2. structure.json —— 两层父子边界

```json
{
  "component_ranges":  [[0, 2], [2, 2], [2, 5]],
  "inspection_ranges": [[0, 3], [3, 3], [3, 9], [9, 9], [9, 10]]
}
```

- `component_ranges`：长度等于设备槽数 E，第 `e` 项是设备 `e` 所含部件槽的
  **半开区间** `[start, end)`；空列表设备满足 `start == end`。
- `inspection_ranges`：长度等于部件槽数 C，第 `c` 项是部件 `c` 所含检查槽的
  半开区间；空列表部件满足 `start == end`。
- 两组区间都必须从 0 开始、首尾相接、恰好覆盖 `[0, C)` 或 `[0, I)`，
  否则档案结构无法解释，按档案错误处理。

## 3. manifest.json —— 列与分页覆盖

```json
{
  "format": "qc-columnar-archive",
  "version": 1,
  "logical_slot_counts": {"equipment": 2, "component": 5, "inspection": 10},
  "columns": {
    "inspection_category": {
      "level": "inspection",
      "kind": "predicate",
      "nullable": true,
      "pages": [
        {"page_id": "p0", "path": "pages/inspection_category/p0.json",
         "slot_start": 0, "slot_end": 6, "sha256": "<hex>"},
        {"page_id": "p1", "path": "pages/inspection_category/p1.json",
         "slot_start": 6, "slot_end": 10, "sha256": "<hex>"}
      ]
    },
    "inspection_result":   {"level": "inspection", "kind": "predicate",   ...},
    "inspection_detail":   {"level": "inspection", "kind": "projection", ...},
    "component_remark":     {"level": "component",  "kind": "projection", ...}
  }
}
```

固定四列，缺列或出现未知列均为档案错误：

| 列名 | 层级 | 角色 |
|---|---|---|
| `inspection_category` | inspection | 谓词列：检查类别 |
| `inspection_result` | inspection | 谓词列：检查结果 |
| `inspection_detail` | inspection | 投影列：检查明细 |
| `component_remark` | component | 投影列：部件备注 |

- 每列的页按 `slot_start` 升序排列，半开区间首尾相接、无重叠无缺口，
  恰好覆盖该层级的整个槽空间；槽空间为空时页列表为空。
- **各列分页互相独立，可以不同**（页大小、分界均可不同）。
- 页路径必须是档案目录内的相对路径，禁止绝对路径或包含 `..`。
- `sha256` 是页文件原始字节的 SHA-256 十六进制摘要。
- 结构信息（structure.json）与取值页分开存放，可独立读取。

## 4. 取值页文件

```json
{
  "page_id": "p0",
  "column": "inspection_category",
  "slot_start": 0,
  "slot_end": 6,
  "values": ["外观检查", "耐压试验", null, "绝缘测试", "耐压试验", "耐压试验"]
}
```

- `values` 长度必须等于 `slot_end - slot_start`，第 k 个元素对应逻辑槽
  `slot_start + k`；`null` 表示该槽为空值。
- 页头的 `column / slot_start / slot_end / page_id` 必须与 manifest 登记一致。
- 字节摘要不符、JSON 无法解析、结构不符均视为该页损坏。

## 5. 查询语义

查询输入是一个**非空类别字面值**和一个**非空结果字面值**。

1. 仅当同一次检查（同一个 inspection 槽）的类别列与结果列分别等于两个字面值时
   产生一个命中；同一部件内两次不同检查各满足一列**不得**凑成命中。
2. 空值（null）不等于任何字面值，不可能命中。
3. 输出按原设备 → 部件 → 检查顺序排列，每行包含：
   - 定位坐标：设备序号、设备内部件序号、部件内检查序号（均从 1 开始），
     以及三个层级的绝对槽号；
   - 该次检查的类别、结果、检查明细；
   - 其所属部件的部件备注。
4. 投影列空值原样保留为 `null`，不得用邻槽内容顶替。

## 6. 读取规则（离线查询入口必须遵守）

- 允许读取全部必要的**结构**与**谓词**数据；谓词列按页流式扫描，
  任一时刻只保留当前页，不物化整棵树。
- 投影载荷页（明细、备注）只有在同时满足以下条件时才允许打开：
  该页覆盖至少一个命中检查槽（检查级列），或覆盖至少一个命中检查所属部件的
  部件槽（部件级列）。既不含选中检查槽、也不含其所属部件槽的投影页
  **一次查询中不得打开**；未命中页不做预检。
- 内存占用只随命中数与当前打开页数增长，不随档案全部明细载荷总量增长。
- 每次实际读页都必须留下可核查记录：阶段（predicate/projection）、列、页 ID、
  路径、覆盖槽范围、打开原因、状态（ok/各类错误）、字节数。
- 已选槽需要的载荷页缺失或损坏时：受影响命中标记为 `undeliverable` 并在
  `missing_pages` 中定位该页；不涉及该页的命中仍可交付。
- 结构文件、清单或谓词页缺失/损坏导致查询无法正确进行时，整体报
  **档案错误（archive_error）**，不交付命中。
