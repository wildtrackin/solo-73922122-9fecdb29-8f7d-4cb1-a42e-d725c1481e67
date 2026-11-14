"""对账引擎单元测试：去重、连续修订、缺口/冲突隔离、跨部门更正、缺汇总。"""

from __future__ import annotations

import pytest

from app.recon_models import (
    DepartmentTotal,
    ReconcileRequest,
    SalaryRevision,
)
from app.reconcile import dedup_revisions, reconcile


def rev(payslip_id: str, seq: int, dept: str, amount: int, action: str = "post") -> SalaryRevision:
    return SalaryRevision(
        payslip_id=payslip_id,
        revision_no=seq,
        department=dept,
        amount_cents=amount,
        action=action,  # type: ignore[arg-type]
    )


def make_request(revisions, totals=None, month="2026-10") -> ReconcileRequest:
    return ReconcileRequest(
        pay_month=month,
        revisions=list(revisions),
        department_totals=[
            DepartmentTotal(department=d, expected_total_cents=t) for d, t in (totals or [])
        ],
    )


class TestDedup:
    def test_exact_duplicates_counted_once(self):
        r1 = rev("P1", 1, "D1", 100)
        unique, dup = dedup_revisions([r1, r1.model_copy(deep=True), r1.model_copy(deep=True)])
        assert len(unique) == 1
        assert dup == 2

    def test_same_seq_different_content_not_duplicate(self):
        unique, dup = dedup_revisions([rev("P1", 1, "D1", 100), rev("P1", 1, "D1", 200)])
        assert len(unique) == 2
        assert dup == 0


class TestHappyPath:
    def test_single_department_matched(self):
        req = make_request(
            [rev("P1", 1, "D1", 1000), rev("P2", 1, "D1", 2000)],
            [("D1", 3000)],
        )
        resp = reconcile(req)
        assert resp.all_passed is True
        assert resp.passed_departments == ["D1"]
        assert len(resp.warehouse_details) == 2
        dept = resp.departments[0]
        assert dept.status == "matched"
        assert dept.computed_total_cents == 3000
        assert dept.variance_cents == 0
        assert dept.detail_count == 2

    def test_out_of_order_revisions_resolve_final_state(self):
        # 乱序：seq2 先到、seq1 后到，最终取 seq2
        req = make_request(
            [rev("P1", 2, "D1", 1500), rev("P1", 1, "D1", 1000)],
            [("D1", 1500)],
        )
        resp = reconcile(req)
        assert resp.passed_departments == ["D1"]
        detail = resp.warehouse_details[0]
        assert detail.revision_no == 2
        assert detail.amount_cents == 1500

    def test_void_excludes_amount(self):
        req = make_request(
            [
                rev("P1", 1, "D1", 1000),
                rev("P1", 2, "D1", 1000, action="void"),
                rev("P2", 1, "D1", 500),
            ],
            [("D1", 500)],
        )
        resp = reconcile(req)
        assert resp.passed_departments == ["D1"]
        assert len(resp.warehouse_details) == 1
        assert resp.warehouse_details[0].payslip_id == "P2"

    def test_zero_total_department_without_revisions_passes(self):
        # 只申报 0 汇总、无任何修订的部门也可对平
        req = make_request([rev("P1", 1, "D1", 100)], [("D1", 100), ("D2", 0)])
        resp = reconcile(req)
        assert set(resp.passed_departments) == {"D1", "D2"}
        d2 = next(d for d in resp.departments if d.department == "D2")
        assert d2.status == "matched"
        assert d2.computed_total_cents == 0


class TestMismatch:
    def test_amount_mismatch_reports_variance(self):
        req = make_request([rev("P1", 1, "D1", 1000)], [("D1", 1200)])
        resp = reconcile(req)
        assert resp.all_passed is False
        assert resp.passed_departments == []
        dept = resp.departments[0]
        assert dept.status == "mismatched"
        # 差额 = 申报 - 重算
        assert dept.variance_cents == 200
        assert dept.reason == "amount_mismatch"
        # 未通过部门的明细不得入仓
        assert resp.warehouse_details == []

    def test_negative_variance_when_computed_exceeds_expected(self):
        req = make_request([rev("P1", 1, "D1", 1500)], [("D1", 1000)])
        dept = reconcile(req).departments[0]
        assert dept.variance_cents == -500


class TestSequenceGap:
    def test_gap_quarantines_all_involved_departments(self):
        # P1 只有 seq1 和 seq3（缺 seq2），涉及 D1、D2 两个部门
        req = make_request(
            [
                rev("P1", 1, "D1", 1000),
                rev("P1", 3, "D2", 1200),
                rev("P2", 1, "D1", 500),  # D1 的其他完好明细也不能放行 D1
                rev("P3", 1, "D3", 700),
            ],
            [("D1", 1500), ("D2", 1200), ("D3", 700)],
        )
        resp = reconcile(req)
        q = next(x for x in resp.quarantined_payslips if x.payslip_id == "P1")
        assert q.reasons == ["sequence_gap"]
        assert set(q.affected_departments) == {"D1", "D2"}
        assert q.observed_revision_nos == [1, 3]

        by_dept = {d.department: d for d in resp.departments}
        assert by_dept["D1"].status == "quarantined"
        assert by_dept["D2"].status == "quarantined"
        assert by_dept["D1"].computed_total_cents is None
        assert by_dept["D1"].variance_cents is None
        assert by_dept["D1"].quarantined_payslip_ids == ["P1"]
        # D3 不受波及，正常通过
        assert by_dept["D3"].status == "matched"
        assert resp.passed_departments == ["D3"]
        assert [d.payslip_id for d in resp.warehouse_details] == ["P3"]

    def test_revision_not_starting_at_one_is_gap(self):
        req = make_request([rev("P1", 2, "D1", 1000)], [("D1", 1000)])
        resp = reconcile(req)
        assert resp.quarantined_payslips[0].reasons == ["sequence_gap"]
        assert resp.departments[0].status == "quarantined"


class TestRevisionConflict:
    def test_same_seq_conflicting_content_quarantines(self):
        req = make_request(
            [
                rev("P1", 1, "D1", 1000),
                rev("P1", 1, "D1", 2000),  # 同序号金额冲突
                rev("P1", 2, "D1", 1500),
            ],
            [("D1", 1500)],
        )
        resp = reconcile(req)
        q = resp.quarantined_payslips[0]
        assert q.reasons == ["revision_conflict"]
        assert resp.departments[0].status == "quarantined"
        assert resp.all_passed is False

    def test_conflict_and_gap_both_reported(self):
        req = make_request(
            [
                rev("P1", 1, "D1", 1000),
                rev("P1", 1, "D1", 2000),
                rev("P1", 4, "D2", 300),
            ],
            [("D1", 2000), ("D2", 300)],
        )
        q = reconcile(req).quarantined_payslips[0]
        assert set(q.reasons) == {"revision_conflict", "sequence_gap"}
        assert set(q.affected_departments) == {"D1", "D2"}

    def test_identical_resubmission_at_same_seq_is_not_conflict(self):
        req = make_request(
            [
                rev("P1", 1, "D1", 1000),
                rev("P1", 1, "D1", 1000),  # 完全相同重传
                rev("P1", 2, "D1", 1200),
            ],
            [("D1", 1200)],
        )
        resp = reconcile(req)
        assert resp.duplicate_revision_count == 1
        assert resp.quarantined_payslips == []
        assert resp.passed_departments == ["D1"]


class TestCrossDepartment:
    def test_cross_department_correction_both_sides_checked(self):
        # seq1 入 D1，seq2 改到 D2：D1 最终无该单金额，D2 承担最终金额
        req = make_request(
            [
                rev("P1", 1, "D1", 1000),
                rev("P1", 2, "D2", 1000),
                rev("P2", 1, "D1", 800),
            ],
            [("D1", 800), ("D2", 1000)],
        )
        resp = reconcile(req)
        by_dept = {d.department: d for d in resp.departments}
        assert by_dept["D1"].status == "matched"
        assert by_dept["D1"].computed_total_cents == 800
        assert by_dept["D2"].status == "matched"
        assert by_dept["D2"].computed_total_cents == 1000
        ids = {(d.payslip_id, d.department) for d in resp.warehouse_details}
        assert ids == {("P1", "D2"), ("P2", "D1")}

    def test_cross_department_void_removes_from_both(self):
        req = make_request(
            [
                rev("P1", 1, "D1", 1000),
                rev("P1", 2, "D2", 1000, action="void"),
            ],
            [("D1", 0), ("D2", 0)],
        )
        resp = reconcile(req)
        assert resp.passed_departments == ["D1", "D2"]
        assert resp.warehouse_details == []

    def test_quarantine_follows_cross_department_history(self):
        # 跨部门修订后又出现缺口，新旧部门都要隔离
        req = make_request(
            [
                rev("P1", 1, "D1", 1000),
                rev("P1", 2, "D2", 1000),
                rev("P1", 4, "D2", 900),
            ],
            [("D1", 0), ("D2", 900)],
        )
        resp = reconcile(req)
        q = resp.quarantined_payslips[0]
        assert set(q.affected_departments) == {"D1", "D2"}
        assert {d.department for d in resp.departments if d.status == "quarantined"} == {
            "D1",
            "D2",
        }


class TestMissingTotal:
    def test_department_without_total_not_released(self):
        req = make_request([rev("P1", 1, "D1", 1000), rev("P2", 1, "D2", 500)], [("D1", 1000)])
        resp = reconcile(req)
        assert resp.passed_departments == ["D1"]
        d2 = next(d for d in resp.departments if d.department == "D2")
        assert d2.status == "missing_total"
        assert d2.reason == "missing_department_total"
        assert d2.expected_total_cents is None
        assert d2.computed_total_cents == 500
        assert d2.variance_cents is None
        # 缺汇总部门的明细同样不能入仓
        assert {d.department for d in resp.warehouse_details} == {"D1"}
        assert resp.all_passed is False


class TestSampleScenario:
    def test_builtin_sample_matches_expected_outcome(self):
        from app.samples import build_sample_reconcile_request

        resp = reconcile(build_sample_reconcile_request())
        statuses = {d.department: d.status for d in resp.departments}
        assert statuses == {
            "D_A": "matched",
            "D_B": "quarantined",
            "D_C": "quarantined",
            "D_D": "matched",
            "D_E": "matched",
            "D_F": "missing_total",
        }
        assert resp.duplicate_revision_count == 1
        assert resp.revision_count == 12
        assert resp.raw_revision_count == 13
        assert resp.passed_departments == ["D_A", "D_D", "D_E"]
        quarantined_ids = {q.payslip_id for q in resp.quarantined_payslips}
        assert quarantined_ids == {"PS-202610-004", "PS-202610-005"}
        # 可入仓明细只含通过部门的最终入账单
        assert {(d.payslip_id, d.department) for d in resp.warehouse_details} == {
            ("PS-202610-001", "D_A"),
            ("PS-202610-006", "D_D"),
        }
        assert resp.all_passed is False
