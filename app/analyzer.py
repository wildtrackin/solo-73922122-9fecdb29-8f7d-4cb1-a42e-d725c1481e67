"""基于 sqlglot 的列级血缘分析器（不连接任何业务数据库）。

设计要点
========

* 每个 ``SELECT`` / ``UNION ALL`` 查询体被分析为一个 :class:`ScopeResult`，
  记录该作用域输出列到物理来源（含中间列路径）的映射；
* 派生表（子查询）与 CTE 递归调用分析器，外层引用其输出列时把内层来源
  整体继承并在 ``path`` 上追加一跳 —— 依赖因此能够穿过派生查询传至最终结果；
* 敏感标记三态传播：显式敏感 -> True；元数据缺失/无法解析 -> None（未知，
  绝不当作无敏感来源）；SUM/COUNT/AVG、CASE、CAST、哈希等表达式都不解除标记；
* 别名严格按作用域解释，内层 WITH 的同名 CTE 遮蔽外层；
  UNION ALL 按列位置合并两侧来源；未被引用的 CTE 不参与分析；
* 元数据缺失、列名歧义、不支持的语法均产生 :class:`Issue` 并置
  ``incomplete=True``。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from sqlglot import exp, parse_one
from sqlglot.errors import ParseError

from .models import AnalyzeResponse, Dependency, Issue, OutputColumn, PathHop, SourceRef

# 声明支持的语法范围
SUPPORTED_SYNTAX = [
    "non_recursive_cte",
    "derived_table",
    "inner_join",
    "left_join",
    "right_join",
    "full_outer_join",
    "cross_join",
    "star_expansion",
    "union_all",
    "sum_count_avg_aggregates",
    "where_group_by_having_order_by",
    "aliases_scoped",
]


# --------------------------------------------------------------------------- #
# 内部数据结构
# --------------------------------------------------------------------------- #


@dataclass
class ColumnFact:
    """作用域内某关系上的一个输出列。"""

    name: str
    sources: list[SourceRef]  # 该列追溯到的物理来源（path 已含内层跳数）
    known: bool  # 列结构是否已知（元数据缺失导致星号无法展开时为 False）
    expression: str = ""  # 产生该列的 SQL 表达式文本


@dataclass
class Relation:
    """FROM/JOIN 中的一个关系。"""

    alias: str  # 本作用域内解释列限定名所用的别名
    kind: str  # table / derived / cte
    table_name: Optional[str]  # 物理表名（仅 kind=table）
    columns: list[ColumnFact]
    node: Optional[exp.Expression] = None


@dataclass
class ScopeResult:
    """一个查询体（SELECT 或集合操作）的分析结果。"""

    columns: list[ColumnFact]
    dependencies: list[Dependency] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# 分析器
# --------------------------------------------------------------------------- #


class LineageAnalyzer:
    def __init__(self, tables: dict, dialect: str = "postgres"):
        # 元数据键统一小写
        self.meta: dict[str, dict[str, bool]] = {}
        for tname, tmeta in tables.items():
            self.meta[tname.lower()] = {c.name.lower(): c.sensitive for c in tmeta.columns}
        self.dialect = dialect
        self.issues: list[Issue] = []
        # 全部 CTE 节点（用于输出参与/未参与清单）
        self.all_cte_nodes: list[exp.CTE] = []
        self.used_cte_nodes: set[int] = set()
        # CTE 作用域栈：每层为 {小写名: CTE}，内层同名遮蔽外层
        self._cte_stack: list[dict[str, exp.CTE]] = []
        # 防止 CTE/派生表分析循环
        self._analyzing: set[int] = set()
        # 同一分析内已上报过的内嵌子查询，避免重复 issue
        self._reported_subqueries: set[int] = set()
        # 跨作用域冒泡的依赖（派生表/CTE 内层的 where/join/group 等）
        self._nested_deps: list[Dependency] = []

    # ------------------------------------------------------------------ #
    # 问题上报
    # ------------------------------------------------------------------ #

    def _loc(self, node: Optional[exp.Expression]) -> Optional[str]:
        if node is None:
            return None
        meta = getattr(node, "meta", None) or {}
        line = meta.get("line")
        col = meta.get("col")
        if line:
            return f"line {line}, col {col}"
        snippet = node.sql()[:60]
        return f"near `{snippet}`" if snippet else None

    def _issue(self, code: str, message: str, node: Optional[exp.Expression] = None) -> None:
        self.issues.append(Issue(code=code, message=message, location=self._loc(node)))

    # ------------------------------------------------------------------ #
    # CTE 作用域
    # ------------------------------------------------------------------ #

    def _cte_is_recursive(self, cte: exp.CTE) -> bool:
        with_node = cte.parent
        return bool(isinstance(with_node, exp.With) and with_node.args.get("recursive"))

    def _push_ctes(self, with_node: Optional[exp.With]) -> Optional[dict[str, exp.CTE]]:
        if with_node is None:
            return None
        if with_node.args.get("recursive"):
            self._issue(
                "recursive_cte_unsupported",
                "RECURSIVE CTE 不在支持范围内（仅支持非递归 CTE），相关引用标记为未知",
                with_node,
            )
        layer: dict[str, exp.CTE] = {}
        for cte in with_node.expressions:
            self.all_cte_nodes.append(cte)
            layer[cte.alias.lower()] = cte
        self._cte_stack.append(layer)
        return layer

    def _pop_ctes(self, layer: Optional[dict[str, exp.CTE]]) -> None:
        if layer is not None:
            self._cte_stack.pop()

    def _find_cte(self, name: str) -> Optional[exp.CTE]:
        key = name.lower()
        for layer in reversed(self._cte_stack):
            if key in layer:
                return layer[key]
        return None

    # ------------------------------------------------------------------ #
    # 入口
    # ------------------------------------------------------------------ #

    def analyze(self, sql: str) -> AnalyzeResponse:
        try:
            tree = parse_one(sql, read=self.dialect)
        except ParseError as exc:
            msg = str(exc).split("\n")[0]
            loc = None
            errors = getattr(exc, "errors", None)
            if errors and isinstance(errors[0], dict):
                loc = f"line {errors[0].get('line')}, col {errors[0].get('col')}"
            self.issues.append(Issue(code="parse_error", message=msg, location=loc))
            return self._empty_response()

        if tree is None or not isinstance(tree, (exp.Select, exp.Union, exp.Intersect, exp.Except)):
            self._issue(
                "unsupported_statement",
                f"仅支持单条 SELECT 语句，收到 {type(tree).__name__ if tree else '空语句'}",
                tree,
            )
            return self._empty_response()

        root = self._analyze_query(tree)

        # 根作用域依赖 + 派生表/CTE 内层冒泡上来的依赖（去重后保留顺序）
        merged_deps: list[Dependency] = []
        seen_dep: set[str] = set()
        for dep in list(self._nested_deps) + root.dependencies:
            key = dep.model_dump_json()
            if key not in seen_dep:
                seen_dep.add(key)
                merged_deps.append(dep)

        unused = [
            cte.alias for cte in self.all_cte_nodes if id(cte) not in self.used_cte_nodes
        ]
        cte_info = [
            {
                "name": cte.alias,
                "recursive": self._cte_is_recursive(cte),
                "analyzed": id(cte) in self.used_cte_nodes,
            }
            for cte in self.all_cte_nodes
        ]

        outputs: list[OutputColumn] = []
        for i, fact in enumerate(root.columns, start=1):
            outputs.append(
                OutputColumn(
                    ordinal=i,
                    output_name=fact.name or f"col_{i}",
                    expression=fact.expression or fact.name or f"col_{i}",
                    sources=fact.sources,
                    intermediate_path=self._last_hops(fact.sources),
                    sensitive=self._rollup(fact.sources),
                )
            )

        return AnalyzeResponse(
            incomplete=bool(self.issues),
            output_columns=outputs,
            dependencies=merged_deps,
            ctes=cte_info,
            unused_ctes=unused,
            issues=self.issues,
            supported_syntax=SUPPORTED_SYNTAX,
        )

    def _empty_response(self) -> AnalyzeResponse:
        return AnalyzeResponse(
            incomplete=True,
            output_columns=[],
            dependencies=[],
            ctes=[
                {
                    "name": cte.alias,
                    "recursive": self._cte_is_recursive(cte),
                    "analyzed": False,
                }
                for cte in self.all_cte_nodes
            ],
            unused_ctes=[cte.alias for cte in self.all_cte_nodes],
            issues=self.issues,
            supported_syntax=SUPPORTED_SYNTAX,
        )

    # ------------------------------------------------------------------ #
    # 查询体：SELECT / UNION ALL
    # ------------------------------------------------------------------ #

    def _analyze_query(self, node: exp.Expression, scope_label: str = "root") -> ScopeResult:
        if isinstance(node, (exp.Union, exp.Intersect, exp.Except)):
            return self._analyze_union(node, scope_label)
        if isinstance(node, exp.Select):
            return self._analyze_select(node, scope_label)
        self._issue(
            "unsupported_query",
            f"不支持的查询体类型 {type(node).__name__}",
            node,
        )
        return ScopeResult(columns=[])

    def _analyze_union(self, node: exp.Union, scope_label: str = "root") -> ScopeResult:
        # sqlglot 中 Intersect/Except 是 Union 的子类，需按精确类型排除
        if not type(node) is exp.Union:
            self._issue(
                "set_op_unsupported",
                f"集合操作 {type(node).__name__.upper()} 不支持，仅支持 UNION ALL / UNION",
                node,
            )
            return ScopeResult(columns=[])
        if node.args.get("distinct"):
            self._issue(
                "union_distinct_degraded",
                "UNION（去重）不在完整支持范围内，本次仍按列位置合并来源并标记为不完整；"
                "请优先使用 UNION ALL",
                node,
            )

        left = self._analyze_query(node.left, scope_label)
        right = self._analyze_query(node.right, scope_label)
        deps = left.dependencies + right.dependencies

        merged: list[ColumnFact] = []
        width = max(len(left.columns), len(right.columns))
        for i in range(width):
            lf = left.columns[i] if i < len(left.columns) else None
            rf = right.columns[i] if i < len(right.columns) else None
            if lf is None or rf is None:
                self._issue(
                    "union_arity_mismatch",
                    f"UNION 两侧列数不一致：第 {i + 1} 列仅单侧存在",
                    node,
                )
            sources = list(lf.sources if lf else []) + list(rf.sources if rf else [])
            merged.append(
                ColumnFact(
                    name=(lf.name if lf else (rf.name if rf else f"col_{i + 1}")),
                    sources=sources,
                    known=bool(lf and lf.known and rf and rf.known),
                    expression=(lf.expression if lf else (rf.expression if rf else "")),
                )
            )
        return ScopeResult(columns=merged, dependencies=deps)

    # ------------------------------------------------------------------ #
    # SELECT 作用域
    # ------------------------------------------------------------------ #

    def _analyze_select(self, select: exp.Select, scope_label: str = "root") -> ScopeResult:
        key = id(select)
        if key in self._analyzing:
            self._issue("cyclic_reference", "检测到 CTE/子查询循环引用", select)
            return ScopeResult(columns=[])
        self._analyzing.add(key)
        layer = self._push_ctes(select.args.get("with"))
        try:
            relations = self._build_relations(select)
            scope_deps: list[Dependency] = []

            # 先构建输出列：GROUP BY/ORDER BY 的序号与输出别名需要引用它
            out_facts: list[ColumnFact] = []
            for proj in select.expressions:
                out_facts.extend(self._analyze_projection(proj, relations))
            out_by_name = {f.name.lower(): f for f in out_facts}

            def add_dep(clause: str, expr: exp.Expression) -> None:
                refs = self._resolve_expression(expr, relations)
                scope_deps.append(
                    Dependency(
                        clause=clause,
                        expression=expr.sql(),
                        scope=scope_label,
                        sources=refs,
                        sensitive=self._rollup(refs),
                    )
                )

            where = select.args.get("where")
            if where:
                add_dep("where", where.this)

            for join in select.args.get("joins") or []:
                on = join.args.get("on")
                using = join.args.get("using")
                if on:
                    add_dep("join_on", on)
                elif using:
                    # 不同 sqlglot 版本中 using 可能是标识符列表或 Using 节点
                    idents = using.expressions if hasattr(using, "expressions") else using
                    for ident in idents:
                        # USING 列在两侧关系中同名，逐关系解析以保留真实来源
                        for rel in relations:
                            match = next(
                                (cf for cf in rel.columns if cf.name.lower() == ident.name.lower()),
                                None,
                            )
                            if match is not None:
                                refs = [
                                    self._append_hop(r, rel, match.name)
                                    if rel.kind != "table"
                                    else r.model_copy(deep=True)
                                    for r in match.sources
                                ]
                                scope_deps.append(
                                    Dependency(
                                        clause="join_on",
                                        expression=f"USING ({ident.name})",
                                        scope=scope_label,
                                        sources=refs,
                                        sensitive=self._rollup(refs),
                                    )
                                )
                if join.args.get("natural"):
                    self._issue(
                        "natural_join",
                        "NATURAL JOIN 的隐式同名列联接无法精确定位，标记为不完整",
                        join,
                    )

            group = select.args.get("group")
            if group:
                for gexpr in group.expressions:
                    refs = self._resolve_output_ref(gexpr, relations, out_facts, out_by_name)
                    scope_deps.append(
                        Dependency(
                            clause="group_by",
                            expression=gexpr.sql(),
                            scope=scope_label,
                            sources=refs,
                            sensitive=self._rollup(refs),
                        )
                    )

            having = select.args.get("having")
            if having:
                add_dep("having", having.this)

            order = select.args.get("order")
            if order:
                for oexpr in order.expressions:
                    refs = self._resolve_output_ref(oexpr, relations, out_facts, out_by_name)
                    scope_deps.append(
                        Dependency(
                            clause="order_by",
                            expression=oexpr.sql(),
                            scope=scope_label,
                            sources=refs,
                            sensitive=self._rollup(refs),
                        )
                    )

            # 内层作用域（派生表/CTE）的依赖冒泡到分析器全局，
            # 使其能穿过派生查询出现在最终结果中
            if scope_label != "root":
                self._nested_deps.extend(scope_deps)

            return ScopeResult(columns=out_facts, dependencies=scope_deps)
        finally:
            self._pop_ctes(layer)
            self._analyzing.discard(key)

    # ------------------------------------------------------------------ #
    # FROM / JOIN 关系构建
    # ------------------------------------------------------------------ #

    def _build_relations(self, select: exp.Select) -> list[Relation]:
        relations: list[Relation] = []
        from_ = select.args.get("from")
        if from_ is not None:
            # sqlglot 27 的别名挂在关系节点自身（Table/Subquery.alias）
            relations.append(self._build_relation(from_.this, from_.this.alias))
        for join in select.args.get("joins") or []:
            relations.append(self._build_relation(join.this, join.this.alias))
        return relations

    def _build_relation(self, node: exp.Expression, explicit_alias: str) -> Relation:
        # CTE 引用在 sqlglot 中表现为 Table（名字=CTE 名），先于物理表拦截；
        # 作用域栈保证嵌套同名别名解析到最近一层
        if isinstance(node, exp.Table):
            cte = self._find_cte(node.name)
            if cte is not None:
                rel = self._relation_for_cte(cte)
                if explicit_alias:
                    rel.alias = explicit_alias
                return rel
            return self._build_table_relation(node, explicit_alias)
        if isinstance(node, exp.Subquery):
            return self._build_derived_relation(node, explicit_alias)
        self._issue(
            "unsupported_relation",
            f"不支持的 FROM 关系类型 {type(node).__name__}（如函数表/LATERAL 等）",
            node,
        )
        return Relation(
            alias=explicit_alias or "?",
            kind="derived",
            table_name=None,
            columns=[],
            node=node,
        )

    def _build_table_relation(self, table: exp.Table, explicit_alias: str) -> Relation:
        tname = table.name
        alias = explicit_alias or tname
        key = tname.lower()
        if key not in self.meta:
            self._issue(
                "metadata_missing",
                f"物理表 `{tname}` 未在元数据中提供列清单，其所有引用均为未知来源",
                table,
            )
            return Relation(
                alias=alias, kind="table", table_name=tname, columns=[], node=table
            )
        facts = [
            ColumnFact(
                name=col_name,
                known=True,
                sources=[
                    SourceRef(
                        table=tname,
                        column=col_name,
                        sensitive=sensitive,
                        # 首跳即物理表在当前作用域的别名，保证路径完整
                        path=[PathHop(relation=alias, column=col_name, kind="table")],
                    )
                ],
            )
            for col_name, sensitive in self.meta[key].items()
        ]
        return Relation(
            alias=alias, kind="table", table_name=tname, columns=facts, node=table
        )

    def _build_derived_relation(self, sub: exp.Subquery, explicit_alias: str) -> Relation:
        alias = explicit_alias or sub.alias or "?"
        if not explicit_alias and not sub.alias:
            self._issue(
                "derived_without_alias",
                "派生表缺少显式别名，已生成临时名称，外层限定名解析可能不可靠",
                sub,
            )
        result = self._analyze_query(sub.this, scope_label=alias)
        # 内层列的来源（含已累积的路径）原样继承，外层引用时再追加本关系一跳
        return Relation(
            alias=alias,
            kind="derived",
            table_name=None,
            columns=[
                ColumnFact(
                    name=cf.name,
                    sources=[r.model_copy(deep=True) for r in cf.sources],
                    known=cf.known,
                    expression=cf.expression,
                )
                for cf in result.columns
            ],
            node=sub,
        )

    def _relation_for_cte(self, cte: exp.CTE) -> Relation:
        self.used_cte_nodes.add(id(cte))
        result = self._analyze_query(cte.this, scope_label=cte.alias)
        # 与派生表同理：CTE 输出列保留完整来源路径，引用处追加 CTE 跳
        return Relation(
            alias=cte.alias,
            kind="cte",
            table_name=None,
            columns=[
                ColumnFact(
                    name=cf.name,
                    sources=[r.model_copy(deep=True) for r in cf.sources],
                    known=cf.known,
                    expression=cf.expression,
                )
                for cf in result.columns
            ],
            node=cte,
        )

    # ------------------------------------------------------------------ #
    # 投影（含星号展开）
    # ------------------------------------------------------------------ #

    def _analyze_projection(
        self, proj: exp.Expression, relations: list[Relation]
    ) -> list[ColumnFact]:
        # 裸 * 或 t.*
        if isinstance(proj, exp.Star) or (
            isinstance(proj, exp.Column) and isinstance(proj.this, exp.Star)
        ):
            qualifier = proj.table if isinstance(proj, exp.Column) else ""
            return self._expand_star(qualifier, relations, proj)

        refs = self._resolve_expression(proj, relations)
        name = proj.alias_or_name or proj.sql()[:30]
        # 无任何列依赖（COUNT(*) / COUNT(1) / 纯字面量）：不追溯物理列，
        # 显式标注 row_marker，避免被误读为“分析遗漏”
        if not refs and not self._has_scope_column(proj):
            refs = [
                SourceRef(
                    table=None,
                    column=None,
                    sensitive=False,
                    path=[],
                    unresolved_reason="row_marker_no_column_dependency",
                )
            ]
        return [ColumnFact(name=name, sources=refs, known=True, expression=proj.sql())]

    def _has_scope_column(self, expr: exp.Expression) -> bool:
        scope_node = self._enclosing_select(expr)
        for node in expr.walk():
            if isinstance(node, exp.Column) and self._belongs_to_scope(node, scope_node):
                return True
        return False

    def _expand_star(
        self, qualifier: str, relations: list[Relation], node: exp.Expression
    ) -> list[ColumnFact]:
        targets = relations
        if qualifier:
            targets = [r for r in relations if r.alias.lower() == qualifier.lower()]
            if not targets:
                self._issue(
                    "unknown_qualifier",
                    f"星号限定名 `{qualifier}.*` 在当前作用域找不到对应关系",
                    node,
                )
                return []
        facts: list[ColumnFact] = []
        for rel in targets:
            if not rel.columns:
                self._issue(
                    "star_unresolvable",
                    f"关系 `{rel.alias}` 的列结构未知（元数据缺失），`*` 无法展开",
                    node,
                )
                facts.append(
                    ColumnFact(
                        name="*",
                        known=False,
                        expression=node.sql(),
                        sources=[
                            SourceRef(
                                table=rel.table_name,
                                column=None,
                                sensitive=None,
                                path=[PathHop(relation=rel.alias, column="*", kind=rel.kind)],  # type: ignore[arg-type]
                                unresolved_reason="metadata_missing",
                            )
                        ],
                    )
                )
                continue
            for cf in rel.columns:
                if rel.kind == "table":
                    expanded = [r.model_copy(deep=True) for r in cf.sources]
                else:
                    expanded = [self._append_hop(r, rel, cf.name) for r in cf.sources]
                facts.append(
                    ColumnFact(
                        name=cf.name,
                        known=cf.known,
                        sources=expanded,
                        expression=cf.expression or cf.name,
                    )
                )
        return facts

    # ------------------------------------------------------------------ #
    # 表达式 -> 物理来源
    # ------------------------------------------------------------------ #

    def _collect_columns(
        self, expr: exp.Expression, scope_node: exp.Expression
    ) -> list[exp.Column]:
        """收集表达式中属于当前作用域的列，内嵌子查询整体跳过并上报。

        判定方式：从列向上找祖先，先遇到当前 Select 作用域即属于本作用域，
        先遇到其它 Subquery 则属于内嵌子查询（派生表自身的 Subquery 在
        作用域之外，不会被误判）。
        """
        columns: list[exp.Column] = []
        for node in expr.walk():
            if isinstance(node, exp.Subquery):
                if id(node) not in self._reported_subqueries:
                    self._reported_subqueries.add(id(node))
                    self._issue(
                        "subquery_in_expression_unsupported",
                        "表达式中的标量/IN 子查询不在支持范围内，其内部依赖未纳入分析",
                        node,
                    )
                continue
            if isinstance(node, exp.Column) and self._belongs_to_scope(node, scope_node):
                columns.append(node)
        return columns

    def _belongs_to_scope(self, node: exp.Expression, scope_node: exp.Expression) -> bool:
        """列是否归属 scope_node：先遇到作用域节点为 True，先遇到其它 Subquery 为 False。"""
        parent = node.parent
        while parent is not None:
            if parent is scope_node:
                return True
            if isinstance(parent, exp.Subquery):
                return False
            parent = parent.parent
        return False

    def _resolve_expression(
        self, expr: exp.Expression, relations: list[Relation]
    ) -> list[SourceRef]:
        scope_node = self._enclosing_select(expr)
        refs: list[SourceRef] = []
        for col in self._collect_columns(expr, scope_node):
            refs.extend(self._resolve_column(col, relations))

        # 去重（物理来源+路径完全相同只保留一次），保持顺序
        seen: set[str] = set()
        unique: list[SourceRef] = []
        for ref in refs:
            key = ref.model_dump_json()
            if key not in seen:
                seen.add(key)
                unique.append(ref)
        return unique

    @staticmethod
    def _enclosing_select(expr: exp.Expression) -> exp.Expression:
        node = expr
        while node is not None and not isinstance(node, exp.Select):
            node = node.parent
        return node if node is not None else expr

    def _resolve_output_ref(
        self,
        expr: exp.Expression,
        relations: list[Relation],
        out_facts: list[ColumnFact],
        out_by_name: dict[str, ColumnFact],
    ) -> list[SourceRef]:
        """解析 GROUP BY / ORDER BY 表达式对输出列的引用。

        支持三种 PostgreSQL 写法：
        * 序号位置（``GROUP BY 1`` / ``ORDER BY 2``）；
        * 裸输出列别名（``ORDER BY payroll``）；
        * 普通关系表达式（走常规列解析）。
        """
        inner = expr.this if isinstance(expr, exp.Ordered) else expr

        # 序号位置：字面量整数且在输出列序数范围内
        if isinstance(inner, exp.Literal) and inner.is_int:
            ordinal = int(inner.name)
            if 1 <= ordinal <= len(out_facts):
                return [r.model_copy(deep=True) for r in out_facts[ordinal - 1].sources]
            self._issue(
                "ordinal_out_of_range",
                f"分组/排序序号 {ordinal} 超出输出列数量 {len(out_facts)}",
                expr,
            )
            return []

        # 裸标识符命中输出列别名
        if isinstance(inner, exp.Column) and not inner.table:
            fact = out_by_name.get(inner.name.lower())
            if fact is not None:
                return [r.model_copy(deep=True) for r in fact.sources]

        return self._resolve_expression(expr, relations)

    def _resolve_column(self, col: exp.Column, relations: list[Relation]) -> list[SourceRef]:
        cname = col.name.lower()
        qualifier = col.table.lower() if col.table else ""

        candidates: list[tuple[Relation, ColumnFact]] = []
        for rel in relations:
            if qualifier and rel.alias.lower() != qualifier:
                continue
            for cf in rel.columns:
                if cf.name.lower() == cname:
                    candidates.append((rel, cf))

        # 未限定名歧义：两个及以上关系暴露该列，或无法排除空结构关系也持有该列。
        # 元数据缺失的关系没有列清单，同样必须保留为歧义风险而非静默忽略。
        if not qualifier:
            exposed = [
                rel
                for rel in relations
                if rel.columns and any(cf.name.lower() == cname for cf in rel.columns)
            ]
            empty_rels = [rel for rel in relations if not rel.columns]
            ambiguous_rels = list(exposed)
            if exposed and empty_rels:
                ambiguous_rels.extend(empty_rels)
            if len(ambiguous_rels) > 1 or (exposed and empty_rels and len(relations) > 1):
                names = sorted(r.alias for r in ambiguous_rels)
                self._issue(
                    "ambiguous_column",
                    f"未限定列 `{col.name}` 可能在多个关系中同时存在"
                    f"（{', '.join(names)}；含元数据缺失关系），"
                    "全部可解析候选均保留并标记分析不完整",
                    col,
                )

        if not candidates:
            matched_rels = [r for r in relations if not qualifier or r.alias.lower() == qualifier]
            if qualifier and not matched_rels:
                reason = "unknown_relation"
                msg = f"限定名 `{col.table}.{col.name}` 的关系别名在当前作用域不存在"
            elif matched_rels and all(r.kind in ("derived", "cte") for r in matched_rels):
                reason = "unknown_column"
                msg = (
                    f"列 `{col.sql()}` 不存在于派生表/CTE 的输出列中"
                    "（外层只能引用内层 SELECT 暴露的列名）"
                )
            else:
                reason = "metadata_missing"
                msg = (
                    f"列 `{col.sql()}` 无法在当前作用域解析"
                    "（列名不存在或表元数据缺失），按未知来源处理"
                )
            self._issue(reason, msg, col)
            return [
                SourceRef(
                    table=None,
                    column=col.name,
                    sensitive=None,
                    path=[
                        PathHop(
                            relation=col.table or "?",
                            column=col.name,
                            kind="table",
                        )
                    ],
                    unresolved_reason=reason,
                )
            ]

        out: list[SourceRef] = []
        for rel, cf in candidates:
            for ref in cf.sources:
                if rel.kind == "table":
                    # 物理表来源已在构建时带好首跳（含别名），直接继承
                    out.append(ref.model_copy(deep=True))
                else:
                    # 穿过派生表/CTE：追加本作用域关系别名与列名一跳
                    out.append(self._append_hop(ref, rel, cf.name))
        return out

    # ------------------------------------------------------------------ #
    # 路径与敏感标记
    # ------------------------------------------------------------------ #

    @staticmethod
    def _append_hop(ref: SourceRef, rel: Relation, column_name: str) -> SourceRef:
        new_ref = ref.model_copy(deep=True)
        new_ref.path.append(
            PathHop(relation=rel.alias, column=column_name, kind=rel.kind)  # type: ignore[arg-type]
        )
        return new_ref

    @staticmethod
    def _last_hops(refs: list[SourceRef]) -> list[PathHop]:
        seen: set[str] = set()
        hops: list[PathHop] = []
        for ref in refs:
            if ref.path:
                key = ref.path[-1].model_dump_json()
                if key not in seen:
                    seen.add(key)
                    hops.append(ref.path[-1])
        return hops

    @staticmethod
    def _rollup(refs: list[SourceRef]) -> Optional[bool]:
        """三态聚合：任一 True -> True；否则任一 None -> None；否则 False。

        函数（含 SUM/COUNT/AVG）、CASE、CAST、哈希表达式等都不解除标记：
        标记只随物理来源传播，表达式变换不改变来源集合。
        """
        if any(r.sensitive is True for r in refs):
            return True
        if any(r.sensitive is None for r in refs):
            return None
        return False
