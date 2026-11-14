"""工资单修订对账引擎（纯函数式，无 IO、无数据库依赖）。

处理流程：

1. 去重：内容完全一致的重传修订只保留一条；
2. 按工资单号分组，按修订序号排序；
3. 完整性校验：
   - 同序号出现不同内容（部门 / 金额 / 动作不一致）→ revision_conflict；
   - 序号未从 1 开始或中间出现缺口 → sequence_gap；
4. 隔离：凡命中上述问题的工资单，其**历次修订涉及的全部部门**整体隔离，
   这些部门不能再用剩余明细参与对账；
5. 对完好工资单取最大序号修订作为最终状态：void 不计金额，post 计入最终部门；
6. 逐部门重算应发总额，与申报汇总核对；缺少汇总的部门不得放行。
"""

from __future__ import annotations

from collections import OrderedDict, defaultdict
from typing import Iterable

from .recon_models import (
    DepartmentReconcileResult,
    DepartmentTotal,
    QuarantinedPayslip,
    ReconcileRequest,
    ReconcileResponse,
    ReconciledDetail,
    SalaryRevision,
)


def _dedup_key(rev: SalaryRevision) -> tuple:
    return (
        rev.payslip_id,
        rev.revision_no,
        rev.department,
        rev.amount_cents,
        rev.action,
    )


def dedup_revisions(
    revisions: Iterable[SalaryRevision],
) -> tuple[list[SalaryRevision], int]:
    """去除完全相同的重传，保留首次出现顺序，返回 (去重后明细, 重传条数)。"""
    seen: set[tuple] = set()
    unique: list[SalaryRevision] = []
    duplicates = 0
    for rev in revisions:
        key = _dedup_key(rev)
        if key in seen:
            duplicates += 1
            continue
        seen.add(key)
        unique.append(rev)
    return unique, duplicates


def _check_payslip(
    payslip_id: str,
    revisions: list[SalaryRevision],
) -> tuple[set[str], set[str], list[int]]:
    """检查单个工资单的修订链。

    返回 (问题代码集合, 历次涉及部门集合, 观测到的序号升序列表)。
    同序号内容冲突与序号缺口各自独立判定，可同时命中。
    """
    reasons: set[str] = set()
    departments = {rev.department for rev in revisions}

    # 同序号分组：同一序号出现多个不同内容即冲突
    by_seq: dict[int, list[SalaryRevision]] = defaultdict(list)
    for rev in revisions:
        by_seq[rev.revision_no].append(rev)
    for seq, group in by_seq.items():
        signatures = {(r.department, r.amount_cents, r.action) for r in group}
        if len(signatures) > 1:
            reasons.add("revision_conflict")

    observed = sorted(by_seq.keys())
    # 序号必须从 1 开始且连续（缺口含未从 1 开始的情形）
    if observed != list(range(1, len(observed) + 1)):
        reasons.add("sequence_gap")

    return reasons, departments, observed


def reconcile(request: ReconcileRequest) -> ReconcileResponse:
    """执行月度工资单修订对账，返回逐部门结论与可入仓明细。"""
    unique_revisions, duplicate_count = dedup_revisions(request.revisions)

    # 按工资单号归集（保持首次出现顺序，输出更稳定）
    grouped: dict[str, list[SalaryRevision]] = OrderedDict()
    for rev in unique_revisions:
        grouped.setdefault(rev.payslip_id, []).append(rev)

    # ---- 1. 完整性校验与隔离 ------------------------------------------------
    quarantined: list[QuarantinedPayslip] = []
    # 被隔离工资单波及的部门 -> 导致隔离的工资单号
    quarantined_departments: dict[str, list[str]] = defaultdict(list)

    # 完好工资单的最终状态
    final_revisions: dict[str, SalaryRevision] = {}

    for payslip_id, revisions in grouped.items():
        reasons, departments, observed = _check_payslip(payslip_id, revisions)
        if reasons:
            quarantined.append(
                QuarantinedPayslip(
                    payslip_id=payslip_id,
                    reasons=sorted(reasons),
                    affected_departments=sorted(departments),
                    observed_revision_nos=observed,
                )
            )
            for dept in departments:
                quarantined_departments[dept].append(payslip_id)
            continue
        # 修订链完好：最大序号即最终状态（序号连续，取 max 最直观）
        final_revisions[payslip_id] = max(revisions, key=lambda r: r.revision_no)

    # ---- 2. 按最终状态重算各部门应发总额（作废不计金额） -------------------
    computed_totals: dict[str, int] = defaultdict(int)
    detail_counts: dict[str, int] = defaultdict(int)
    warehouse_details: list[ReconciledDetail] = []
    for payslip_id, rev in final_revisions.items():
        if rev.action == "void":
            continue
        computed_totals[rev.department] += rev.amount_cents
        detail_counts[rev.department] += 1
        warehouse_details.append(
            ReconciledDetail(
                payslip_id=payslip_id,
                revision_no=rev.revision_no,
                department=rev.department,
                amount_cents=rev.amount_cents,
                action="post",
            )
        )

    # ---- 3. 汇总申报：校验重复部门并建索引 ---------------------------------
    expected_totals = _index_totals(request.department_totals)

    # 全部相关部门 = 修订中出现过的部门 ∪ 申报汇总中的部门
    all_departments = set(computed_totals) | set(quarantined_departments) | set(expected_totals)

    # ---- 4. 逐部门给出结论 --------------------------------------------------
    results: list[DepartmentReconcileResult] = []
    passed: list[str] = []
    for dept in sorted(all_departments):
        if dept in quarantined_departments:
            results.append(
                DepartmentReconcileResult(
                    department=dept,
                    status="quarantined",
                    computed_total_cents=None,
                    expected_total_cents=expected_totals.get(dept),
                    variance_cents=None,
                    reason="department_quarantined",
                    detail_count=0,
                    quarantined_payslip_ids=sorted(quarantined_departments[dept]),
                )
            )
            continue

        computed = computed_totals.get(dept, 0)
        if dept not in expected_totals:
            # 明细存在（或仅修订中出现）却缺少汇总：不得放行
            results.append(
                DepartmentReconcileResult(
                    department=dept,
                    status="missing_total",
                    computed_total_cents=computed,
                    expected_total_cents=None,
                    variance_cents=None,
                    reason="missing_department_total",
                    detail_count=detail_counts.get(dept, 0),
                )
            )
            continue

        expected = expected_totals[dept]
        variance = expected - computed
        if variance == 0:
            results.append(
                DepartmentReconcileResult(
                    department=dept,
                    status="matched",
                    computed_total_cents=computed,
                    expected_total_cents=expected,
                    variance_cents=0,
                    reason=None,
                    detail_count=detail_counts.get(dept, 0),
                )
            )
            passed.append(dept)
        else:
            results.append(
                DepartmentReconcileResult(
                    department=dept,
                    status="mismatched",
                    computed_total_cents=computed,
                    expected_total_cents=expected,
                    variance_cents=variance,
                    reason="amount_mismatch",
                    detail_count=detail_counts.get(dept, 0),
                )
            )

    # 可入仓明细只保留通过部门的入账单，并按部门、工资单号排序保证稳定
    passed_set = set(passed)
    warehouse_details = sorted(
        (d for d in warehouse_details if d.department in passed_set),
        key=lambda d: (d.department, d.payslip_id),
    )
    quarantined.sort(key=lambda q: q.payslip_id)

    return ReconcileResponse(
        pay_month=request.pay_month,
        revision_count=len(unique_revisions),
        raw_revision_count=len(request.revisions),
        duplicate_revision_count=duplicate_count,
        passed_departments=sorted(passed),
        warehouse_details=warehouse_details,
        departments=results,
        quarantined_payslips=quarantined,
        all_passed=bool(all_departments) and len(passed) == len(all_departments),
    )


def _index_totals(totals: list[DepartmentTotal]) -> dict[str, int]:
    """将部门汇总列表转成映射；同部门重复申报直接判为请求非法。"""
    index: dict[str, int] = {}
    for item in totals:
        if item.department in index:
            raise ValueError(f"部门 {item.department} 的应发总额重复申报")
        index[item.department] = item.expected_total_cents
    return index
