"""薪酬数据字段溯源 API。

启动后访问 http://localhost:8000/docs 即可在 Swagger UI 中提交分析。
本服务只做静态 SQL 血缘分析，不连接任何业务数据库。
"""

from __future__ import annotations

from fastapi import FastAPI, HTTPException
from fastapi.responses import RedirectResponse

from .analyzer import LineageAnalyzer
from .models import AnalyzeRequest, AnalyzeResponse
from .samples import (
    SAMPLE_SQL_INCOMPLETE,
    SAMPLE_SQL_NESTED_ALIASES,
    SAMPLE_SQL_UNION_ALL,
    SAMPLE_TABLES,
    build_sample_request,
)

app = FastAPI(
    title="薪酬数据字段溯源 API",
    version="1.0.0",
    description=(
        "提交 PostgreSQL SELECT、源表列清单及敏感标记，返回每个输出列的"
        "物理来源、中间列路径与三态敏感标记，以及筛选/联接/分组依赖。\n\n"
        "**不连接业务库**，纯静态分析。未知来源的 sensitive 为 `null`，"
        "绝不等同于无敏感来源；存在任何问题时 `incomplete=true`。"
    ),
)


@app.get("/", include_in_schema=False)
def root() -> RedirectResponse:
    return RedirectResponse(url="/docs")


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.post("/api/v1/lineage", response_model=AnalyzeResponse, tags=["lineage"])
def analyze_lineage(request: AnalyzeRequest) -> AnalyzeResponse:
    """对提交的 SELECT 做列级溯源分析。

    解析失败、元数据缺失、列名歧义或不支持语法都会在 ``issues`` 中定位，
    并将 ``incomplete`` 置为 true；HTTP 层仍返回 200 以便拿到全部线索。
    """
    if not request.sql.strip():
        raise HTTPException(status_code=422, detail="sql 不能为空")
    analyzer = LineageAnalyzer(request.tables, dialect=request.dialect)
    return analyzer.analyze(request.sql)


@app.get("/api/v1/samples/tables", tags=["samples"])
def get_sample_tables() -> dict:
    """虚构薪酬表结构（含敏感标记），可直接作为请求的 tables 字段。"""
    return {
        name: {
            "columns": [c.model_dump() for c in meta.columns],
        }
        for name, meta in SAMPLE_TABLES.items()
    }


@app.get("/api/v1/samples/sql/{kind}", tags=["samples"])
def get_sample_sql(kind: str) -> dict:
    """获取样例 SQL：nested（嵌套同名别名）/ union_all / incomplete。"""
    sqls = {
        "nested": SAMPLE_SQL_NESTED_ALIASES,
        "union_all": SAMPLE_SQL_UNION_ALL,
        "incomplete": SAMPLE_SQL_INCOMPLETE,
    }
    if kind not in sqls:
        raise HTTPException(status_code=404, detail=f"未知样例 {kind}，可选：{list(sqls)}")
    return {"kind": kind, "sql": sqls[kind]}


@app.post("/api/v1/samples/lineage/{kind}", response_model=AnalyzeResponse, tags=["samples"])
def analyze_sample(kind: str) -> AnalyzeResponse:
    """直接用虚构薪酬表结构跑指定样例，便于快速体验。"""
    if kind not in ("nested", "union_all", "incomplete"):
        raise HTTPException(status_code=404, detail=f"未知样例 {kind}")
    req = build_sample_request(kind)
    return LineageAnalyzer(req.tables, dialect=req.dialect).analyze(req.sql)
