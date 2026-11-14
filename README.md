# 薪酬数据服务 API（字段溯源 + 工资单修订对账）

本服务包含两个相互独立的能力，均基于 **FastAPI**，**不连接任何业务数据库**：

1. **薪酬数据字段溯源**（FastAPI + sqlglot）：静态分析导出 SQL 依赖了哪些敏感字段；
2. **工资单修订对账**：月度更正明细入仓前，按工资单修订链重算各部门应发总额并与申报汇总核对，
   隔离存在序号缺口/内容冲突的工资单，防止冲突工资单被漏算后仍误判对平。

启动后访问 http://localhost:8000/docs ，可在 Swagger UI 中直接操作两类接口。

---

# 一、字段溯源 API

薪酬数据工程师在对外导出部门薪酬数据前，用本服务静态分析导出 SQL 依赖了
哪些**敏感字段**，从而修改导出逻辑。

## 能力范围

| 能力 | 说明 |
| --- | --- |
| 非递归 CTE | 支持 `WITH ... AS`，未被引用的 CTE 不参与分析；`RECURSIVE` 标记为不支持 |
| 派生表 | 子查询作为关系，来源与依赖可穿过任意层派生查询到达最终结果 |
| JOIN | INNER / LEFT / RIGHT / FULL / CROSS，输出 `JOIN ON` 联接依赖 |
| 星号 | 裸 `*` 与 `t.*` 按元数据展开，派生表内的星号同样可追溯 |
| UNION ALL | 按**列位置**合并两侧来源，输出名取左侧；`UNION`（去重）降级标记不完整 |
| 聚合 | SUM / COUNT / AVG（及 MIN/MAX），`COUNT(*)` 标注为无列依赖的行标记 |
| 表达式 | CASE / CAST / 算术 / `md5()` 哈希等均**不解除**敏感标记 |
| 子句依赖 | WHERE / JOIN ON / GROUP BY / HAVING / ORDER BY 依赖单独列出 |
| 别名作用域 | 别名严格按作用域解释，嵌套同名别名互不遮蔽错误 |

## 三态敏感标记（关键语义）

每个来源与每个输出列的 `sensitive` 是三态：

- `true`：至少一个物理来源列被显式标记为敏感；
- `false`：所有物理来源都明确存在且不敏感；
- `null`：存在**无法解析**的来源（元数据缺失、列不存在、歧义等）。

> **未知（null）绝不等于无敏感来源。** 只要分析中出现任何问题，响应的
> `incomplete` 即为 `true`，并在 `issues` 中给出问题代码与定位（行/列或 SQL 片段）。

哈希、聚合、类型转换等任何表达式变换都不会把敏感标记"洗掉"——标记只随
物理来源传播。

## 快速开始（Docker Compose）

```bash
docker compose up --build
# 打开浏览器访问
#   http://localhost:8000/docs
```

在 Swagger UI 中可直接：

1. `GET /api/v1/samples/tables` 查看虚构薪酬表结构与敏感标记；
2. `GET /api/v1/samples/sql/nested` 查看嵌套同名别名样例 SQL；
3. `POST /api/v1/samples/lineage/nested` 一键运行样例；
4. 或在 `POST /api/v1/lineage` 中提交自己的 SQL 与表元数据。

本地无 Docker 时：

```bash
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## 请求格式

`POST /api/v1/lineage`

```json
{
  "sql": "SELECT e.employee_name, SUM(s.base_salary) AS total FROM pay_salary s JOIN hr_employee e ON s.employee_id = e.employee_id GROUP BY e.employee_name",
  "tables": {
    "pay_salary": {
      "columns": [
        {"name": "employee_id", "sensitive": false},
        {"name": "base_salary", "sensitive": true, "comment": "基本薪资"}
      ]
    },
    "hr_employee": {
      "columns": [
        {"name": "employee_id", "sensitive": false},
        {"name": "employee_name", "sensitive": true, "comment": "姓名"}
      ]
    }
  },
  "dialect": "postgres"
}
```

## 响应结构（节选）

```json
{
  "incomplete": false,
  "output_columns": [
    {
      "ordinal": 2,
      "output_name": "total",
      "expression": "total",
      "sensitive": true,
      "sources": [
        {
          "table": "pay_salary",
          "column": "base_salary",
          "sensitive": true,
          "path": [
            {"relation": "s", "column": "base_salary", "kind": "table"}
          ],
          "unresolved_reason": null
        }
      ],
      "intermediate_path": [
        {"relation": "s", "column": "base_salary", "kind": "table"}
      ]
    }
  ],
  "dependencies": [
    {"clause": "join_on", "expression": "s.employee_id = e.employee_id", "sensitive": false, "sources": [ ... ]},
    {"clause": "group_by", "expression": "e.employee_name", "sensitive": true, "sources": [ ... ]}
  ],
  "ctes": [],
  "unused_ctes": [],
  "issues": [],
  "supported_syntax": [ ... ]
}
```

`path` 按穿过顺序记录每一跳（物理表 → 各层派生表/CTE → 当前引用），
`kind` 取值 `table` / `derived` / `cte`。

## 问题代码（issues[].code）

| code | 含义 |
| --- | --- |
| `parse_error` | SQL 无法解析（含行列定位） |
| `unsupported_statement` / `unsupported_query` | 非 SELECT 或不支持的查询体 |
| `recursive_cte_unsupported` | 递归 CTE 不在支持范围 |
| `set_op_unsupported` | INTERSECT / EXCEPT 等不支持的集合操作 |
| `union_distinct_degraded` | UNION 去重不在完整支持范围，已降级并标记不完整 |
| `metadata_missing` | 表元数据缺失或列无法解析，来源标记为未知 |
| `ambiguous_column` | 未限定列名在多个关系中存在歧义，候选全部保留 |
| `unknown_relation` / `unknown_column` | 限定名别名不存在 / 派生表未暴露该列 |
| `subquery_in_expression_unsupported` | 标量子查询、IN 子查询等不支持 |
| `star_unresolvable` / `unknown_qualifier` | 星号无法展开 |
| `natural_join` | NATURAL JOIN 隐式列无法精确定位 |

## 虚构薪酬表

`app/samples.py` 内置 4 张虚构表：`hr_employee`（含身份证号、银行卡号等
敏感个人信息）、`pay_salary`（基本薪资/奖金/实发等敏感薪酬字段）、
`org_department`、`pay_salary_archive`，以及三个样例：

- `nested`：多层派生表 + 内外层同名别名 `s` + CTE + JOIN + 星号 + 聚合 +
  未引用 CTE；
- `union_all`：在线表与归档表 UNION ALL 的按位置合并；
- `incomplete`：元数据缺失、歧义列、标量子查询三类问题的不完整分析示范。

## 测试

```bash
pip install -r requirements.txt pytest httpx
pytest -q
```

---

# 二、工资单修订对账 API

薪酬数仓工程师在**月度更正明细入仓前**，用本接口核对各部门应发总额，
确保存在冲突的工资单不会被漏算后仍误判"对平"。所有计算在单次请求内存中完成，
不落库、无外部依赖。

## 对账规则（关键语义）

1. **条数上限**：单次提交同月最多 **500 条**工资单修订，超出返回 422。
2. **乱序与去重**：修订可乱序提交；五条字段（工资单号、修订序号、部门、金额、
   动作）完全相同的重传**只算一次**，响应中通过 `duplicate_revision_count` 披露丢弃条数。
3. **连续修订确定最终状态**：每张工资单的修订序号必须**从 1 开始且连续**，
   取最大序号的修订作为最终状态；`void`（作废）不产生金额，`post`（入账）
   金额计入其最终归属部门。
4. **序号缺口**：序号未从 1 开始或中间缺号（如只有 1、3）→ 标记 `sequence_gap`。
5. **同序号内容冲突**：同一工资单同一序号出现不同部门/金额/动作 → 标记
   `revision_conflict`（完全相同的重传不算冲突）。
6. **整单隔离**：命中缺口或冲突的工资单，其**历次修订涉及的全部部门**一律
   隔离（`quarantined`），这些部门**不能用剩余明细放行**，也不输出差额
   （差额无法确定），响应给出导致隔离的工资单号与原因。
7. **跨部门更正**：工资单从部门 A 改到部门 B 时，A、B 两边都按**最终状态**
   核对——A 不再承担该单金额，B 承担最终金额；若最终动作为作废则两边都不计。
8. **缺少汇总不得放行**：修订中出现、但未申报应发总额的部门标记为
   `missing_total`，即使重算金额自洽也不放行。
9. **差额口径**：`variance_cents = 申报应发总额 - 重算应发总额`（单位：分），
   仅对明细完整的部门输出；隔离或缺汇总部门为 `null`。

## 快速体验

在 Swagger UI（`/docs`）中：

- `GET /api/v1/samples/reconcile/payload` 查看覆盖全部场景的样例请求体；
- `POST /api/v1/samples/reconcile` 一键运行样例；
- 或在 `POST /api/v1/payroll/reconcile` 中提交自己的数据。

## 请求格式

`POST /api/v1/payroll/reconcile`

```json
{
  "pay_month": "2026-10",
  "revisions": [
    {"payslip_id": "PS-001", "revision_no": 1, "department": "D_A", "amount_cents": 100000, "action": "post"},
    {"payslip_id": "PS-001", "revision_no": 2, "department": "D_B", "amount_cents": 100000, "action": "post"},
    {"payslip_id": "PS-002", "revision_no": 1, "department": "D_A", "amount_cents": 80000,  "action": "post"},
    {"payslip_id": "PS-002", "revision_no": 2, "department": "D_A", "amount_cents": 80000,  "action": "void"}
  ],
  "department_totals": [
    {"department": "D_A", "expected_total_cents": 100000},
    {"department": "D_B", "expected_total_cents": 100000}
  ]
}
```

字段约束：`revision_no >= 1`；`amount_cents` 为非负整数（分）；
`action` 仅允许 `post` / `void`；`revisions` 至少 1 条、至多 500 条；
同一部门在 `department_totals` 中重复申报返回 422。

## 响应结构（节选）

```json
{
  "pay_month": "2026-10",
  "revision_count": 4,
  "raw_revision_count": 4,
  "duplicate_revision_count": 0,
  "passed_departments": ["D_A", "D_B"],
  "warehouse_details": [
    {"payslip_id": "PS-001", "revision_no": 2, "department": "D_B", "amount_cents": 100000, "action": "post"}
  ],
  "departments": [
    {
      "department": "D_A",
      "status": "matched",
      "computed_total_cents": 100000,
      "expected_total_cents": 100000,
      "variance_cents": 0,
      "reason": null,
      "detail_count": 1,
      "quarantined_payslip_ids": []
    }
  ],
  "quarantined_payslips": [],
  "all_passed": true
}
```

### 部门状态（`departments[].status`）

| status | 含义 | 是否放行 |
| --- | --- | --- |
| `matched` | 明细完整且重算总额与申报一致（含申报 0、无修订的部门） | 是 |
| `mismatched` | 明细完整但总额不平，`variance_cents` 给出差额 | 否 |
| `quarantined` | 关联工资单存在序号缺口/同序号冲突，差额无法确定 | 否 |
| `missing_total` | 修订涉及该部门但未申报应发总额 | 否 |

### 隔离工资单（`quarantined_payslips[]`）

```json
{
  "payslip_id": "PS-004",
  "reasons": ["sequence_gap"],
  "affected_departments": ["D_A", "D_B"],
  "observed_revision_nos": [1, 3]
}
```

- `reasons` 可同时包含 `sequence_gap` 与 `revision_conflict`；
- `affected_departments` 是该单**历次修订出现过的全部部门**，均被隔离；
- `warehouse_details` 只包含**核对通过部门**的最终入账单（作废单与隔离/缺汇总部门的明细均不入仓）。

### 内置样例覆盖的场景

`build_sample_reconcile_request`（见 `app/samples.py`）一次覆盖：
同部门金额更正、入账后作废、跨部门更正、序号缺口隔离、同序号冲突隔离、
完全相同重传去重、缺少汇总部门、零汇总无修订部门。

## 目录结构

```
app/
  main.py          FastAPI 路由（溯源 + 对账 + 样例）
  analyzer.py      sqlglot 列级血缘分析器（作用域/三态传播/问题定位）
  models.py        溯源请求/响应 Pydantic 模型
  recon_models.py  对账请求/响应 Pydantic 模型（含 500 条上限等约束）
  reconcile.py     对账引擎（去重/连续修订/缺口冲突隔离/跨部门/差额）
  samples.py       虚构薪酬表结构、溯源样例 SQL、对账样例请求
tests/             分析器、对账引擎与 API 测试（80+ 用例）
Dockerfile
docker-compose.yml
requirements.txt
```

## Docker Compose 启动

```bash
docker compose up --build
# 打开浏览器访问
#   http://localhost:8000/docs
```

本地无 Docker 时：

```bash
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```
