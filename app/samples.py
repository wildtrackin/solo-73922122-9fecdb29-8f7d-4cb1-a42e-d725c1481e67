"""虚构薪酬部门表结构（演示用，不连接任何真实业务库）。"""

from __future__ import annotations

from .models import AnalyzeRequest, SourceColumn, TableMetadata

# --------------------------------------------------------------------------- #
# 虚构薪酬表结构
# --------------------------------------------------------------------------- #

SAMPLE_TABLES: dict[str, TableMetadata] = {
    "hr_employee": TableMetadata(
        columns=[
            SourceColumn(name="employee_id", sensitive=False, comment="员工ID"),
            SourceColumn(name="dept_id", sensitive=False, comment="部门ID"),
            SourceColumn(name="employee_name", sensitive=True, comment="员工姓名（个人信息）"),
            SourceColumn(name="id_card_no", sensitive=True, comment="身份证号（高度敏感）"),
            SourceColumn(name="bank_card_no", sensitive=True, comment="银行卡号（高度敏感）"),
            SourceColumn(name="hire_date", sensitive=False, comment="入职日期"),
            SourceColumn(name="employment_status", sensitive=False, comment="在职状态"),
        ]
    ),
    "pay_salary": TableMetadata(
        columns=[
            SourceColumn(name="salary_id", sensitive=False),
            SourceColumn(name="employee_id", sensitive=False, comment="员工ID"),
            SourceColumn(name="pay_month", sensitive=False, comment="发薪月份"),
            SourceColumn(name="base_salary", sensitive=True, comment="基本薪资（敏感）"),
            SourceColumn(name="bonus", sensitive=True, comment="奖金（敏感）"),
            SourceColumn(name="allowance", sensitive=True, comment="津贴（敏感）"),
            SourceColumn(name="social_deduction", sensitive=False, comment="社保代扣"),
            SourceColumn(name="tax_deduction", sensitive=False, comment="个税代扣"),
            SourceColumn(name="net_pay", sensitive=True, comment="实发薪资（敏感）"),
        ]
    ),
    "org_department": TableMetadata(
        columns=[
            SourceColumn(name="dept_id", sensitive=False, comment="部门ID"),
            SourceColumn(name="dept_name", sensitive=False, comment="部门名称"),
            SourceColumn(name="cost_center", sensitive=False, comment="成本中心"),
            SourceColumn(name="manager_id", sensitive=False, comment="部门负责人ID"),
        ]
    ),
    "pay_salary_archive": TableMetadata(
        columns=[
            SourceColumn(name="employee_id", sensitive=False),
            SourceColumn(name="pay_month", sensitive=False),
            SourceColumn(name="base_salary", sensitive=True, comment="归档基本薪资（敏感）"),
            SourceColumn(name="bonus", sensitive=True, comment="归档奖金（敏感）"),
            SourceColumn(name="net_pay", sensitive=True, comment="归档实发薪资（敏感）"),
        ]
    ),
}

# --------------------------------------------------------------------------- #
# 样例 SQL
# --------------------------------------------------------------------------- #

SAMPLE_SQL_NESTED_ALIASES = """\
-- 嵌套派生表 + 同名别名 + 非递归 CTE + JOIN + 聚合 + 星号
WITH dept_emp AS (
    SELECT
        e.employee_id,
        e.dept_id,
        e.employee_name,
        e.id_card_no
    FROM hr_employee e
    WHERE e.employment_status = 'ACTIVE'
),
unreferenced_cte AS (
    -- 该 CTE 定义后未被引用，不应参与分析
    SELECT employee_id, bank_card_no FROM hr_employee
)
SELECT
    d.dept_name,
    s.employee_id,
    s.employee_name,
    s.monthly_total,
    SUM(s.monthly_total) AS dept_payroll_sum,
    COUNT(*)                 AS headcount,
    AVG(s.monthly_total)      AS avg_monthly_pay
FROM (
    -- 内层派生表也叫 s，与外层别名同名，按各自作用域解释
    SELECT
        s.employee_id,
        s.pay_month,
        s.base_salary + s.bonus + s.allowance AS monthly_total,
        s_emp.employee_name
    FROM (
        SELECT * FROM pay_salary
    ) s
    JOIN (
        -- 再嵌一层同名别名 s：别名只在各自作用域内有效
        SELECT employee_id, employee_name FROM hr_employee
    ) s_emp ON s.employee_id = s_emp.employee_id
) s
INNER JOIN dept_emp e ON s.employee_id = e.employee_id
LEFT JOIN org_department d ON e.dept_id = d.dept_id
WHERE s.monthly_total > 0
GROUP BY d.dept_name, s.employee_id, s.employee_name
HAVING SUM(s.monthly_total) > 10000
ORDER BY dept_payroll_sum DESC;
"""

SAMPLE_SQL_UNION_ALL = """\
-- UNION ALL：按列位置合并在线表与归档表来源
SELECT employee_id, pay_month, base_salary, net_pay
FROM pay_salary
UNION ALL
SELECT employee_id, pay_month, base_salary, net_pay
FROM pay_salary_archive;
"""

SAMPLE_SQL_INCOMPLETE = """\
-- 故意制造三类问题：
--   1) ext_contractor 表未提供元数据（metadata_missing）
--   2) employee_id 未限定且在两张表中都存在（ambiguous_column）
--   3) 标量子查询不在支持范围内（subquery_in_expression_unsupported）
SELECT
    e.employee_id,
    e.employee_name,
    c.contractor_code,
    (SELECT max(pay_month) FROM pay_salary) AS latest_month
FROM hr_employee e
JOIN ext_contractor c ON e.employee_id = c.employee_id
WHERE employee_id > 0;
"""


def build_sample_request(kind: str = "nested") -> AnalyzeRequest:
    sql = {
        "nested": SAMPLE_SQL_NESTED_ALIASES,
        "union_all": SAMPLE_SQL_UNION_ALL,
        "incomplete": SAMPLE_SQL_INCOMPLETE,
    }[kind]
    return AnalyzeRequest(sql=sql, tables=SAMPLE_TABLES)
