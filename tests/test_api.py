"""FastAPI 路由层测试（不连数据库，使用 TestClient）。"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app
from app.samples import SAMPLE_TABLES

client = TestClient(app)


def payload(sql: str) -> dict:
    return {
        "sql": sql,
        "tables": {
            name: {"columns": [c.model_dump() for c in meta.columns]}
            for name, meta in SAMPLE_TABLES.items()
        },
    }


class TestEndpoints:
    def test_health(self):
        assert client.get("/health").json() == {"status": "ok"}

    def test_docs_available(self):
        r = client.get("/docs")
        assert r.status_code == 200
        assert "swagger" in r.text.lower() or "openapi" in r.text.lower()

    def test_openapi_has_endpoint(self):
        spec = client.get("/openapi.json").json()
        assert "/api/v1/lineage" in spec["paths"]

    def test_lineage_basic(self):
        r = client.post(
            "/api/v1/lineage",
            json=payload("SELECT employee_id, base_salary FROM pay_salary"),
        )
        assert r.status_code == 200
        data = r.json()
        assert data["incomplete"] is False
        sens = {c["output_name"]: c["sensitive"] for c in data["output_columns"]}
        assert sens == {"employee_id": False, "base_salary": True}

    def test_lineage_incomplete_still_200(self):
        r = client.post("/api/v1/lineage", json=payload("SELECT x FROM no_such_table"))
        assert r.status_code == 200
        data = r.json()
        assert data["incomplete"] is True
        assert data["output_columns"][0]["sensitive"] is None
        assert data["issues"]

    def test_empty_sql_rejected(self):
        r = client.post("/api/v1/lineage", json=payload("   "))
        assert r.status_code == 422

    def test_parse_error_located(self):
        r = client.post("/api/v1/lineage", json=payload("SELECT FROM WHERE"))
        data = r.json()
        assert data["incomplete"] is True
        assert data["issues"][0]["code"] == "parse_error"
        assert data["issues"][0]["location"]

    def test_sample_endpoints(self):
        tables = client.get("/api/v1/samples/tables").json()
        assert "pay_salary" in tables and "hr_employee" in tables

        sql = client.get("/api/v1/samples/sql/nested").json()["sql"]
        assert "WITH" in sql

        for kind in ("nested", "union_all", "incomplete"):
            r = client.post(f"/api/v1/samples/lineage/{kind}")
            assert r.status_code == 200
            assert r.json()["output_columns"]

        assert client.get("/api/v1/samples/sql/nope").status_code == 404

    def test_nested_sample_unused_cte(self):
        r = client.post("/api/v1/samples/lineage/nested").json()
        assert r["incomplete"] is False
        assert "unreferenced_cte" in r["unused_ctes"]
        # 敏感薪资列穿透多层派生表后仍为敏感
        payroll = next(c for c in r["output_columns"] if c["output_name"] == "dept_payroll_sum")
        assert payroll["sensitive"] is True

    def test_union_all_sample_merges_sources(self):
        r = client.post("/api/v1/samples/lineage/union_all").json()
        col = next(c for c in r["output_columns"] if c["output_name"] == "base_salary")
        assert {s["table"] for s in col["sources"]} == {
            "pay_salary",
            "pay_salary_archive",
        }


class TestReconcileApi:
    def _payload(self, **overrides):
        payload = {
            "pay_month": "2026-10",
            "revisions": [
                {
                    "payslip_id": "P1",
                    "revision_no": 1,
                    "department": "D1",
                    "amount_cents": 1000,
                    "action": "post",
                },
                {
                    "payslip_id": "P1",
                    "revision_no": 2,
                    "department": "D1",
                    "amount_cents": 1200,
                    "action": "post",
                },
            ],
            "department_totals": [
                {"department": "D1", "expected_total_cents": 1200},
            ],
        }
        payload.update(overrides)
        return payload

    def test_openapi_includes_reconcile_endpoint(self):
        spec = client.get("/openapi.json").json()
        assert "/api/v1/payroll/reconcile" in spec["paths"]

    def test_reconcile_matched(self):
        r = client.post("/api/v1/payroll/reconcile", json=self._payload())
        assert r.status_code == 200
        data = r.json()
        assert data["passed_departments"] == ["D1"]
        assert data["all_passed"] is True
        assert data["warehouse_details"][0]["revision_no"] == 2
        assert data["warehouse_details"][0]["amount_cents"] == 1200

    def test_reconcile_gap_quarantines(self):
        revisions = [
            {"payslip_id": "P1", "revision_no": 1, "department": "D1",
             "amount_cents": 1000, "action": "post"},
            {"payslip_id": "P1", "revision_no": 3, "department": "D2",
             "amount_cents": 1000, "action": "post"},
        ]
        r = client.post(
            "/api/v1/payroll/reconcile",
            json=self._payload(
                revisions=revisions,
                department_totals=[
                    {"department": "D1", "expected_total_cents": 1000},
                    {"department": "D2", "expected_total_cents": 1000},
                ],
            ),
        )
        data = r.json()
        assert data["quarantined_payslips"][0]["reasons"] == ["sequence_gap"]
        assert {d["department"] for d in data["departments"]} == {"D1", "D2"}
        assert all(d["status"] == "quarantined" for d in data["departments"])
        assert data["warehouse_details"] == []

    def test_reconcile_missing_total_rejected_from_release(self):
        revisions = [
            {"payslip_id": "P1", "revision_no": 1, "department": "D9",
             "amount_cents": 1000, "action": "post"},
        ]
        r = client.post(
            "/api/v1/payroll/reconcile",
            json=self._payload(revisions=revisions, department_totals=[]),
        )
        data = r.json()
        assert data["departments"][0]["status"] == "missing_total"
        assert data["passed_departments"] == []

    def test_over_500_revisions_rejected(self):
        revisions = [
            {
                "payslip_id": f"P{i}",
                "revision_no": 1,
                "department": "D1",
                "amount_cents": 1,
                "action": "post",
            }
            for i in range(501)
        ]
        r = client.post(
            "/api/v1/payroll/reconcile",
            json=self._payload(revisions=revisions),
        )
        assert r.status_code == 422

    def test_invalid_action_rejected(self):
        bad = self._payload()
        bad["revisions"][0]["action"] = "delete"
        assert client.post("/api/v1/payroll/reconcile", json=bad).status_code == 422

    def test_negative_amount_rejected(self):
        bad = self._payload()
        bad["revisions"][0]["amount_cents"] = -1
        assert client.post("/api/v1/payroll/reconcile", json=bad).status_code == 422

    def test_revision_zero_rejected(self):
        bad = self._payload()
        bad["revisions"][0]["revision_no"] = 0
        assert client.post("/api/v1/payroll/reconcile", json=bad).status_code == 422

    def test_empty_revisions_rejected(self):
        r = client.post(
            "/api/v1/payroll/reconcile",
            json=self._payload(revisions=[]),
        )
        assert r.status_code == 422

    def test_duplicate_department_total_rejected(self):
        payload = self._payload(
            department_totals=[
                {"department": "D1", "expected_total_cents": 1200},
                {"department": "D1", "expected_total_cents": 1100},
            ]
        )
        r = client.post("/api/v1/payroll/reconcile", json=payload)
        assert r.status_code == 422

    def test_sample_reconcile_endpoints(self):
        payload = client.get("/api/v1/samples/reconcile/payload")
        assert payload.status_code == 200
        assert payload.json()["pay_month"] == "2026-10"

        r = client.post("/api/v1/samples/reconcile")
        assert r.status_code == 200
        data = r.json()
        assert data["passed_departments"] == ["D_A", "D_D", "D_E"]
        assert data["duplicate_revision_count"] == 1
        assert {q["payslip_id"] for q in data["quarantined_payslips"]} == {
            "PS-202610-004",
            "PS-202610-005",
        }

    def test_lineage_endpoint_still_available(self):
        # 保留原溯源 API
        spec = client.get("/openapi.json").json()
        assert "/api/v1/lineage" in spec["paths"]
        r = client.post(
            "/api/v1/lineage",
            json=payload("SELECT employee_id FROM pay_salary"),
        )
        assert r.status_code == 200
