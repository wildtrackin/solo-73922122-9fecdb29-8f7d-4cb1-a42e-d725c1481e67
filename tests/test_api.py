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
