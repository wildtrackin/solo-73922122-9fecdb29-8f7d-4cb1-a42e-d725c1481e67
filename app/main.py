"""薪酬数据字段溯源 API。

启动后访问 http://localhost:8000/docs 即可在 Swagger UI 中提交分析。
本服务只做静态 SQL 血缘分析，不连接任何业务数据库。
"""

from __future__ import annotations

from fastapi import FastAPI, HTTPException
from fastapi.responses import RedirectResponse

from .analyzer import LineageAnalyzer
from .models import AnalyzeRequest, AnalyzeResponse
from .recon_models import ReconcileRequest, ReconcileResponse
from .reconcile import reconcile
from .samples import (
    SAMPLE_SQL_INCOMPLETE,
    SAMPLE_SQL_NESTED_ALIASES,
    SAMPLE_SQL_UNION_ALL,
    SAMPLE_TABLES,
    build_sample_request,
    build_sample_reconcile_request,
)

app = FastAPI(
    title="薪酬数据服务 API",
    version="1.1.0",
    description=(
        "## 字段溯源\n\n"
        "提交 PostgreSQL SELECT、源表列清单及敏感标记，返回每个输出列的"
        "物理来源、中间列路径与三态敏感标记，以及筛选/联接/分组依赖。"
        "**不连接业务库**，纯静态分析。未知来源的 sensitive 为 `null`，"
        "绝不等同于无敏感来源；存在任何问题时 `incomplete=true`。\n\n"
        "## 工资单修订对账\n\n"
        "提交同月最多 500 条工资单修订（乱序、重传自动去重）及各部门应发总额，"
        "按连续修订确定每张工资单的最终状态（作废不计金额），逐部门核对。"
        "序号缺口或同序号内容冲突会隔离该工资单涉及的全部部门；"
        "缺少应发汇总的部门同样不得放行。"
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


# --------------------------------------------------------------------------- #
# 工资单修订对账
# --------------------------------------------------------------------------- #


@app.post(
    "/api/v1/payroll/reconcile",
    response_model=ReconcileResponse,
    tags=["payroll-reconcile"],
)
def reconcile_payroll(request: ReconcileRequest) -> ReconcileResponse:
    """月度工资单修订对账。

    修订可乱序提交，完全相同的重传只算一次；按连续修订序号确定每张工资单的
    最终状态，作废（void）不计金额。出现序号缺口或同序号内容冲突时，隔离该
    工资单历次涉及的全部部门，且不能用剩余明细放行它们；跨部门更正时新旧部门
    均按最终状态核对。返回核对通过的部门及可入仓明细，其余部门给出差额或无法
    确定差额的原因；缺少应发汇总的部门也不得放行。
    """
    try:
        return reconcile(request)
    except ValueError as exc:
        # 例如同一部门重复申报应发总额
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get(
    "/api/v1/samples/reconcile/payload",
    tags=["samples"],
)
def get_sample_reconcile_payload() -> dict:
    """获取工资单对账样例请求体（含正常、跨部门更正、缺口、冲突、缺汇总等场景）。"""
    request = build_sample_reconcile_request()
    return request.model_dump()


@app.post(
    "/api/v1/samples/reconcile",
    response_model=ReconcileResponse,
    tags=["samples"],
)
def run_sample_reconcile() -> ReconcileResponse:
    """一键运行工资单对账样例，便于在 Swagger UI 中快速体验。"""
    return reconcile(build_sample_reconcile_request())
