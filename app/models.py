"""请求 / 响应数据模型。

敏感标记 ``sensitive`` 为三态：

* ``True``  —— 来源物理列在元数据中被显式标记为敏感；
* ``False`` —— 来源物理列存在且未被标记为敏感；
* ``None``  —— 来源无法解析（元数据缺失等），此时不得当作无敏感来源。
"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field


class SourceColumn(BaseModel):
    """提交的源表列清单中的一列。"""

    name: str = Field(..., description="物理列名")
    sensitive: bool = Field(False, description="是否为敏感字段")
    comment: Optional[str] = Field(None, description="可选说明，如身份证号/薪资")


class TableMetadata(BaseModel):
    """一张物理源表的元数据。"""

    columns: list[SourceColumn] = Field(default_factory=list)


class AnalyzeRequest(BaseModel):
    """溯源分析请求。"""

    sql: str = Field(..., description="PostgreSQL 方言的单条 SELECT 语句")
    tables: dict[str, TableMetadata] = Field(
        default_factory=dict,
        description="物理表名 -> 列清单（键为表名，忽略 schema 前缀，大小写不敏感）",
    )
    dialect: str = Field("postgres", description="sqlglot 方言，默认 postgres")


class PathHop(BaseModel):
    """中间列路径上的一跳：经某作用域别名/CTE 中的某列传递。"""

    relation: str = Field(..., description="该作用域内的关系别名（表别名/派生表别名/CTE 名）")
    column: str = Field(..., description="该关系上被引用的列名")
    kind: Literal["table", "derived", "cte"] = Field(..., description="关系类型")


class SourceRef(BaseModel):
    """一个最终可追溯到的物理列来源，或一个无法解析的来源。"""

    table: Optional[str] = Field(None, description="物理表名；无法解析时为 None")
    column: Optional[str] = Field(None, description="物理列名；无法解析时为 None")
    sensitive: Optional[bool] = Field(
        ..., description="敏感标记；None 表示未知，不得视为不敏感"
    )
    path: list[PathHop] = Field(
        default_factory=list,
        description="从物理列到当前引用点的中间列路径（按穿过顺序）",
    )
    unresolved_reason: Optional[str] = Field(
        None, description="无法解析时的原因，如 metadata_missing / ambiguous"
    )


class OutputColumn(BaseModel):
    """SELECT 输出列的溯源结果。"""

    ordinal: int = Field(..., description="输出列序号，从 1 开始（UNION ALL 按位置合并）")
    output_name: str = Field(..., description="输出列名/别名")
    expression: str = Field(..., description="该输出列的 SQL 表达式文本")
    sources: list[SourceRef] = Field(default_factory=list, description="物理列来源清单")
    intermediate_path: list[PathHop] = Field(
        default_factory=list,
        description="最终作用域中直接引用的中间列路径（来源 path 的最后一跳视图）",
    )
    sensitive: Optional[bool] = Field(
        ...,
        description=(
            "聚合后的敏感标记：任一来源敏感为 True；"
            "无敏感来源但存在未知来源为 None；全部明确不敏感才为 False"
        ),
    )


class Dependency(BaseModel):
    """筛选 / 联接 / 分组 / 排序等子句中的一个依赖项。"""

    clause: str = Field(..., description="子句类型：where / join_on / group_by / having / order_by")
    expression: str = Field(..., description="依赖表达式文本")
    relation: Optional[str] = Field(None, description="所在作用域的关系别名（可空）")
    scope: str = Field("root", description="依赖所在作用域：root 或派生表/CTE 别名")
    sources: list[SourceRef] = Field(default_factory=list)
    sensitive: Optional[bool] = Field(..., description="同 OutputColumn 的三态聚合规则")


class Issue(BaseModel):
    """分析过程中发现的问题。存在任意问题时 incomplete=True。"""

    code: str = Field(..., description="问题代码")
    message: str = Field(..., description="人类可读说明")
    location: Optional[str] = Field(None, description="定位：行:列 或 SQL 片段")


class AnalyzeResponse(BaseModel):
    """溯源分析响应。"""

    incomplete: bool = Field(
        ..., description="分析是否不完整（存在元数据缺失、歧义或不支持语法等）"
    )
    output_columns: list[OutputColumn]
    dependencies: list[Dependency] = Field(
        default_factory=list,
        description="筛选/联接/分组/排序依赖（已随派生查询向下穿透）",
    )
    ctes: list[dict] = Field(default_factory=list, description="参与分析的 CTE 及递归标记")
    unused_ctes: list[str] = Field(
        default_factory=list, description="定义了但未被引用、不参与分析的 CTE"
    )
    issues: list[Issue] = Field(default_factory=list)
    supported_syntax: list[str] = Field(default_factory=list)
