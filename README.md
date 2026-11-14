# 薪酬数据字段溯源 API（Salary Column Lineage API）

薪酬数据工程师在对外导出部门薪酬数据前，用本服务静态分析导出 SQL 依赖了
哪些**敏感字段**，从而修改导出逻辑。服务基于 **FastAPI + sqlglot**，
**不连接任何业务数据库**，只对提交的 SQL 和元数据做静态血缘分析。

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

## 目录结构

```
app/
  main.py        FastAPI 路由
  analyzer.py    sqlglot 列级血缘分析器（作用域/三态传播/问题定位）
  models.py      Pydantic 请求响应模型
  samples.py     虚构薪酬表结构与样例 SQL
tests/           分析器与 API 测试（48 个用例）
Dockerfile
docker-compose.yml
requirements.txt
```
