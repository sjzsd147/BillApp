"""Regression tests for bill import audit fixes and duplicate-batch re-import."""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path
from typing import List

from fastapi.testclient import TestClient
import pymupdf
import pytest

from billapp.app import create_app
from billapp.core.db import create_connection
from billapp.dedup.transfer import compute_transfer_score
from billapp.importers.base import FileProbe
from billapp.importers.boc_debit import BOCDebitImporter
from billapp.importers.icbc_credit import ICBCCreditImporter
from billapp.importers.pdf import open_pdf_document
from billapp.importers.profile_engine import BankProfile, ProfileImporter

try:
    from backend.tests.fixtures.synthetic_alipay import AlipayRow, generate_synthetic_alipay_csv
    from backend.tests.fixtures.synthetic_pdf import generate_synthetic_icbc_pdf
    from backend.tests.fixtures.synthetic_wechat import WeChatRow, generate_synthetic_wechat_xlsx
except ImportError:
    from tests.fixtures.synthetic_alipay import AlipayRow, generate_synthetic_alipay_csv
    from tests.fixtures.synthetic_pdf import generate_synthetic_icbc_pdf
    from tests.fixtures.synthetic_wechat import WeChatRow, generate_synthetic_wechat_xlsx


@pytest.fixture()
def client(tmp_path: Path):
    db_path = tmp_path / "audit_fixes.db"
    app = create_app(db_path=db_path)
    with TestClient(app) as tc:
        yield tc


def test_alipay_partial_refund_not_double_counted_and_note_persisted(client: TestClient):
    """Alipay partial refund must only be credited once, and original note must be saved."""
    csv_bytes = generate_synthetic_alipay_csv(
        rows=[
            AlipayRow(
                txn_time="2026-10-16 12:00:00",
                category="日用百货",
                counterparty="京东商城",
                account="jd@example.com",
                description="日用品组合装",
                direction="支出",
                amount=100.00,
                pay_method="工商银行信用卡(8801)",
                status="交易成功",
                order_id="2026101600000000000000000999",
                merchant_order_id="JD999",
                note="家庭采购备注",
            ),
            AlipayRow(
                txn_time="2026-10-17 09:30:00",
                category="退款",
                counterparty="京东商城",
                account="jd@example.com",
                description="部分商品退款",
                direction="不计收支",
                amount=30.00,
                pay_method="工商银行信用卡(8801)",
                status="退款成功",
                order_id="2026101600000000000000000999*F-refund-001",
                merchant_order_id="JD999R",
                note="退货其中一件",
            ),
        ]
    )

    prev = client.post(
        "/api/import/preview",
        files={"file": ("alipay_partial_refund.csv", io.BytesIO(csv_bytes), "text/csv")},
    )
    assert prev.status_code == 200
    token = prev.json()["token"]

    commit = client.post("/api/import/commit", json={"token": token})
    assert commit.status_code == 200
    assert commit.json()["imported_count"] == 2
    assert commit.json()["refund_count"] == 1

    with create_connection(client.app.state.db_path) as conn:
        orig = conn.execute(
            "SELECT id, amount_cents, refunded_cents, note, pay_method FROM transactions WHERE direction = 'expense'"
        ).fetchone()
        ref = conn.execute(
            "SELECT id, amount_cents, refund_of, note FROM transactions WHERE direction = 'refund'"
        ).fetchone()
        assert orig is not None and ref is not None
        assert orig["refunded_cents"] == 3000  # Must NOT be 6000
        assert ref["refund_of"] == orig["id"]
        assert orig["note"] == "家庭采购备注"
        assert ref["note"] == "退货其中一件"
        assert orig["pay_method"] == "工商银行信用卡(8801)"


def test_wechat_normal_status_refund_linked_and_pay_method_preserved(client: TestClient):
    """WeChat refund against normal-status expense must link refund_of and preserve pay_method + note."""
    xlsx_bytes = generate_synthetic_wechat_xlsx(
        rows=[
            WeChatRow(
                txn_time="2026-11-05 18:00:00",
                txn_type="商户消费",
                counterparty="瑞幸咖啡",
                product="生椰拿铁",
                direction="支出",
                amount=24.00,
                pay_method="招商银行信用卡(1234)",
                status="支付成功",
                order_id="4200001000202611050001",
                merchant_order_id="LK001",
                note="下午茶备注",
            ),
            WeChatRow(
                txn_time="2026-11-05 18:05:00",
                txn_type="商户消费-退款",
                counterparty="瑞幸咖啡",
                product="生椰拿铁退款",
                direction="收入",
                amount=24.00,
                pay_method="招商银行信用卡(1234)",
                status="已全额退款",
                order_id="5010001000202611050001",
                merchant_order_id="LK001R",
                note="门店缺货退款",
            ),
        ]
    )

    prev = client.post(
        "/api/import/preview",
        files={"file": ("wechat_refund.xlsx", io.BytesIO(xlsx_bytes), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
    )
    assert prev.status_code == 200
    token = prev.json()["token"]

    commit = client.post("/api/import/commit", json={"token": token})
    assert commit.status_code == 200
    assert commit.json()["refund_count"] == 1

    with create_connection(client.app.state.db_path) as conn:
        orig = conn.execute(
            "SELECT id, refunded_cents, pay_method, note FROM transactions WHERE direction = 'expense'"
        ).fetchone()
        ref = conn.execute(
            "SELECT id, refund_of, note FROM transactions WHERE direction = 'refund'"
        ).fetchone()
        assert orig is not None and ref is not None
        assert orig["refunded_cents"] == 2400
        assert ref["refund_of"] == orig["id"]
        assert orig["pay_method"] == "招商银行信用卡(1234)"
        assert orig["note"] == "下午茶备注"
        assert ref["note"] == "门店缺货退款"


def test_duplicate_batch_reimport_replaces_old_batch_and_preserves_manual_category(client: TestClient):
    """Re-importing a file with identical SHA-1 re-parses, replaces old batch, and preserves manual edits."""
    csv_bytes = generate_synthetic_alipay_csv(
        rows=[
            AlipayRow(
                txn_time="2026-11-01 12:15:00",
                category="餐饮美食",
                counterparty="星巴克",
                account="sb@example.com",
                description="美式咖啡",
                direction="支出",
                amount=32.00,
                pay_method="余额宝",
                status="交易成功",
                order_id="2026110100001",
                merchant_order_id="SB001",
            ),
            AlipayRow(
                txn_time="2026-11-02 08:30:00",
                category="交通出行",
                counterparty="滴滴出行",
                account="dd@example.com",
                description="快车",
                direction="支出",
                amount=18.50,
                pay_method="余额宝",
                status="交易成功",
                order_id="2026110200002",
                merchant_order_id="DD002",
            ),
        ]
    )

    # First import
    p1 = client.post("/api/import/preview", files={"file": ("dup_batch.csv", csv_bytes, "text/csv")})
    assert p1.status_code == 200
    assert p1.json()["existing_batch_id"] is None
    c1 = client.post("/api/import/commit", json={"token": p1.json()["token"]})
    assert c1.status_code == 200
    batch1 = c1.json()["batch_id"]
    assert c1.json()["replaced_batch_id"] is None

    # Manually set category & excluded flag on first transaction
    with create_connection(client.app.state.db_path) as conn:
        manual_cat = conn.execute("SELECT id FROM categories WHERE key = 'entertainment.gaming'").fetchone()
        assert manual_cat is not None
        cat_id = manual_cat["id"]
        conn.execute(
            "UPDATE transactions SET category_id = ?, category_source = 'manual', category_confidence = 1.0, excluded = 1 WHERE order_id = '2026110100001'",
            (cat_id,),
        )
        conn.commit()

    # Second import of exact same file -> re-parse + replace
    p2 = client.post("/api/import/preview", files={"file": ("dup_batch.csv", csv_bytes, "text/csv")})
    assert p2.status_code == 200
    p2_data = p2.json()
    assert p2_data["existing_batch_id"] == batch1
    assert any("原批次" in w for w in p2_data["warnings"])

    c2 = client.post("/api/import/commit", json={"token": p2_data["token"]})
    assert c2.status_code == 200
    c2_data = c2.json()
    assert c2_data["replaced_batch_id"] == batch1
    assert c2_data["batch_id"] != batch1
    assert c2_data["imported_count"] == 2

    with create_connection(client.app.state.db_path) as conn:
        batches = conn.execute("SELECT id, row_count FROM import_batches").fetchall()
        assert len(batches) == 1
        assert batches[0]["id"] == c2_data["batch_id"]
        assert batches[0]["row_count"] == 2

        tx1 = conn.execute(
            "SELECT category_id, category_source, excluded FROM transactions WHERE order_id = '2026110100001'"
        ).fetchone()
        assert tx1["category_id"] == cat_id
        assert tx1["category_source"] == "manual"
        assert tx1["excluded"] == 1


def test_multicard_icbc_override_preserves_other_card_tails(client: TestClient):
    """Overriding primary card metadata on a multi-card ICBC PDF must not clobber the other card's tail."""
    pdf_bytes = generate_synthetic_icbc_pdf(password="icbc123")
    prev = client.post(
        "/api/import/preview",
        files={"file": ("icbc_multi.pdf", io.BytesIO(pdf_bytes), "application/pdf")},
        data={"password": "icbc123"},
    )
    assert prev.status_code == 200
    p_data = prev.json()
    assert len(p_data["accounts"]) >= 2
    first_acc = p_data["accounts"][0]

    commit = client.post(
        "/api/import/commit",
        json={
            "token": p_data["token"],
            "account_overrides": {
                "bank": first_acc["bank"],
                "tail": first_acc["tail"],
                "kind": first_acc["kind"],
                "holder": "李四",
            },
        },
    )
    assert commit.status_code == 200

    with create_connection(client.app.state.db_path) as conn:
        tails = {
            r["tail"]
            for r in conn.execute(
                "SELECT DISTINCT a.tail FROM transactions t JOIN accounts a ON t.account_id = a.id"
            ).fetchall()
        }
        assert len(tails) >= 2


def test_transfer_pattern_b_platform_credit_card_repay():
    """WeChat/Alipay repayment to a bank_credit account must score via Pattern B (credit_card_repay)."""
    out_txn = {
        "id": 1,
        "source": "wechat",
        "txn_time": "2026-11-05 10:00:00",
        "amount_cents": 200000,
        "currency": "CNY",
        "direction": "transfer",
        "counterparty": "信用卡还款",
        "description": "工商银行信用卡还款",
        "status": "已到账",
        "acc_kind": "wallet",
        "acc_bank": "微信支付",
        "acc_tail": None,
    }
    in_txn = {
        "id": 2,
        "source": "icbc_credit",
        "txn_time": "2026-11-05 10:02:00",
        "amount_cents": 200000,
        "currency": "CNY",
        "direction": "transfer",
        "counterparty": "微信还款",
        "description": "跨行还款",
        "status": "交易成功",
        "acc_kind": "bank_credit",
        "acc_bank": "工商银行",
        "acc_tail": "8801",
    }
    score, features = compute_transfer_score(out_txn, in_txn)
    assert features.get("pattern") == "credit_card_repay"
    assert score >= 0.90


def test_review_api_reads_llm_arbitration_from_features_and_columns(client: TestClient):
    """GET /api/review must expose LLM verdict/reason/confidence from both columns and features_json."""
    with create_connection(client.app.state.db_path) as conn:
        a1 = conn.execute("INSERT INTO accounts (kind, bank, tail) VALUES ('platform', '支付宝', '0001')").lastrowid
        a2 = conn.execute("INSERT INTO accounts (kind, bank, tail) VALUES ('bank_debit', '中国银行', '6666')").lastrowid
        b1 = conn.execute(
            "INSERT INTO import_batches (file_name, file_sha1, file_size, source) VALUES ('a.csv', 'sha_a', 10, 'alipay')"
        ).lastrowid
        t1 = conn.execute(
            """
            INSERT INTO transactions (batch_id, source, account_id, txn_time, time_precision, amount_cents, direction, fingerprint)
            VALUES (?, 'alipay', ?, '2026-11-01 12:00:00', 'second', 5000, 'expense', 'fp1')
            """,
            (b1, a1),
        ).lastrowid
        t2 = conn.execute(
            """
            INSERT INTO transactions (batch_id, source, account_id, txn_time, time_precision, amount_cents, direction, fingerprint)
            VALUES (?, 'boc_debit', ?, '2026-11-01 12:01:00', 'second', 5000, 'expense', 'fp2')
            """,
            (b1, a2),
        ).lastrowid
        feat = {
            "breakdown": {"amount": 0.3, "tail": 0.1, "time": 0.3, "channel": 0.0, "name": 0.0, "penalty": 0.0},
            "llm_arbitration": {"verdict": "same", "confidence": 0.92, "reason": "同一笔商户消费"},
        }
        conn.execute(
            """
            INSERT INTO match_links (kind, primary_id, secondary_id, score, features_json, status)
            VALUES ('duplicate', ?, ?, 0.70, ?, 'pending')
            """,
            (t1, t2, json.dumps(feat, ensure_ascii=False)),
        )
        conn.commit()

    resp = client.get("/api/review")
    assert resp.status_code == 200
    items = resp.json()
    assert len(items) == 1
    assert items[0]["llm"] == {"verdict": "same", "reason": "同一笔商户消费", "confidence": 0.92}


def test_profile_importer_html_xls_and_shifted_header_and_auto_detect_false():
    """ProfileImporter must parse HTML-disguised .xls, recover shifted headers, and honor auto_detect=False."""
    html_xls = (
        "<html><body><table>"
        "<tr><td colspan='4'>某银行对账单</td></tr>"
        "<tr><td colspan='4'>导出时间：2026-11-10</td></tr>"
        "<tr><th>交易日期</th><th>交易金额</th><th>交易对方</th><th>摘要</th></tr>"
        "<tr><td>2026-11-01</td><td>-45.50</td><td>盒马鲜生</td><td>生鲜采购</td></tr>"
        "</table></body></html>"
    ).encode("utf-8")

    yaml_text = """
id: "html_xls_bank"
name: "HTML伪装XLS银行"
bank: "某银行"
account_kind: "wallet"
file_type: "xlsx"
auto_detect: false
table_detection:
  header_row_index: 1
  data_start_offset: 1
  header_row_keywords:
    - "交易日期"
    - "交易金额"
    - "交易对方"
column_mapping:
  date: "交易日期"
  amount: "交易金额"
  counterparty: "交易对方"
  description: "摘要"
amount_mode:
  type: "signed"
  amount_col: "交易金额"
"""
    profile = BankProfile.from_yaml(yaml_text)
    imp = ProfileImporter(profile)
    probe = FileProbe(html_xls, file_name="statement.xls")

    # auto_detect: false must prevent automatic detection
    assert imp.detect(probe) == 0.0

    # Explicit parse must succeed despite HTML .xls AND shifted header row (actual row 2 vs configured 1)
    result = imp.parse(probe)
    assert len(result.transactions) == 1
    assert result.transactions[0].amount_cents == 4550
    assert result.transactions[0].direction == "expense"
    assert result.accounts[0].kind == "wallet"


def test_owner_only_encrypted_pdf_opens_without_password_and_multi_zip_warns(client: TestClient):
    """Owner-password-only PDF opens without password prompt; multi-file ZIP emits skip warning."""
    # 1. Owner-password-only PDF (empty user password)
    d = pymupdf.open()
    p = d.new_page()
    p.insert_text((50, 50), "Plain bank statement text", fontsize=12)
    owner_only_pdf = d.tobytes(encryption=pymupdf.PDF_ENCRYPT_AES_256, owner_pw="owner_lock", user_pw="")
    d.close()

    with open_pdf_document(owner_only_pdf) as opened:
        assert len(opened) == 1

    # 2. Multi-file ZIP warning
    csv1 = generate_synthetic_alipay_csv()
    csv2 = b"date,amount\n2026-11-01,-10.00\n"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("alipay_main.csv", csv1)
        zf.writestr("other_month.csv", csv2)

    resp = client.post(
        "/api/import/preview",
        files={"file": ("multi_bills.zip", buf.getvalue(), "application/zip")},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["need_password"] is False
    assert any("已跳过" in w for w in data["warnings"])


def test_boc_and_icbc_detectors_do_not_hijack_wrong_card_kind():
    """BOC debit detector rejects BOC credit card PDF; ICBC credit detector rejects ICBC debit PDF."""
    d_boc_cc = pymupdf.open()
    p1 = d_boc_cc.new_page()
    p1.insert_text((50, 50), "中国银行信用卡对账单", fontname="china-ss", fontsize=12)
    boc_cc_bytes = d_boc_cc.tobytes()
    d_boc_cc.close()

    assert BOCDebitImporter().detect(FileProbe(boc_cc_bytes, file_name="中国银行信用卡.pdf")) == 0.0

    d_icbc_dc = pymupdf.open()
    p2 = d_icbc_dc.new_page()
    p2.insert_text((50, 50), "中国工商银行借记卡流水", fontname="china-ss", fontsize=12)
    icbc_dc_bytes = d_icbc_dc.tobytes()
    d_icbc_dc.close()

    assert ICBCCreditImporter().detect(FileProbe(icbc_dc_bytes, file_name="工商银行借记卡.pdf")) == 0.0
