"""Comprehensive integration tests for newly added AI Model Selection & Mapping Wizard E2E flows."""

from __future__ import annotations

import httpx
from fastapi.testclient import TestClient

from billapp.app import create_app


def test_unknown_csv_custom_profile_autodetect_and_suggest(tmp_path):
    """Uploading unknown CSV triggers need_mapping; saving custom YAML profile enables auto-detect."""
    db_file = tmp_path / "goal_mapping.db"
    app = create_app(db_path=str(db_file))
    unknown_csv = (
        "\r\n".join(
            [
                "未来数字银行账户交易导出表",
                "持卡人：测试用户 | 尾号：7788",
                "记账年月日,时刻点,款项金额,余留资金,交易对方名称,业务摘要备注",
                "2026-11-07,09:30:00,-45.00,2000.00,蓝蛙西餐厅,工作日早餐",
                "2026-11-07,18:45:00,320.00,2320.00,某设计工作室,兼职报酬入账",
            ]
        )
        + "\r\n"
    ).encode("utf-8-sig")

    with TestClient(app) as client:
        # 1. Unknown CSV requires mapping initially
        p1 = client.post(
            "/api/import/preview",
            files={"file": ("future_bank.csv", unknown_csv, "text/csv")},
        )
        assert p1.status_code == 200
        assert p1.json()["need_mapping"] is True

        # 2. Heuristic column suggestion finds exact columns
        sug = client.post(
            "/api/profiles/suggest",
            json={
                "headers": ["记账年月日", "时刻点", "款项金额", "余留资金", "交易对方名称", "业务摘要备注"],
                "sample_rows": [["2026-11-07", "09:30:00", "-45.00", "2000.00", "蓝蛙西餐厅", "工作日早餐"]],
            },
        )
        assert sug.status_code == 200
        cm = sug.json()["column_mapping"]
        assert cm["date"] == "记账年月日"
        assert cm["amount"] == "款项金额"
        assert cm["counterparty"] == "交易对方名称"

        # 3. Save custom profile (without requiring bank name match in preamble)
        yaml_content = """id: "future_bank_custom"
name: "未来数字银行导出模板"
bank: "自定义机构"
account_kind: "debit"
file_type: "auto"
currency: "CNY"
table_detection:
  header_row_index: 2
  data_start_offset: 1
  header_row_keywords:
    - "记账年月日"
    - "款项金额"
    - "交易对方名称"
    - "业务摘要备注"
column_mapping:
  date: "记账年月日"
  amount: "款项金额"
  counterparty: "交易对方名称"
  description: "业务摘要备注"
amount_mode:
  type: "signed"
  amount_col: "款项金额"
"""
        cp = client.post(
            "/api/profiles",
            json={
                "id": "future_bank_custom",
                "name": "未来数字银行导出模板",
                "yaml_content": yaml_content,
            },
        )
        assert cp.status_code == 201

        # 4. Re-upload SAME unknown CSV WITHOUT passing profile_id -> auto-detected!
        p2 = client.post(
            "/api/import/preview",
            files={"file": ("future_bank.csv", unknown_csv, "text/csv")},
        )
        assert p2.status_code == 200
        d2 = p2.json()
        assert d2["need_mapping"] is False
        assert d2["total_rows"] == 2
        assert d2["token"]

        # 5. Commit & verify rows persisted
        cm_res = client.post("/api/import/commit", json={"token": d2["token"]})
        assert cm_res.status_code == 200
        assert cm_res.json()["imported_count"] == 2


def test_llm_models_endpoint_with_saved_settings(monkeypatch, tmp_path):
    """POST /api/llm/models falls back to saved DB Base URL & encrypted API Key."""
    db_file = tmp_path / "goal_llm_models.db"
    app = create_app(db_path=str(db_file))

    captured = {}

    def fake_list_models(self):
        captured["base_url"] = self.config.base_url
        captured["api_key"] = self.config.api_key
        return {
            "ok": True,
            "models": [
                {"id": "Qwen3.8-Flash", "name": "Qwen3.8-Flash", "owned_by": "qwen", "description": ""},
                {"id": "DeepSeek-V4-Pro", "name": "DeepSeek-V4-Pro", "owned_by": "deepseek", "description": ""},
            ],
            "error": None,
        }

    monkeypatch.setattr("billapp.llm.client.LLMClient.list_models", fake_list_models)

    with TestClient(app) as client:
        # Save encrypted settings first
        put_res = client.put(
            "/api/settings",
            json={
                "llm": {
                    "base_url": "http://localhost:8790/v1",
                    "api_key": "secret-key-xyz",
                    "model": "Qwen3.8-Flash",
                    "enabled": True,
                }
            },
        )
        assert put_res.status_code == 200

        # Empty body should load saved base_url & api_key from DB
        res = client.post("/api/llm/models", json={})
        assert res.status_code == 200
        body = res.json()
        assert body["ok"] is True
        assert [m["id"] for m in body["models"]] == ["Qwen3.8-Flash", "DeepSeek-V4-Pro"]
        assert captured["base_url"] == "http://localhost:8790/v1"
        assert captured["api_key"] == "secret-key-xyz"
