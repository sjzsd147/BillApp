"""Unit and integration tests for Core Data Layer, Importers (Alipay & WeChat), L1 & L2 dedup."""

from __future__ import annotations

import io
import json
import sqlite3
from decimal import Decimal
from pathlib import Path

import pytest

try:
    from backend.tests.fixtures.synthetic_pdf import prepare_cjk_font
except ImportError:
    from tests.fixtures.synthetic_pdf import prepare_cjk_font

from billapp.core.db import configure_connection, create_connection, get_db, init_db
from billapp.core.models import (
    AccountCreate,
    AccountRef,
    ImportBatchCreate,
    TransactionCreate,
)
from billapp.core.money import format_money, parse_signed_amount, to_cents, to_display
from billapp.dedup.fingerprint import (
    assign_fingerprints,
    compute_fallback_fingerprint,
    compute_order_fingerprint,
)
from billapp.dedup.refund import (
    extract_alipay_parent_order_id,
    link_in_memory_refunds,
    link_in_source_refunds,
)
from billapp.importers.alipay import AlipayImporter
from billapp.importers.base import FileProbe
from billapp.importers.common import (
    detect_encoding,
    extract_channel_and_normalize,
    parse_datetime,
    parse_funding_channel,
)
from billapp.importers.registry import ImporterRegistry, get_registry
from billapp.importers.wechat import WeChatImporter
try:
    from backend.tests.fixtures.synthetic_alipay import generate_synthetic_alipay_csv
    from backend.tests.fixtures.synthetic_wechat import generate_synthetic_wechat_xlsx
except ImportError:
    from tests.fixtures.synthetic_alipay import generate_synthetic_alipay_csv
    from tests.fixtures.synthetic_wechat import generate_synthetic_wechat_xlsx


# ============================================================================
# 1. Money & Currency Utilities Tests
# ============================================================================


def test_money_to_cents_conversion() -> None:
    assert to_cents("123.45") == 12345
    assert to_cents("-29.80") == -2980
    assert to_cents("1,029.80") == 102980
    assert to_cents("¥15.80") == 1580
    assert to_cents("$14.97") == 1497
    assert to_cents("0.00") == 0
    assert to_cents(None) == 0
    assert to_cents("") == 0
    assert to_cents(100) == 100
    assert to_cents(123.45) == 12345
    assert to_cents(Decimal("123.45")) == 12345
    assert to_cents("123.455") == 12346  # ROUND_HALF_UP


def test_money_to_display() -> None:
    assert to_display(12345) == "123.45"
    assert to_display(-2980) == "-29.80"
    assert to_display(0) == "0.00"
    assert to_display(5) == "0.05"


def test_format_money() -> None:
    assert format_money(12345, "CNY") == "¥123.45"
    assert format_money(-2980, "CNY") == "-¥29.80"
    assert format_money(1497, "USD") == "$14.97"
    assert format_money(12345, "CNY", symbol=False) == "123.45 CNY"


def test_parse_signed_amount() -> None:
    cents, direction = parse_signed_amount("-29.80")
    assert cents == 2980
    assert direction == "expense"

    cents_pos, dir_pos = parse_signed_amount("+1,000.00")
    assert cents_pos == 100000
    assert dir_pos == "income"


# ============================================================================
# 2. SQLite Database, WAL & Schema Integrity Tests
# ============================================================================


def test_db_init_and_pragmas() -> None:
    conn = init_db(":memory:")
    # Foreign keys check
    fk_check = conn.execute("PRAGMA foreign_keys;").fetchone()[0]
    assert fk_check == 1

    # Busy timeout check
    timeout = conn.execute("PRAGMA busy_timeout;").fetchone()[0]
    assert timeout == 5000

    # Table count check (all 10 core tables created)
    tables = [
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%';"
        ).fetchall()
    ]
    expected_tables = {
        "accounts",
        "import_batches",
        "transactions",
        "match_links",
        "match_blocklist",
        "categories",
        "rules",
        "llm_cache",
        "settings",
        "bank_profiles",
    }
    assert expected_tables.issubset(set(tables))
    conn.close()


def test_db_foreign_key_and_cascade_delete() -> None:
    conn = init_db(":memory:")

    # 1. Insert account
    cur = conn.execute(
        "INSERT INTO accounts (kind, bank, tail, currency) VALUES (?, ?, ?, ?)",
        ("platform", "支付宝", "0001", "CNY"),
    )
    acc_id = cur.lastrowid

    # 2. Insert batch
    cur = conn.execute(
        "INSERT INTO import_batches (file_name, file_sha1, file_size, source) VALUES (?, ?, ?, ?)",
        ("test.csv", "sha1_batch_1", 1024, "alipay"),
    )
    batch_id = cur.lastrowid

    # 3. Insert transaction
    cur = conn.execute(
        """
        INSERT INTO transactions (
            batch_id, source, account_id, txn_time, time_precision,
            amount_cents, direction, fingerprint
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            batch_id,
            "alipay",
            acc_id,
            "2026-11-09 12:00:00",
            "second",
            2747,
            "expense",
            "fp_test_123",
        ),
    )
    txn_id = cur.lastrowid

    # Verify transaction exists
    count = conn.execute("SELECT count(*) FROM transactions WHERE id = ?", (txn_id,)).fetchone()[0]
    assert count == 1

    # Cascade delete batch -> transaction should be deleted automatically
    conn.execute("DELETE FROM import_batches WHERE id = ?", (batch_id,))
    count_after = conn.execute("SELECT count(*) FROM transactions WHERE id = ?", (txn_id,)).fetchone()[0]
    assert count_after == 0

    # Foreign key constraint check: inserting transaction with non-existent batch fails
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            """
            INSERT INTO transactions (
                batch_id, source, account_id, txn_time, time_precision,
                amount_cents, direction, fingerprint
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                99999,
                "alipay",
                acc_id,
                "2026-11-09 12:00:00",
                "second",
                2747,
                "expense",
                "fp_invalid_fk",
            ),
        )
    conn.close()


# ============================================================================
# 3. Alipay Statement Parser & Reconciliation Formula Tests
# ============================================================================


def test_alipay_synthetic_parsing() -> None:
    probe = FileProbe(generate_synthetic_alipay_csv(), file_name="alipay_synthetic.csv")

    importer = AlipayImporter()
    confidence = importer.detect(probe)
    assert confidence >= 0.9, f"Expected Alipay detection >= 0.9, got {confidence}"

    res = importer.parse(probe)

    assert len(res.transactions) == 5
    assert res.holder_name == "张三"
    assert res.period == ("2026-10-09 00:00:00", "2026-11-09 23:59:59")

    expenses = [t for t in res.transactions if t.direction == "expense"]
    neutrals = [t for t in res.transactions if t.direction == "neutral"]
    assert len(expenses) == 3
    assert len(neutrals) == 2

    account_keys = {(a.kind, a.bank, a.tail) for a in res.accounts}
    assert ("platform", "支付宝", "0001") in account_keys
    assert ("bank_credit", "工商银行", "8801") in account_keys
    assert ("bank_debit", "中国银行", "6666") in account_keys

    check_map = {sc.name: sc for sc in res.self_checks}
    assert check_map["总笔数校验"].passed is True
    assert check_map["支出笔数自检"].passed is True
    assert check_map["不计收支笔数自检"].passed is True

    fingerprints = [t.fingerprint for t in res.transactions]
    assert all(fp is not None and len(fp) == 40 for fp in fingerprints)
    assert len(set(fingerprints)) == 5


# ============================================================================
# 4. WeChat Statement Parser & Self-Check Tests
# ============================================================================


def test_wechat_synthetic_parsing() -> None:
    probe = FileProbe(generate_synthetic_wechat_xlsx(), file_name="wechat_synthetic.xlsx")

    importer = WeChatImporter()
    confidence = importer.detect(probe)
    assert confidence >= 0.9, f"Expected WeChat detection >= 0.9, got {confidence}"

    res = importer.parse(probe)

    assert len(res.transactions) == 5
    assert res.holder_name == "demo_user"
    assert res.period == ("2026-10-09 00:00:00", "2026-11-09 20:08:20")

    check_map = {sc.name: sc for sc in res.self_checks}
    assert check_map["总笔数自检"].passed is True
    assert check_map["支出笔数自检"].passed is True
    assert check_map["支出金额自检"].passed is True

    acc_keys = {(a.kind, a.bank, a.tail) for a in res.accounts}
    assert ("wallet", "微信支付", None) in acc_keys
    assert ("bank_credit", "工商银行", "8801") in acc_keys

    fingerprints = [t.fingerprint for t in res.transactions]
    assert all(fp is not None and len(fp) == 40 for fp in fingerprints)
    assert len(set(fingerprints)) == 5


# ============================================================================
# 5. L1 Fingerprint Engine Tests
# ============================================================================


def test_fingerprint_with_order_id() -> None:
    fp1 = compute_order_fingerprint("alipay", "2026110900000000000000000101\t")
    fp2 = compute_order_fingerprint("alipay", "2026110900000000000000000101")
    assert fp1 == fp2
    assert len(fp1) == 40

    fp3 = compute_order_fingerprint("wechat", "450000057820261109001")
    assert fp1 != fp3


def test_fingerprint_fallback_with_intra_file_sequence() -> None:
    # 2 identical bank transactions in the same file (same time, amount, counterparty)
    txns = [
        TransactionCreate(
            source="icbc_credit",
            txn_time="2026-11-09 12:00:00",
            amount_cents=500,
            currency="CNY",
            direction="expense",
            counterparty="上海地铁",
            description="乘车",
            account_ref=AccountRef(kind="bank_credit", bank="工商银行", tail="8801"),
        ),
        TransactionCreate(
            source="icbc_credit",
            txn_time="2026-11-09 12:00:00",
            amount_cents=500,
            currency="CNY",
            direction="expense",
            counterparty="上海地铁",
            description="乘车",
            account_ref=AccountRef(kind="bank_credit", bank="工商银行", tail="8801"),
        ),
    ]

    assign_fingerprints(txns)
    # Different fingerprints assigned due to sequence counter seq_n
    assert txns[0].fingerprint != txns[1].fingerprint

    # Re-running on identical list produces identical deterministic hashes
    fp0_orig = txns[0].fingerprint
    fp1_orig = txns[1].fingerprint

    txns_reimport = [
        TransactionCreate(
            source="icbc_credit",
            txn_time="2026-11-09 12:00:00",
            amount_cents=500,
            currency="CNY",
            direction="expense",
            counterparty="上海地铁",
            description="乘车",
            account_ref=AccountRef(kind="bank_credit", bank="工商银行", tail="8801"),
        ),
        TransactionCreate(
            source="icbc_credit",
            txn_time="2026-11-09 12:00:00",
            amount_cents=500,
            currency="CNY",
            direction="expense",
            counterparty="上海地铁",
            description="乘车",
            account_ref=AccountRef(kind="bank_credit", bank="工商银行", tail="8801"),
        ),
    ]
    assign_fingerprints(txns_reimport)
    assert txns_reimport[0].fingerprint == fp0_orig
    assert txns_reimport[1].fingerprint == fp1_orig


# ============================================================================
# 6. L2 In-Source Refund Linking & Neutral Propagation Tests
# ============================================================================


def test_alipay_refund_order_prefix_and_neutral_propagation() -> None:
    # 1. Normal purchase + refund
    txns = [
        TransactionCreate(
            source="alipay",
            txn_time="2026-10-16 13:26:32",
            amount_cents=3280,
            currency="CNY",
            direction="expense",
            counterparty="淘菜菜",
            order_id="2026101600000000000000000201",
            status="交易成功",
        ),
        TransactionCreate(
            source="alipay",
            txn_time="2026-11-03 08:47:44",
            amount_cents=3280,
            currency="CNY",
            direction="refund",
            counterparty="淘菜菜",
            order_id="2026101600000000000000000201*F-refundplatform3-R-9000000000001_9000000000002_4",
            status="退款成功",
        ),
    ]
    linked = link_in_memory_refunds(txns)
    assert linked == 1
    assert txns[0].refunded_cents == 3280
    assert txns[0].direction == "expense"

    # 2. Cancelled purchase (交易关闭) + refund -> Neutral propagation
    txns_cancelled = [
        TransactionCreate(
            source="alipay",
            txn_time="2026-10-16 13:25:48",
            amount_cents=2980,
            currency="CNY",
            direction="neutral",
            counterparty="测试数码店",
            order_id="2026101600000000000000000104",
            status="交易关闭",
        ),
        TransactionCreate(
            source="alipay",
            txn_time="2026-10-16 13:26:21",
            amount_cents=2980,
            currency="CNY",
            direction="refund",
            counterparty="测试数码店",
            order_id="2026101600000000000000000104_9000000000000000104",
            status="退款成功",
        ),
    ]
    linked_c = link_in_memory_refunds(txns_cancelled)
    assert linked_c == 1
    # Both transactions must be neutral!
    assert txns_cancelled[0].direction == "neutral"
    assert txns_cancelled[1].direction == "neutral"
    assert txns_cancelled[0].refunded_cents == 0


def test_wechat_refund_matching_and_neutral_propagation() -> None:
    # WeChat: counterparty + amount + 10 min window
    txns = [
        TransactionCreate(
            source="wechat",
            txn_time="2026-11-05 21:19:03",
            amount_cents=400,
            currency="CNY",
            direction="expense",
            counterparty="校园物业中心",
            order_id="450000058020261105001",
            status="已全额退款",
        ),
        TransactionCreate(
            source="wechat",
            txn_time="2026-11-05 21:19:08",
            amount_cents=400,
            currency="CNY",
            direction="refund",
            counterparty="校园物业中心",
            order_id="501012090120261105001",
            status="已全额退款",
        ),
    ]
    linked = link_in_memory_refunds(txns)
    assert linked == 1
    # Both are propagated to neutral because purchase was already fully refunded
    assert txns[0].direction == "neutral"
    assert txns[1].direction == "neutral"


def test_db_refund_linking_propagation() -> None:
    conn = init_db(":memory:")
    # Setup accounts & batch
    cur = conn.execute(
        "INSERT INTO accounts (kind, bank, tail, currency) VALUES (?, ?, ?, ?)",
        ("platform", "支付宝", "0001", "CNY"),
    )
    acc_id = cur.lastrowid
    cur = conn.execute(
        "INSERT INTO import_batches (file_name, file_sha1, file_size, source) VALUES (?, ?, ?, ?)",
        ("batch_test.csv", "sha1_ref_test", 1024, "alipay"),
    )
    batch_id = cur.lastrowid

    # Insert orig cancelled transaction and refund transaction
    conn.execute(
        """
        INSERT INTO transactions (
            batch_id, source, account_id, txn_time, time_precision,
            amount_cents, direction, order_id, status, fingerprint
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            batch_id,
            "alipay",
            acc_id,
            "2026-10-16 13:25:48",
            "second",
            2980,
            "expense",
            "2026101600000000000000000104",
            "交易关闭",
            "fp_orig_closed",
        ),
    )

    conn.execute(
        """
        INSERT INTO transactions (
            batch_id, source, account_id, txn_time, time_precision,
            amount_cents, direction, order_id, status, fingerprint
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            batch_id,
            "alipay",
            acc_id,
            "2026-10-16 13:26:21",
            "second",
            2980,
            "refund",
            "2026101600000000000000000104_9000000000000000104",
            "退款成功",
            "fp_ref_closed",
        ),
    )

    linked = link_in_source_refunds(conn, batch_id)
    assert linked == 1

    # Verify neutral propagation in DB
    rows = conn.execute(
        "SELECT order_id, direction, refund_of FROM transactions WHERE batch_id = ?",
        (batch_id,),
    ).fetchall()
    row_map = {r["order_id"]: r for r in rows}

    orig_row = row_map["2026101600000000000000000104"]
    ref_row = row_map["2026101600000000000000000104_9000000000000000104"]

    assert orig_row["direction"] == "neutral"
    assert ref_row["direction"] == "neutral"
    conn.close()


# ============================================================================
# 7. Synthetic Edge Cases & Registry Tests
# ============================================================================


def test_registry_detection_and_dispatch() -> None:
    reg = get_registry()
    p_alipay = FileProbe(generate_synthetic_alipay_csv(), file_name="alipay_syn.csv")
    imp_alipay, score_a = reg.detect_importer(p_alipay)
    assert imp_alipay is not None
    assert imp_alipay.source_id == "alipay"
    assert score_a >= 0.9

    p_wechat = FileProbe(generate_synthetic_wechat_xlsx(), file_name="wechat_syn.xlsx")
    imp_wechat, score_w = reg.detect_importer(p_wechat)
    assert imp_wechat is not None
    assert imp_wechat.source_id == "wechat"
    assert score_w >= 0.9

    # Unrecognized dummy file
    dummy = FileProbe(b"Hello world\nThis is not a statement\n12345")
    imp_dummy, score_d = reg.detect_importer(dummy)
    assert imp_dummy is None
    assert score_d == 0.0


def test_wechat_csv_format_parsing() -> None:
    # Synthetic WeChat CSV content
    synthetic_csv = """微信支付账单明细
微信昵称：[测试用户]
起始时间：[2026-10-09 00:00:00] 终止时间：[2026-11-09 20:00:00]
共2笔记录
收入：1笔 50.00元
支出：1笔 15.00元
中性交易：0笔 0.00元
----------------------微信支付账单明细列表--------------------
交易时间,交易类型,交易对方,商品,收/支,金额(元),支付方式,当前状态,交易单号,商户单号,备注
2026-11-09 12:00:00,商户消费,全家便利店,午餐便当,支出,15.00,工商银行信用卡(8801),支付成功,4500000001,M001,
2026-11-08 10:00:00,微信红包,张三,红包,收入,50.00,零钱,已存入零钱,4500000002,M002,
"""
    probe = FileProbe(synthetic_csv.encode("utf-8"), file_name="wechat_synthetic.csv")
    importer = WeChatImporter()
    assert importer.detect(probe) >= 0.8

    res = importer.parse(probe)
    assert len(res.transactions) == 2
    assert res.holder_name == "测试用户"
    assert res.transactions[0].amount_cents == 1500
    assert res.transactions[0].direction == "expense"
    assert res.transactions[0].fund_account_ref.tail == "8801"
    assert res.transactions[1].amount_cents == 5000
    assert res.transactions[1].direction == "income"

    check_map = {sc.name: sc for sc in res.self_checks}
    assert check_map["总笔数自检"].passed is True
    assert check_map["支出金额自检"].passed is True
    assert check_map["收入金额自检"].passed is True


def test_corrupted_empty_statements() -> None:
    empty_probe = FileProbe(b"", file_name="empty.csv")
    with pytest.raises(ValueError):
        AlipayImporter().parse(empty_probe)

    no_header_probe = FileProbe(b"Some text\nNo headers here", file_name="no_header.csv")
    with pytest.raises(ValueError):
        AlipayImporter().parse(no_header_probe)


# ============================================================================
# 8. Full DB Persistence, Effective Expense Query & Normalization Tests
# ============================================================================


def test_full_database_import_and_effective_expense() -> None:
    conn = init_db(":memory:")

    alipay_probe = FileProbe(generate_synthetic_alipay_csv(), file_name="alipay_syn.csv")
    alipay_res = AlipayImporter().parse(alipay_probe)

    # 1. Insert Batch
    cur = conn.execute(
        """
        INSERT INTO import_batches (
            file_name, file_sha1, file_size, source, row_count,
            period_start, period_end, self_check_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            alipay_probe.file_name,
            alipay_probe.file_sha1,
            alipay_probe.file_size,
            "alipay",
            len(alipay_res.transactions),
            alipay_res.period[0] if alipay_res.period else None,
            alipay_res.period[1] if alipay_res.period else None,
            json.dumps([sc.model_dump() for sc in alipay_res.self_checks], ensure_ascii=False),
        ),
    )
    batch_id = cur.lastrowid

    # Duplicate file SHA1 insert must fail
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            """
            INSERT INTO import_batches (
                file_name, file_sha1, file_size, source, row_count
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                alipay_probe.file_name,
                alipay_probe.file_sha1,
                alipay_probe.file_size,
                "alipay",
                len(alipay_res.transactions),
            ),
        )

    # 2. Insert Accounts
    account_id_map = {}
    for acc in alipay_res.accounts:
        cur = conn.execute(
            """
            INSERT INTO accounts (kind, bank, tail, alias, holder, currency)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(kind, bank, tail) DO UPDATE SET alias=excluded.alias
            RETURNING id
            """,
            (acc.kind, acc.bank, acc.tail, acc.alias, acc.holder, acc.currency),
        )
        acc_id = cur.fetchone()[0]
        account_id_map[(acc.kind, acc.bank, acc.tail)] = acc_id

    # 3. Insert Transactions
    for txn in alipay_res.transactions:
        # resolve account id
        acc_key = (
            txn.account_ref.kind,
            txn.account_ref.bank,
            txn.account_ref.tail,
        ) if txn.account_ref else ("platform", "支付宝", "0001")
        account_id = account_id_map.get(acc_key, 1)

        fund_id = None
        if txn.fund_account_ref:
            f_key = (
                txn.fund_account_ref.kind,
                txn.fund_account_ref.bank,
                txn.fund_account_ref.tail,
            )
            fund_id = account_id_map.get(f_key)

        conn.execute(
            """
            INSERT INTO transactions (
                batch_id, source, account_id, fund_account_id,
                txn_time, time_precision, amount_cents, currency,
                direction, counterparty, description, pay_method,
                channel, merchant_norm, order_id, merchant_order_id,
                status, platform_category, fingerprint, refund_of,
                refunded_cents
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                batch_id,
                txn.source,
                account_id,
                fund_id,
                txn.txn_time,
                txn.time_precision,
                txn.amount_cents,
                txn.currency,
                txn.direction,
                txn.counterparty,
                txn.description,
                txn.pay_method,
                txn.channel,
                txn.merchant_norm,
                txn.order_id,
                txn.merchant_order_id,
                txn.status,
                txn.platform_category,
                txn.fingerprint,
                txn.refund_of,
                txn.refunded_cents,
            ),
        )

    # Verify total transactions inserted in DB
    total_in_db = conn.execute("SELECT count(*) FROM transactions WHERE batch_id = ?", (batch_id,)).fetchone()[0]
    assert total_in_db == 5

    # Duplicate fingerprint insert must fail
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            """
            INSERT INTO transactions (
                batch_id, source, account_id, txn_time, time_precision,
                amount_cents, direction, fingerprint
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                batch_id,
                "alipay",
                1,
                "2026-11-09 11:44:08",
                "second",
                2747,
                "expense",
                alipay_res.transactions[0].fingerprint,
            ),
        )

    # Query Effective Expense in SQLite: 2747 + 1090 + 1480 = 5317 cents
    succ_expense_sum = conn.execute(
        """
        SELECT sum(amount_cents - refunded_cents)
        FROM transactions
        WHERE direction = 'expense' AND duplicate_of IS NULL AND excluded = 0
        """
    ).fetchone()[0]
    assert succ_expense_sum == 5317

    conn.close()


def test_channel_and_merchant_normalization() -> None:
    cases = [
        ("支付宝-麦当劳（中国）有限公司", "alipay", "麦当劳"),
        ("财付通-星辰大学", "tenpay", "星辰大学"),
        ("网银在线-沙县小吃测试店", "jdpay", "沙县小吃测试店"),
        ("美团支付-美团外卖商家", "meituan", "美团外卖商家"),
        ("云闪付-滴滴出行", "unionpay", "滴滴出行"),
        ("罗森便利店REDEMPTION", None, "罗森便利店"),
        ("中国银联股份有限公司", None, "中国银联"),
    ]

    for raw, exp_ch, exp_norm in cases:
        ch, norm = extract_channel_and_normalize(raw)
        assert ch == exp_ch, f"Failed channel on {raw}: expected {exp_ch}, got {ch}"
        assert exp_norm in norm, f"Failed norm on {raw}: expected {exp_norm} in {norm}"


def test_datetime_parser_formats() -> None:
    iso, prec = parse_datetime("2026-11-09 11:44:08")
    assert iso == "2026-11-09 11:44:08" and prec == "second"

    iso, prec = parse_datetime("2026/11/09 11:44:08")
    assert iso == "2026-11-09 11:44:08" and prec == "second"

    iso, prec = parse_datetime("2026-11-09 11:44")
    assert iso == "2026-11-09 11:44:00" and prec == "minute"

    iso, prec = parse_datetime("2026-11-09")
    assert iso == "2026-11-09" and prec == "day"

    iso, prec = parse_datetime("20261109")
    assert iso == "2026-11-09" and prec == "day"

    iso, prec = parse_datetime(None)
    assert prec == "day"


# ============================================================================
# 9. Bare-CR CSV & ZIP Archive Unwrapping Regression Tests
# ============================================================================


def _build_custom_multipage_pdf_bytes(
    *,
    password: str | None = None,
    multipage_repeat_header: bool = True,
) -> bytes:
    """Create a synthetic custom-format PDF statement using PyMuPDF for AI mapping/ZIP tests."""
    import pymupdf

    doc = pymupdf.open()
    headers = ["入账日期", "收支金额", "账单说明", "收款方"]
    col_widths = [95, 85, 160, 160]
    col_xs = [40]
    for w in col_widths:
        col_xs.append(col_xs[-1] + w)

    pages_rows = [
        [
            headers,
            ["2026-11-10", "-38.50", "门店消费", "星巴克咖啡"],
            ["2026-11-11", "500.00", "奖金补贴", "某科技公司"],
        ]
    ]
    if multipage_repeat_header:
        pages_rows.append(
            [
                headers,
                ["2026-11-12", "-12.00", "便捷购物", "全家便利店"],
            ]
        )

    for page_idx, rows in enumerate(pages_rows):
        page = doc.new_page(width=700, height=450)
        fontname = prepare_cjk_font(page)
        if page_idx == 0:
            page.insert_text(pymupdf.Point(40, 38), "自定义导出账单明细", fontname=fontname, fontsize=14)

        y0 = 85
        row_h = 24
        for r_i in range(len(rows) + 1):
            y = y0 + r_i * row_h
            page.draw_line(pymupdf.Point(col_xs[0], y), pymupdf.Point(col_xs[-1], y), color=(0.3, 0.3, 0.3), width=0.5)
        for x in col_xs:
            page.draw_line(pymupdf.Point(x, y0), pymupdf.Point(x, y0 + len(rows) * row_h), color=(0.3, 0.3, 0.3), width=0.5)

        for r_i, row_vals in enumerate(rows):
            y = y0 + r_i * row_h + 16
            for c_i, val in enumerate(row_vals):
                page.insert_text(pymupdf.Point(col_xs[c_i] + 4, y), val, fontname=fontname, fontsize=9)

    save_kwargs = {}
    if password:
        save_kwargs = {
            "encryption": pymupdf.PDF_ENCRYPT_AES_128,
            "user_pw": password,
            "owner_pw": password,
        }
    return doc.tobytes(**save_kwargs)


def test_bare_cr_csv_does_not_crash_raw_extract_or_profile(tmp_path: Path) -> None:
    """CSV files using bare CR line endings (or stray CR) must not raise _csv.Error."""
    from billapp.api.import_ import extract_raw_rows_from_probe
    from billapp.importers.profile_engine import BankProfile, ProfileImporter

    csv_bytes = (
        "交易日期,交易时间,交易金额,账户余额,交易备注\r"
        "2026-11-10,12:00:00,-35.00,1000.00,午餐\r"
        "2026-11-11,09:30:00,100.00,1100.00,报销\r"
    ).encode("utf-8")
    csv_path = tmp_path / "custom_cr_only.csv"
    csv_path.write_bytes(csv_bytes)
    probe = FileProbe(csv_path)
    assert "\r" not in probe.read_text()

    raw_file_type, raw_rows = extract_raw_rows_from_probe(probe)
    assert raw_file_type == "csv"
    assert len(raw_rows) == 3
    assert raw_rows[1][0] == "2026-11-10"

    custom_profile = BankProfile.from_yaml(
        """
id: custom_cr_csv
name: 自定义CR银行流水
bank: 测试银行
account_kind: debit
file_type: csv
table_detection:
  header_row_pattern: "交易日期.*交易金额"
column_mapping:
  date:
    header: ["交易日期"]
  time:
    header: ["交易时间"]
    optional: true
  amount:
    header: ["交易金额"]
  balance:
    header: ["账户余额"]
    optional: true
  description:
    header: ["交易备注"]
    optional: true
amount_mode:
  type: signed
  negative_means: expense
  amount_col: "交易金额"
"""
    )
    parsed = ProfileImporter(custom_profile).parse(probe)
    assert len(parsed.transactions) == 2


def test_zip_unknown_pdf_falls_back_to_mapping_then_parses_with_user_profile(tmp_path: Path) -> None:
    """An unrecognized multi-page PDF inside .zip must fall back to AI mapping raw_rows, then parse via saved user profile."""
    import zipfile
    from fastapi.testclient import TestClient
    from billapp.app import create_app

    pdf_bytes = _build_custom_multipage_pdf_bytes(multipage_repeat_header=True)
    zip_buf = io.BytesIO()
    with zipfile.ZipFile(zip_buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("__MACOSX/._ignore.pdf", b"mac-junk")
        zf.writestr("自定义账单.pdf", pdf_bytes)
    zip_bytes = zip_buf.getvalue()

    app = create_app(db_path=str(tmp_path / "zip_custom_pdf.db"))
    with TestClient(app) as client:
        # 1. Without a matching profile, .zip unpacks cleanly and returns need_mapping=True with extracted PDF rows
        resp = client.post(
            "/api/import/preview",
            files={"file": ("自定义账单导出.zip", zip_bytes, "application/zip")},
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["need_password"] is False
        assert data["need_mapping"] is True
        assert data["detected_file_type"] == "pdf"
        assert data["raw_rows"][0] == ["入账日期", "收支金额", "账单说明", "收款方"]

        # 2. User / AI saves a custom mapping profile -> subsequent preview parses cleanly & skips page-2 header
        profile_yaml = """id: custom_zip_pdf_test
name: 自定义ZIP测试配置
bank: 测试银行
account_kind: debit
file_type: pdf
table_detection:
  header_row_keywords: ["入账日期", "收支金额", "账单说明", "收款方"]
  data_start_offset: 1
column_mapping:
  date:
    header: "入账日期"
  amount:
    header: "收支金额"
  description:
    header: "账单说明"
  counterparty:
    header: "收款方"
amount_mode:
  type: signed
  negative_means: expense
  amount_col: "收支金额"
date_time:
  date_col: "入账日期"
"""
        prof_resp = client.post(
            "/api/profiles",
            json={"id": "custom_zip_pdf_test", "name": "自定义ZIP测试配置", "yaml_content": profile_yaml},
        )
        assert prof_resp.status_code == 201, prof_resp.text

        parsed_resp = client.post(
            "/api/import/preview",
            files={"file": ("自定义账单导出.zip", zip_bytes, "application/zip")},
        )
        assert parsed_resp.status_code == 200, parsed_resp.text
        parsed = parsed_resp.json()
        assert parsed["need_mapping"] is False
        assert parsed["source"] == "profile:custom_zip_pdf_test"
        assert parsed["total_rows"] == 3
        txns = parsed["rows"]
        assert txns[0]["amount_cents"] == 3850 and txns[0]["direction"] == "expense"
        assert txns[1]["amount_cents"] == 50000 and txns[1]["direction"] == "income"
        assert txns[2]["amount_cents"] == 1200 and txns[2]["direction"] == "expense"

        commit_resp = client.post("/api/import/commit", json={"token": parsed["token"]})
        assert commit_resp.status_code == 200, commit_resp.text
        assert commit_resp.json()["batch_id"] > 0
        assert commit_resp.json()["imported_count"] == 3


def test_zip_with_encrypted_pdf_prompts_and_unlocks(tmp_path: Path) -> None:
    """An encrypted PDF wrapped inside a .zip must return need_password, fail cleanly on wrong pw, and unlock to raw_rows on right pw."""
    import zipfile
    from fastapi.testclient import TestClient
    from billapp.app import create_app

    pdf_bytes = _build_custom_multipage_pdf_bytes(password="246810", multipage_repeat_header=False)
    zip_buf = io.BytesIO()
    with zipfile.ZipFile(zip_buf, "w") as zf:
        zf.writestr("locked_stmt.pdf", pdf_bytes)
    zip_bytes = zip_buf.getvalue()

    app = create_app(db_path=str(tmp_path / "zip_locked_pdf.db"))
    with TestClient(app) as client:
        resp_no_pw = client.post(
            "/api/import/preview",
            files={"file": ("locked_stmt.zip", zip_bytes, "application/zip")},
        )
        assert resp_no_pw.status_code == 200, resp_no_pw.text
        assert resp_no_pw.json()["need_password"] is True

        resp_wrong_pw = client.post(
            "/api/import/preview",
            data={"password": "000000"},
            files={"file": ("locked_stmt.zip", zip_bytes, "application/zip")},
        )
        assert resp_wrong_pw.status_code == 400
        assert resp_wrong_pw.json()["code"] == "IMPORT_PASSWORD_WRONG"

        resp_ok = client.post(
            "/api/import/preview",
            data={"password": "246810"},
            files={"file": ("locked_stmt.zip", zip_bytes, "application/zip")},
        )
        assert resp_ok.status_code == 200, resp_ok.text
        data_ok = resp_ok.json()
        assert data_ok["need_password"] is False
        assert data_ok["need_mapping"] is True
        assert data_ok["detected_file_type"] == "pdf"
        assert data_ok["raw_rows"][1][3] == "星巴克咖啡"


def test_zip_unknown_csv_and_encrypted_zip_prompt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ZIP wrapping an unknown CSV feeds AI mapping raw_rows; encrypted ZIP entries trigger need_password."""
    import zipfile
    from fastapi.testclient import TestClient
    from billapp.app import create_app

    csv_bytes = "消费时间,商户名,消费金额\r2026-11-12,测试商户,-19.90\r".encode("utf-8")
    zip_buf = io.BytesIO()
    with zipfile.ZipFile(zip_buf, "w") as zf:
        zf.writestr("custom_bill.csv", csv_bytes)
    zip_bytes = zip_buf.getvalue()

    app = create_app(db_path=str(tmp_path / "zip_csv_mapping.db"))
    with TestClient(app) as client:
        resp_extract = client.post(
            "/api/import/extract-raw",
            files={"file": ("custom_bill.zip", zip_bytes, "application/zip")},
        )
        assert resp_extract.status_code == 200, resp_extract.text
        raw_rows = resp_extract.json()["raw_rows"]
        assert raw_rows[0] == ["消费时间", "商户名", "消费金额"]
        assert raw_rows[1] == ["2026-11-12", "测试商户", "-19.90"]

        resp_preview = client.post(
            "/api/import/preview",
            files={"file": ("custom_bill.zip", zip_bytes, "application/zip")},
        )
        assert resp_preview.status_code == 200, resp_preview.text
        data_prev = resp_preview.json()
        assert data_prev["need_mapping"] is True
        assert data_prev["raw_rows"][1][1] == "测试商户"

        # Simulate encrypted ZipCrypto entry (ZipInfo.flag_bits & 0x1)
        orig_infolist = zipfile.ZipFile.infolist

        def _encrypted_infolist(self: zipfile.ZipFile):
            infos = orig_infolist(self)
            for info in infos:
                info.flag_bits |= 0x1
                info.extract_version = 20
            return infos

        monkeypatch.setattr(zipfile.ZipFile, "infolist", _encrypted_infolist)
        resp_enc_zip = client.post(
            "/api/import/preview",
            files={"file": ("encrypted_stmt.zip", zip_bytes, "application/zip")},
        )
        assert resp_enc_zip.status_code == 200, resp_enc_zip.text
        assert resp_enc_zip.json()["need_password"] is True
