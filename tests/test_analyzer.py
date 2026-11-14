"""溯源分析器测试：覆盖需求中的全部能力与失败语义。"""

from __future__ import annotations

import pytest

from app.analyzer import LineageAnalyzer
from app.models import AnalyzeRequest, SourceColumn, TableMetadata


# --------------------------------------------------------------------------- #
# 测试用元数据
# --------------------------------------------------------------------------- #

TABLES = {
    "t_salary": TableMetadata(
        columns=[
            SourceColumn(name="emp_id", sensitive=False),
            SourceColumn(name="dept_id", sensitive=False),
            SourceColumn(name="base_salary", sensitive=True),
            SourceColumn(name="bonus", sensitive=True),
        ]
    ),
    "t_emp": TableMetadata(
        columns=[
            SourceColumn(name="emp_id", sensitive=False),
            SourceColumn(name="emp_name", sensitive=True),
            SourceColumn(name="id_card", sensitive=True),
        ]
    ),
    "t_dept": TableMetadata(
        columns=[
            SourceColumn(name="dept_id", sensitive=False),
            SourceColumn(name="dept_name", sensitive=False),
        ]
    ),
    "t_archive": TableMetadata(
        columns=[
            SourceColumn(name="emp_id", sensitive=False),
            SourceColumn(name="base_salary", sensitive=True),
        ]
    ),
}


def analyze(sql: str, tables: dict | None = None) -> AnalyzeRequest:
    return LineageAnalyzer(tables or TABLES).analyze(sql)


def src_tuples(col):
    return {(s.table, s.column, s.sensitive) for s in col.sources if s.table}


# --------------------------------------------------------------------------- #
# 1. 基础投影与敏感标记
# --------------------------------------------------------------------------- #


class TestBasicProjection:
    def test_plain_columns_sensitivity(self):
        res = analyze("SELECT emp_id, base_salary FROM t_salary")
        assert res.incomplete is False
        assert res.output_columns[0].sensitive is False
        assert res.output_columns[1].sensitive is True
        assert src_tuples(res.output_columns[1]) == {("t_salary", "base_salary", True)}

    def test_expression_keeps_sensitivity(self):
        res = analyze("SELECT base_salary + bonus AS total FROM t_salary")
        col = res.output_columns[0]
        assert col.sensitive is True
        assert src_tuples(col) == {
            ("t_salary", "base_salary", True),
            ("t_salary", "bonus", True),
        }

    def test_mixed_sensitivity_rolls_up_to_sensitive(self):
        res = analyze("SELECT emp_id + base_salary AS mix FROM t_salary")
        assert res.output_columns[0].sensitive is True

    def test_hash_does_not_clear_sensitivity(self):
        # 哈希/编码不解除标记：来源仍是敏感列
        res = analyze("SELECT md5(id_card) AS id_hash FROM t_emp")
        col = res.output_columns[0]
        assert col.sensitive is True
        assert src_tuples(col) == {("t_emp", "id_card", True)}

    def test_cast_and_case_keep_sensitivity(self):
        res = analyze(
            "SELECT CASE WHEN base_salary > 0 THEN CAST(bonus AS text) ELSE 'x' END v "
            "FROM t_salary"
        )
        assert res.output_columns[0].sensitive is True


# --------------------------------------------------------------------------- #
# 2. 聚合 SUM / COUNT / AVG
# --------------------------------------------------------------------------- #


class TestAggregates:
    def test_sum_propagates_sensitive(self):
        res = analyze("SELECT dept_id, SUM(base_salary) s FROM t_salary GROUP BY dept_id")
        assert res.output_columns[1].sensitive is True
        assert src_tuples(res.output_columns[1]) == {("t_salary", "base_salary", True)}

    def test_count_star_has_no_column_dependency(self):
        res = analyze("SELECT COUNT(*) AS c FROM t_salary")
        col = res.output_columns[0]
        assert col.sensitive is False
        assert len(col.sources) == 1
        assert col.sources[0].unresolved_reason == "row_marker_no_column_dependency"

    def test_count_column_and_avg(self):
        res = analyze(
            "SELECT COUNT(emp_id) c, AVG(base_salary) a, SUM(bonus) b FROM t_salary"
        )
        assert res.output_columns[0].sensitive is False
        assert res.output_columns[1].sensitive is True
        assert res.output_columns[2].sensitive is True


# --------------------------------------------------------------------------- #
# 3. JOIN（INNER / LEFT）与联接依赖
# --------------------------------------------------------------------------- #


class TestJoins:
    def test_inner_join_lineage(self):
        res = analyze(
            "SELECT e.emp_name, s.base_salary "
            "FROM t_salary s INNER JOIN t_emp e ON s.emp_id = e.emp_id"
        )
        assert res.incomplete is False
        names = {c.output_name for c in res.output_columns}
        assert names == {"emp_name", "base_salary"}
        join_deps = [d for d in res.dependencies if d.clause == "join_on"]
        assert len(join_deps) == 1
        assert {("t_salary", "emp_id"), ("t_emp", "emp_id")} <= {
            (s.table, s.column) for s in join_deps[0].sources
        }

    def test_left_join(self):
        res = analyze(
            "SELECT d.dept_name, e.emp_name FROM t_dept d "
            "LEFT JOIN t_emp e ON d.dept_id = e.emp_id"
        )
        assert res.incomplete is False
        assert res.output_columns[1].sensitive is True

    def test_join_dependencies_pierce_derived(self):
        res = analyze(
            "SELECT x.emp_name FROM ("
            " SELECT emp_id, emp_name FROM t_emp"
            ") x JOIN t_dept d ON x.emp_id = d.dept_id"
        )
        dep = next(d for d in res.dependencies if d.clause == "join_on")
        # 联接依赖穿过派生表追溯到物理列
        assert any(s.table == "t_emp" and s.column == "emp_id" for s in dep.sources)

    def test_using_join_resolves_both_sides(self):
        res = analyze(
            "SELECT e.emp_name FROM t_emp e JOIN t_salary s USING (emp_id)"
        )
        join_deps = [d for d in res.dependencies if d.clause == "join_on"]
        tables = {s.table for d in join_deps for s in d.sources}
        assert {"t_emp", "t_salary"} <= tables

    def test_inner_where_bubbles_through_derived(self):
        res = analyze(
            "SELECT x.pay FROM ("
            " SELECT emp_id, base_salary AS pay FROM t_salary WHERE base_salary > 10"
            ") x"
        )
        where = next(d for d in res.dependencies if d.clause == "where")
        assert where.scope == "x"
        assert any(s.table == "t_salary" and s.column == "base_salary" for s in where.sources)
        assert where.sensitive is True

    def test_deeply_nested_dependencies_reach_root(self):
        res = analyze(
            "SELECT o.emp_id FROM ("
            " SELECT m.emp_id FROM ("
            "  SELECT emp_id, base_salary FROM t_salary WHERE bonus > 5"
            "  GROUP BY emp_id, base_salary"
            " ) m"
            ") o"
        )
        scopes = {d.scope for d in res.dependencies}
        assert "m" in scopes
        assert any(d.clause == "where" for d in res.dependencies)
        assert any(d.clause == "group_by" for d in res.dependencies)


# --------------------------------------------------------------------------- #
# 4. 派生表与中间列路径
# --------------------------------------------------------------------------- #


class TestDerivedTables:
    def test_path_through_derived(self):
        res = analyze(
            "SELECT outer_s.emp_id, outer_s.pay "
            "FROM (SELECT emp_id, base_salary AS pay FROM t_salary) outer_s"
        )
        pay = next(c for c in res.output_columns if c.output_name == "pay")
        ref = pay.sources[0]
        assert (ref.table, ref.column) == ("t_salary", "base_salary")
        relations = [h.relation for h in ref.path]
        assert "outer_s" in relations
        cols = [h.column for h in ref.path]
        assert "base_salary" in cols and "pay" in cols

    def test_nested_same_alias_scoped(self):
        # 内外两层派生表同名 s：别名按各自作用域解释，路径中两次出现 s
        res = analyze(
            "SELECT s.emp_id FROM ("
            " SELECT s.emp_id FROM (SELECT emp_id, base_salary FROM t_salary) s"
            ") s"
        )
        ref = res.output_columns[0].sources[0]
        assert ref.table == "t_salary"
        assert [h.relation for h in ref.path].count("s") == 2
        assert all(h.kind == "derived" for h in ref.path if h.relation == "s")

    def test_unknown_derived_column(self):
        res = analyze("SELECT s.ghost FROM (SELECT emp_id FROM t_salary) s")
        assert res.incomplete is True
        assert any(i.code == "unknown_column" for i in res.issues)
        col = res.output_columns[0]
        assert col.sensitive is None  # 未知不得当作无敏感


# --------------------------------------------------------------------------- #
# 5. 星号展开
# --------------------------------------------------------------------------- #


class TestStar:
    def test_bare_star_expands_all_columns(self):
        res = analyze("SELECT * FROM t_salary")
        assert {c.output_name for c in res.output_columns} == {
            "emp_id",
            "dept_id",
            "base_salary",
            "bonus",
        }
        sens = {c.output_name: c.sensitive for c in res.output_columns}
        assert sens["base_salary"] is True and sens["emp_id"] is False

    def test_qualified_star(self):
        res = analyze(
            "SELECT e.* FROM t_emp e JOIN t_dept d ON e.emp_id = d.dept_id"
        )
        assert {c.output_name for c in res.output_columns} == {
            "emp_id",
            "emp_name",
            "id_card",
        }

    def test_star_through_derived_keeps_lineage(self):
        res = analyze("SELECT x.* FROM (SELECT * FROM t_salary) x")
        sal = next(c for c in res.output_columns if c.output_name == "base_salary")
        assert sal.sensitive is True
        assert sal.sources[0].table == "t_salary"


# --------------------------------------------------------------------------- #
# 6. 非递归 CTE
# --------------------------------------------------------------------------- #


class TestCTE:
    def test_cte_lineage_and_path(self):
        res = analyze(
            "WITH c AS (SELECT emp_id, base_salary FROM t_salary) "
            "SELECT c.emp_id, c.base_salary FROM c"
        )
        assert res.incomplete is False
        ref = res.output_columns[1].sources[0]
        assert (ref.table, ref.column) == ("t_salary", "base_salary")
        assert any(h.kind == "cte" and h.relation == "c" for h in ref.path)

    def test_unused_cte_not_analyzed(self):
        res = analyze(
            "WITH used AS (SELECT emp_id FROM t_salary), "
            "dead AS (SELECT id_card FROM t_emp) "
            "SELECT emp_id FROM used"
        )
        assert res.unused_ctes == ["dead"]
        assert {c["name"] for c in res.ctes if c["analyzed"]} == {"used"}
        # 未引用 CTE 的敏感列不得出现在结果里
        all_srcs = {s.column for col in res.output_columns for s in col.sources}
        assert "id_card" not in all_srcs

    def test_nested_cte_same_name_shadowing(self):
        res = analyze(
            "WITH c AS (SELECT emp_id FROM t_salary) "
            "SELECT c.emp_id FROM c "
            "JOIN (WITH c AS (SELECT dept_id AS emp_id FROM t_dept) "
            "      SELECT emp_id FROM c) d ON c.emp_id = d.emp_id"
        )
        # 内层 c 遮蔽外层 c，emp_id 同时有两表来源
        dep = next(d for d in res.dependencies if d.clause == "join_on")
        tables = {s.table for s in dep.sources}
        assert {"t_salary", "t_dept"} <= tables

    def test_recursive_cte_flagged(self):
        res = analyze(
            "WITH RECURSIVE r AS (SELECT 1 AS n UNION ALL SELECT n+1 FROM r WHERE n<3) "
            "SELECT n FROM r"
        )
        assert res.incomplete is True
        assert any(i.code == "recursive_cte_unsupported" for i in res.issues)


# --------------------------------------------------------------------------- #
# 7. UNION ALL 按列位置合并
# --------------------------------------------------------------------------- #


class TestUnionAll:
    def test_union_all_merges_by_position(self):
        res = analyze(
            "SELECT emp_id, base_salary FROM t_salary "
            "UNION ALL "
            "SELECT emp_id, base_salary FROM t_archive"
        )
        assert res.incomplete is False
        assert len(res.output_columns) == 2
        second = res.output_columns[1]
        assert {s.table for s in second.sources} == {"t_salary", "t_archive"}
        assert second.sensitive is True

    def test_union_all_takes_left_name(self):
        res = analyze(
            "SELECT emp_id AS a, base_salary AS b FROM t_salary "
            "UNION ALL "
            "SELECT emp_id, base_salary FROM t_archive"
        )
        assert [c.output_name for c in res.output_columns] == ["a", "b"]

    def test_union_distinct_marked_incomplete(self):
        res = analyze(
            "SELECT emp_id FROM t_salary UNION SELECT emp_id FROM t_archive"
        )
        assert res.incomplete is True
        assert any(i.code == "union_distinct_degraded" for i in res.issues)


# --------------------------------------------------------------------------- #
# 8. 筛选 / 分组 / 排序依赖
# --------------------------------------------------------------------------- #


class TestClauseDependencies:
    def test_where_group_having_dependencies(self):
        res = analyze(
            "SELECT dept_id, SUM(base_salary) s FROM t_salary "
            "WHERE bonus > 100 GROUP BY dept_id HAVING SUM(base_salary) > 1000"
        )
        clauses = {d.clause for d in res.dependencies}
        assert {"where", "group_by", "having"} <= clauses
        where = next(d for d in res.dependencies if d.clause == "where")
        assert any(s.column == "bonus" and s.sensitive is True for s in where.sources)

    def test_order_by_output_alias(self):
        res = analyze(
            "SELECT dept_id, SUM(base_salary) payroll FROM t_salary "
            "GROUP BY dept_id ORDER BY payroll DESC"
        )
        ob = next(d for d in res.dependencies if d.clause == "order_by")
        assert any(s.column == "base_salary" for s in ob.sources)
        assert ob.sensitive is True

    def test_group_and_order_by_ordinal(self):
        res = analyze(
            "SELECT dept_id, SUM(base_salary) FROM t_salary GROUP BY 1 ORDER BY 2"
        )
        gb = next(d for d in res.dependencies if d.clause == "group_by")
        ob = next(d for d in res.dependencies if d.clause == "order_by")
        assert any(s.column == "dept_id" for s in gb.sources)
        assert any(s.column == "base_salary" and s.sensitive is True for s in ob.sources)

    def test_order_by_ordinal_out_of_range(self):
        res = analyze("SELECT dept_id FROM t_salary ORDER BY 9")
        assert res.incomplete is True
        assert any(i.code == "ordinal_out_of_range" for i in res.issues)

    def test_dependencies_include_sensitivity(self):
        res = analyze("SELECT emp_id FROM t_salary WHERE base_salary > 0")
        where = next(d for d in res.dependencies if d.clause == "where")
        assert where.sensitive is True


# --------------------------------------------------------------------------- #
# 9. 失败语义：元数据缺失 / 歧义 / 不支持语法
# --------------------------------------------------------------------------- #


class TestFailureModes:
    def test_missing_table_metadata_is_unknown_not_safe(self):
        res = analyze("SELECT col_x FROM ghost_table")
        assert res.incomplete is True
        assert any(i.code == "metadata_missing" for i in res.issues)
        col = res.output_columns[0]
        assert col.sensitive is None
        assert all(s.sensitive is None for s in col.sources)

    def test_ambiguous_unqualified_column(self):
        res = analyze(
            "SELECT emp_id FROM t_salary s JOIN t_emp e ON s.emp_id = e.emp_id "
            "WHERE emp_id > 0"
        )
        assert res.incomplete is True
        assert any(i.code == "ambiguous_column" for i in res.issues)
        where = next(d for d in res.dependencies if d.clause == "where")
        # 两个候选都保留
        assert {s.table for s in where.sources if s.table} == {"t_salary", "t_emp"}

    def test_unknown_relation_qualifier(self):
        res = analyze("SELECT z.emp_id FROM t_salary s")
        assert res.incomplete is True
        assert any(i.code == "unknown_relation" for i in res.issues)

    def test_parse_error_is_located(self):
        res = analyze("SELECT FROM WHERE")
        assert res.incomplete is True
        issue = next(i for i in res.issues if i.code == "parse_error")
        assert issue.location is not None

    def test_unsupported_statement(self):
        res = analyze("DELETE FROM t_salary WHERE emp_id = 1")
        assert res.incomplete is True
        assert any(i.code == "unsupported_statement" for i in res.issues)

    def test_scalar_subquery_flagged(self):
        res = analyze(
            "SELECT emp_id, (SELECT max(emp_id) FROM t_emp) m FROM t_salary"
        )
        assert res.incomplete is True
        assert any(
            i.code == "subquery_in_expression_unsupported" for i in res.issues
        )

    def test_intersect_unsupported(self):
        res = analyze(
            "SELECT emp_id FROM t_salary INTERSECT SELECT emp_id FROM t_emp"
        )
        assert res.incomplete is True
        assert any(i.code == "set_op_unsupported" for i in res.issues)

    def test_unknown_never_treated_as_non_sensitive(self):
        # 混合已知敏感列与未知列：结果必须是 True 而非 False
        res = analyze(
            "SELECT base_salary + unknown_col AS v FROM t_salary"
        )
        assert res.output_columns[0].sensitive is True
        # 纯未知 -> None
        res2 = analyze("SELECT unknown_col FROM t_salary")
        assert res2.output_columns[0].sensitive is None


# --------------------------------------------------------------------------- #
# 10. 三态语义与别名作用域
# --------------------------------------------------------------------------- #


class TestThreeStateSemantics:
    def test_all_known_non_sensitive_is_false(self):
        res = analyze("SELECT emp_id, dept_id FROM t_salary")
        assert all(c.sensitive is False for c in res.output_columns)

    def test_alias_does_not_leak_across_scopes(self):
        res = analyze(
            "SELECT a.emp_id FROM (SELECT emp_id FROM t_salary) a "
            "JOIN (SELECT emp_id FROM t_archive) b ON a.emp_id = b.emp_id"
        )
        col = res.output_columns[0]
        assert {s.table for s in col.sources} == {"t_salary"}

    def test_ordinal_ordering(self):
        res = analyze("SELECT emp_id, dept_id, base_salary, bonus FROM t_salary")
        assert [c.ordinal for c in res.output_columns] == [1, 2, 3, 4]
