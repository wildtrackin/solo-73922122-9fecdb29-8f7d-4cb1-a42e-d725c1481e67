"""工资单修订对账 API 的请求 / 响应数据模型。

核心语义：

* 工资单修订按 ``(工资单号, 修订序号)`` 定位，序号从 1 开始且必须连续；
* 完全相同的重传只算一次；同序号内容冲突（部门/金额/动作不一致）为数据冲突；
* 出现序号缺口或同序号冲突时，该工资单历次修订涉及的**全部部门**一律隔离，
  不得用剩余明细放行；
* 作废（void）修订不产生金额，跨部门更正时新旧部门都按最终状态核对；
* 缺少应发汇总的部门同样不得放行。
"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field

# 一次对账请求允许提交的工资单修订条数上限（同月）。
MAX_REVISIONS = 500

# 修订动作：入账 / 作废。
RevisionAction = Literal["post", "void"]


class SalaryRevision(BaseModel):
    """一条工资单修订明细。"""

    payslip_id: str = Field(..., min_length=1, description="工资单号")
    revision_no: int = Field(..., ge=1, description="修订序号，从 1 开始")
    department: str = Field(..., min_length=1, description="部门标识")
    amount_cents: int = Field(..., ge=0, description="金额（分，非负整数）；作废动作不参与计金额")
    action: RevisionAction = Field(..., description="入账 post / 作废 void")


class DepartmentTotal(BaseModel):
    """一个部门申报的应发总额。"""

    department: str = Field(..., min_length=1, description="部门标识")
    expected_total_cents: int = Field(..., ge=0, description="部门应发总额（分，非负整数）")


class ReconcileRequest(BaseModel):
    """月度工资单修订对账请求。"""

    pay_month: str = Field(
        ...,
        min_length=1,
        description="对账月份，如 2026-10；仅用于溯源标识，不参与计算",
    )
    revisions: list[SalaryRevision] = Field(
        ...,
        min_length=1,
        max_length=MAX_REVISIONS,
        description=f"同月工资单修订明细，最多 {MAX_REVISIONS} 条，可乱序，重传自动去重",
    )
    department_totals: list[DepartmentTotal] = Field(
        default_factory=list,
        description="各部门应发总额；未出现在此清单中的部门按缺少汇总处理，不得放行",
    )


class ReconciledDetail(BaseModel):
    """一条可入仓的工资单最终明细（仅属于核对通过部门）。"""

    payslip_id: str = Field(..., description="工资单号")
    revision_no: int = Field(..., description="最终生效的修订序号")
    department: str = Field(..., description="最终归属部门")
    amount_cents: int = Field(..., description="最终入账金额（分）；作废单不输出到可入仓明细")
    action: RevisionAction = Field(..., description="最终动作（可入仓明细恒为 post）")


class QuarantinedPayslip(BaseModel):
    """因序号缺口或同序号内容冲突被隔离的工资单。"""

    payslip_id: str = Field(..., description="工资单号")
    reasons: list[str] = Field(
        ...,
        description="隔离原因代码：sequence_gap（序号缺口）/ revision_conflict（同序号内容冲突）",
    )
    affected_departments: list[str] = Field(
        ..., description="该工资单历次修订涉及的全部部门（均不得放行）"
    )
    observed_revision_nos: list[int] = Field(
        ..., description="实际观测到的修订序号（升序，便于定位缺口）"
    )


class DepartmentReconcileResult(BaseModel):
    """单个部门的对账结论。"""

    department: str = Field(..., description="部门标识")
    status: Literal["matched", "mismatched", "quarantined", "missing_total"] = Field(
        ...,
        description=(
            "matched=核对通过可放行；mismatched=明细完整但总额不平；"
            "quarantined=相关工资单存在缺口/冲突，无法确定差额；"
            "missing_total=缺少应发汇总"
        ),
    )
    computed_total_cents: Optional[int] = Field(
        None, description="按最终状态重算的应发总额（分）；隔离部门为 null"
    )
    expected_total_cents: Optional[int] = Field(
        None, description="申报的应发总额（分）；缺少汇总时为 null"
    )
    variance_cents: Optional[int] = Field(
        None,
        description="差额（分）= 申报总额 - 重算总额；隔离或缺少汇总时为 null",
    )
    reason: Optional[str] = Field(
        None,
        description=(
            "未放行原因代码：amount_mismatch / department_quarantined / "
            "missing_department_total；通过时为 null"
        ),
    )
    detail_count: int = Field(0, description="该部门最终入账（post）的工资单条数")
    quarantined_payslip_ids: list[str] = Field(
        default_factory=list, description="导致该部门隔离的工资单号"
    )


class ReconcileResponse(BaseModel):
    """月度工资单修订对账响应。"""

    pay_month: str = Field(..., description="回传对账月份")
    revision_count: int = Field(..., description="去重后参与对账的修订条数")
    raw_revision_count: int = Field(..., description="请求中原始提交的修订条数（含重传）")
    duplicate_revision_count: int = Field(..., description="被识别为完全相同重传而丢弃的条数")
    passed_departments: list[str] = Field(
        ..., description="核对通过、可放行的部门清单（排序后）"
    )
    warehouse_details: list[ReconciledDetail] = Field(
        ..., description="可入仓的工资单最终明细，仅包含通过部门的入账单"
    )
    departments: list[DepartmentReconcileResult] = Field(
        ..., description="全部部门的对账结论（按部门名排序）"
    )
    quarantined_payslips: list[QuarantinedPayslip] = Field(
        ..., description="被隔离的工资单清单（按工资单号排序）"
    )
    all_passed: bool = Field(
        ..., description="是否所有涉及部门（含申报汇总的部门）均核对通过"
    )
