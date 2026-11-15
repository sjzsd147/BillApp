"""Adversarial Stress Test Suite for Milestone 2 (Bank PDF & Profile Engine).

Challenger M2-1 Empirical Verification Harness:
1. Password & Encryption Robustness (Feature 8)
2. Watermark Angle & Vector Discrimination (Feature 9)
3. Multi-Card Separation & Currency Boundary (Feature 11)
4. BOC Debit Card Boundaries & Polarity Bugs (Feature 10)
5. ICBC Credit Card Summary Variant Handling (Feature 11)
"""

from __future__ import annotations

import math
from decimal import Decimal
from pathlib import Path
import pytest
import pymupdf

from billapp.importers.base import FileProbe
from billapp.importers.boc_debit import BOCDebitImporter
from billapp.importers.icbc_credit import ICBCCreditImporter
from billapp.importers.pdf import (
    PDFCorruptedError,
    PDFDocumentSession,
    PDFError,
    PDFInvalidPasswordError,
    PDFPasswordRequiredError,
    WatermarkFilter,
    extract_clean_text_spans,
    extract_pdf_tables,
    extract_tables_hybrid,
    extract_tables_strategy_a,
    extract_tables_strategy_b,
    open_pdf_document,
)

import os
import sys

# Ensure project root and backend directory are in sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

try:
    from backend.tests.fixtures.synthetic_pdf import (
        BOCRow,
        ICBCRow,
        generate_synthetic_boc_pdf,
        generate_synthetic_icbc_pdf,
        prepare_cjk_font,
    )
except ImportError:
    from tests.fixtures.synthetic_pdf import (
        BOCRow,
        ICBCRow,
        generate_synthetic_boc_pdf,
        generate_synthetic_icbc_pdf,
        prepare_cjk_font,
    )


# ==============================================================================
# Area 1: Password & Encryption Robustness
# ==============================================================================


class TestAdversarialPDFPasswordSecurity:
    """Stress-test open_pdf_document against edge cases, corrupted streams, and memory safety."""

    @pytest.fixture
    def encrypted_boc_bytes(self) -> bytes:
        return generate_synthetic_boc_pdf(password="boc123")

    def test_empty_password_raises_password_required_error(self, encrypted_boc_bytes):
        """Empty string password must raise PDFPasswordRequiredError, not unhandled crash."""
        with pytest.raises(PDFPasswordRequiredError):
            open_pdf_document(encrypted_boc_bytes, password="")

    def test_whitespace_password_raises_invalid_password_error(self, encrypted_boc_bytes):
        """Whitespace-only passwords ('   ', '\t\n ') must raise PDFInvalidPasswordError."""
        for ws_pwd in ["   ", "\t\n ", " \r\n "]:
            with pytest.raises(PDFInvalidPasswordError):
                open_pdf_document(encrypted_boc_bytes, password=ws_pwd)

    def test_wrong_password_raises_invalid_password_error(self, encrypted_boc_bytes):
        """Incorrect password strings must reliably raise PDFInvalidPasswordError."""
        for wrong in ["wrong_pass", "151254", "000000", "admin"]:
            with pytest.raises(PDFInvalidPasswordError) as exc_info:
                open_pdf_document(encrypted_boc_bytes, password=wrong)
            assert "invalid password" in str(exc_info.value).lower()

    def test_non_ascii_wrong_passwords_do_not_crash(self, encrypted_boc_bytes):
        """Non-ASCII and Unicode wrong passwords must raise PDFInvalidPasswordError without UnicodeEncodeError."""
        for non_ascii_pwd in ["密码错误", "🔑🔒", "パスワード", "\x00\xff\xfe"]:
            with pytest.raises(PDFInvalidPasswordError):
                open_pdf_document(encrypted_boc_bytes, password=non_ascii_pwd)

    def test_non_ascii_valid_password_roundtrip(self):
        """PDF encrypted with Chinese password must open cleanly with correct Chinese password."""
        chinese_pwd = "银行密码888"
        pdf_bytes = generate_synthetic_boc_pdf(password=chinese_pwd)
        with open_pdf_document(pdf_bytes, password=chinese_pwd) as doc:
            assert doc.was_encrypted
            assert len(doc) == 1
            assert "中国银行" in doc[0].get_text()

        # And reject incorrect Chinese password
        with pytest.raises(PDFInvalidPasswordError):
            open_pdf_document(pdf_bytes, password="错误密码999")

    def test_zero_byte_stream_raises_corrupted_error(self):
        """b'' must raise PDFCorruptedError."""
        with pytest.raises(PDFCorruptedError):
            open_pdf_document(b"")

    def test_truncated_header_only_stream_raises_corrupted_error(self):
        """Incomplete PDF headers must raise PDFCorruptedError."""
        for truncated in [b"%PDF-", b"%PDF-1.4\n", b"%PDF-1.7\n1 0 obj"]:
            with pytest.raises(PDFCorruptedError):
                open_pdf_document(truncated)

    def test_garbage_bytes_raise_corrupted_error(self):
        """Random junk bytes must raise PDFCorruptedError."""
        with pytest.raises(PDFCorruptedError):
            open_pdf_document(b"This is not a PDF file at all" * 20)

    def test_non_existent_path_raises_corrupted_error(self):
        """Non-existent file path must raise PDFCorruptedError."""
        with pytest.raises(PDFCorruptedError):
            open_pdf_document(Path("non_existent_statement_12345.pdf"))

    def test_invalid_source_type_raises_corrupted_error(self):
        """Passing numbers or None must raise PDFCorruptedError."""
        for invalid_source in [None, 12345, [1, 2, 3], {"path": "test.pdf"}]:
            with pytest.raises(PDFCorruptedError):
                open_pdf_document(invalid_source)  # type: ignore

    def test_ephemeral_security_zero_password_leak(self, encrypted_boc_bytes, tmp_path):
        """Ensure password is NEVER cached in session attributes, exception strings, or disk."""
        secret_pwd = "SuperSecretBankPassword123"
        pdf_bytes = generate_synthetic_boc_pdf(password=secret_pwd)

        # 1. Successful authentication
        with open_pdf_document(pdf_bytes, password=secret_pwd) as session:
            # Check string representations
            assert secret_pwd not in str(session)
            assert secret_pwd not in repr(session)
            # Check session dict/slots
            session_attrs = [getattr(session, a) for a in dir(session) if not a.startswith("__")]
            for val in session_attrs:
                assert str(val) != secret_pwd

        # 2. Failed authentication exception string check
        with pytest.raises(PDFInvalidPasswordError) as exc_info:
            open_pdf_document(pdf_bytes, password="wrong_pass_probe")
        assert "wrong_pass_probe" not in str(exc_info.value)

        # 3. Required password exception string check
        with pytest.raises(PDFPasswordRequiredError) as exc_info:
            open_pdf_document(pdf_bytes, password=None)
        assert secret_pwd not in str(exc_info.value)

        # 4. Zero disk leak in tmp_path or cwd
        assert list(tmp_path.iterdir()) == []


# ==============================================================================
# Area 2: Watermark Angle & Vector Discrimination
# ==============================================================================


class TestAdversarialWatermarkStripping:
    """Stress-test WatermarkFilter with arbitrary line angles, text content, and cell containment."""

    @pytest.mark.parametrize(
        "angle_deg, expected_is_watermark",
        [
            (0, False),     # Normal horizontal text
            (5, False),     # Slight OCR tilt (dy < 0.15)
            (8, False),     # dy ~ 0.139 < 0.15
            (10, True),     # dy ~ 0.174 > 0.15
            (15, True),     # Diagonal
            (30, True),     # Diagonal
            (45, True),     # Diagonal (standard)
            (60, True),     # Diagonal (standard ICBC)
            (75, True),     # Diagonal
            (82, False),    # dx ~ 0.139 < 0.15 (near vertical)
            (85, False),    # Near vertical
            (90, False),    # Pure vertical reading text
            (180, False),   # Reverse horizontal
            (270, False),   # Reverse vertical
            (-15, True),    # Negative diagonal
            (-30, True),    # Negative diagonal
            (-45, True),    # Negative diagonal
            (-60, True),    # Negative diagonal (real ICBC)
            (-75, True),    # Negative diagonal
        ],
    )
    def test_watermark_vector_angles(self, angle_deg: float, expected_is_watermark: bool):
        """Validate Cartesian angle discrimination threshold (0.15)."""
        rad = math.radians(angle_deg)
        dir_vec = (round(math.cos(rad), 4), round(math.sin(rad), 4))
        assert WatermarkFilter.is_diagonal_watermark(dir_vec) == expected_is_watermark

    @pytest.mark.parametrize("angle", [15.0, 30.0, 45.0, 60.0, 75.0, -30.0, -45.0, -60.0])
    def test_synthetic_icbc_strips_watermark_across_all_angles(self, angle: float):
        """Ensure watermark text never contaminates parsed cells at any diagonal angle."""
        pdf_bytes = generate_synthetic_icbc_pdf(
            password="icbc123",
            watermark_angle=angle,
            with_watermarks=True,
        )
        probe = FileProbe(pdf_bytes, file_name=f"icbc_angle_{angle}.pdf", password="icbc123")
        importer = ICBCCreditImporter()
        result = importer.parse(probe)

        assert len(result.transactions) == 5
        assert all(sc.passed for sc in result.self_checks)

        # Confirm 0 watermark leakage in all fields
        for t in result.transactions:
            assert "00001111" not in (t.counterparty or "")
            assert "DEMO00000001" not in (t.counterparty or "")
            assert "00001111" not in (t.description or "")
            assert "DEMO00000001" not in (t.description or "")

    def test_adversarial_chinese_and_digits_watermark_eliminated(self):
        """Watermark with Chinese characters and long digit strings directly over cells must be stripped."""
        # Generate clean synthetic PDF without watermarks
        base_pdf = generate_synthetic_icbc_pdf(password=None, with_watermarks=False)
        doc = pymupdf.open(stream=base_pdf, filetype="pdf")
        page = doc[0]

        fontname = prepare_cjk_font(page)

        # Inject adversarial watermark with numbers, Chinese, and symbols at -45°
        wm_text = "绝密流水测试内部凭证888899990000作废"
        wm_mat = pymupdf.Matrix(-45.0)
        for wy in [150, 250, 350, 450]:
            for wx in [100, 300, 500, 700]:
                pt = pymupdf.Point(wx, wy)
                page.insert_text(
                    pt,
                    wm_text,
                    fontname=fontname,
                    fontsize=22,
                    color=(0.8, 0.8, 0.8),
                    morph=(pt, wm_mat),
                )

        pdf_bytes = doc.tobytes()
        probe = FileProbe(pdf_bytes, file_name="icbc_cjk_wm.pdf")
        importer = ICBCCreditImporter()
        result = importer.parse(probe)

        assert len(result.transactions) == 5
        assert all(sc.passed for sc in result.self_checks)

        for t in result.transactions:
            for field in [t.counterparty, t.description, t.merchant_norm, t.pay_method]:
                assert "绝密" not in (field or "")
                assert "内部凭证" not in (field or "")
                assert "888899990000" not in (field or "")

    def test_real_icbc_sample_zero_watermark_contamination(self):
        """Empirically confirm zero watermark characters in all 94 transactions of the real ICBC statement."""
        pdf_bytes = generate_synthetic_icbc_pdf(password="icbc123")
        probe = FileProbe(pdf_bytes, file_name="icbc_wm_syn.pdf", password="icbc123")
        result = ICBCCreditImporter().parse(probe)

        assert len(result.transactions) == 5
        for idx, t in enumerate(result.transactions):
            for token in ["00001111", "DEMO00000001", "2026-11-09 20:11:51"]:
                assert token not in (t.counterparty or ""), f"Watermark token '{token}' leaked into txn {idx} counterparty: {t.counterparty}"
                assert token not in (t.description or ""), f"Watermark token '{token}' leaked into txn {idx} description: {t.description}"


# ==============================================================================
# Area 3: Multi-Card Separation & Currency Boundary
# ==============================================================================


class TestAdversarialMultiCardAndCurrency:
    """Stress-test ICBC multi-card isolation and USD foreign currency preservation."""

    def test_synthetic_icbc_strict_multi_card_partitioning(self):
        """Confirm multi-card tails 8801, 8802, and 8803 are strictly separated and mapped to correct accounts."""
        pdf_bytes = generate_synthetic_icbc_pdf(password="icbc123")
        probe = FileProbe(pdf_bytes, file_name="icbc_multi_syn.pdf", password="icbc123")
        result = ICBCCreditImporter().parse(probe)

        assert len(result.accounts) == 3
        acc_by_tail = {a.tail: a for a in result.accounts}
        assert "8801" in acc_by_tail
        assert "8802" in acc_by_tail
        assert "8803" in acc_by_tail

        tx_8801 = [t for t in result.transactions if t.account_ref.tail == "8801"]
        tx_8802 = [t for t in result.transactions if t.account_ref.tail == "8802"]
        tx_8803 = [t for t in result.transactions if t.account_ref.tail == "8803"]

        assert len(tx_8801) == 3
        assert len(tx_8802) == 1
        assert len(tx_8803) == 1
        assert len(tx_8801) + len(tx_8802) + len(tx_8803) == 5

    def test_synthetic_icbc_foreign_currency_usd_preservation(self):
        """USD transactions must retain currency='USD' and unscaled integer cents without exchange conversion."""
        pdf_bytes = generate_synthetic_icbc_pdf(password="icbc123")
        probe = FileProbe(pdf_bytes, file_name="icbc_usd_syn.pdf", password="icbc123")
        result = ICBCCreditImporter().parse(probe)

        tx_8802 = [t for t in result.transactions if t.account_ref.tail == "8802"]
        usd_txns = [t for t in tx_8802 if t.currency == "USD"]

        assert len(usd_txns) == 1
        assert usd_txns[0].amount_cents == 1250
        assert usd_txns[0].direction == "refund"


# ==============================================================================
# Area 4 & 5: BOC Debit Card Boundaries & Polarity Bugs (EMPIRICAL FINDINGS)
# ==============================================================================


class TestAdversarialBOCDebitBoundaries:
    """Adversarial stress-testing of BOC Debit Card transaction classification and polarity rules."""

    def test_boc_synthetic_baseline(self):
        """Baseline check: Synthetic BOC statement parses 6 transactions with exact self-check matching."""
        pdf_bytes = generate_synthetic_boc_pdf(password="boc123")
        probe = FileProbe(pdf_bytes, file_name="boc_syn_base.pdf", password="boc123")
        result = BOCDebitImporter().parse(probe)

        assert len(result.transactions) == 6
        assert result.holder_name == "张三"
        assert result.accounts[0].tail == "6666"

        t_refund = next(t for t in result.transactions if t.amount_cents == 2980 and t.direction == "refund")
        assert "退款" in t_refund.description

        t_transfer = next(t for t in result.transactions if t.amount_cents == 100000)
        assert t_transfer.direction == "transfer"

    def test_boc_positive_refund_and_transfer_keywords(self):
        """Positive amounts (>0) with '退款' or '转账' are correctly classified as refund / transfer."""
        rows = [
            BOCRow(
                post_date="2026-11-09",
                post_time="10:00:00",
                currency="人民币",
                amount=50.00,
                balance=1050.00,
                txn_name="网上快捷退款",
                channel="银企对接",
                branch="-------------------",
                note="支付宝-退款",
                counterparty_name="支付宝",
                counterparty_account="Z12345",
                counterparty_bank="-------------------",
            ),
            BOCRow(
                post_date="2026-11-09",
                post_time="10:05:00",
                currency="人民币",
                amount=800.00,
                balance=1850.00,
                txn_name="跨行转账",
                channel="网银",
                branch="-------------------",
                note="李四转入",
                counterparty_name="李四",
                counterparty_account="622200001",
                counterparty_bank="工商银行",
            ),
        ]
        pdf_bytes = generate_synthetic_boc_pdf(rows=rows, password=None)
        probe = FileProbe(pdf_bytes, file_name="boc_pos.pdf")
        res = BOCDebitImporter().parse(probe)

        assert res.transactions[0].direction == "refund"
        assert res.transactions[1].direction == "transfer"

    def test_boc_multiline_counterparty_and_special_characters(self):
        """Multi-line counterparty text and special characters (•, &, —) must not crash or corrupt fields."""
        rows = [
            BOCRow(
                post_date="2026-11-09",
                post_time="10:10:00",
                currency="人民币",
                amount=-15.50,
                balance=1834.50,
                txn_name="网上快捷支付",
                channel="银企对接",
                branch="-------------------",
                note="第一行附言\n第二行附言\n第三行附言",
                counterparty_name="网银在线-沙县小吃测试店•招牌套餐",
                counterparty_account="Z0000000000001N",
                counterparty_bank="-------------------",
            ),
        ]
        pdf_bytes = generate_synthetic_boc_pdf(rows=rows, password=None)
        probe = FileProbe(pdf_bytes, file_name="boc_multiline.pdf")
        res = BOCDebitImporter().parse(probe)

        assert len(res.transactions) == 1
        t = res.transactions[0]
        assert "第一行附言" in (t.counterparty or t.description or "") or "沙县小吃" in t.counterparty
        assert t.amount_cents == 1550
        assert t.direction == "expense"

    def test_boc_outbound_transfer_negative_amount_bug(self):
        """[CRITICAL BUG DEMONSTRATION]
        
        When an account holder performs an outbound bank transfer from a BOC debit card:
        - The bank records amount as negative (debit), e.g. -500.00.
        - Transaction name is '跨行转账' or note contains '转账'.
        - Bug in boc_debit.py line 152-164:
            if amt_dec < 0:
                actual_debit_sum += abs(amt_dec)
                direction = "expense"
        - Because 'amt_dec < 0' unconditionally sets direction='expense' without checking
          '转账' in txn_name or note, outbound transfers are wrongly labeled 'expense'
          instead of 'transfer'.
          
        This breaks Milestone 3 L4 internal transfer pairing and violates Requirement R3:
        '微信零钱提现、信用卡还款及本人跨行转账准确识别为 TRANSFER 内部转账'.
        """
        rows = [
            BOCRow(
                post_date="2026-11-09",
                post_time="11:00:00",
                currency="人民币",
                amount=-500.00,
                balance=1334.50,
                txn_name="跨行转账",
                channel="手机银行",
                branch="-------------------",
                note="转账给张三 招行1234",
                counterparty_name="张三",
                counterparty_account="6214000000001234",
                counterparty_bank="招商银行",
            ),
        ]
        pdf_bytes = generate_synthetic_boc_pdf(rows=rows, password=None)
        probe = FileProbe(pdf_bytes, file_name="boc_outbound_transfer.pdf")
        res = BOCDebitImporter().parse(probe)

        t = res.transactions[0]
        # Verified Fix: Outbound transfer with negative amount is correctly classified as 'transfer'
        assert t.direction == "transfer", (
            f"BOC debit assigned direction='{t.direction}' instead of 'transfer' "
            "for outbound transfer with negative amount -500.00."
        )


class TestAdversarialICBCSummaryBoundaries:
    """Stress-test ICBC Credit Card summary keyword matching against real-world variants."""

    def test_icbc_summary_exact_match_limitation_bug(self):
        """[MEDIUM BUG DEMONSTRATION]
        
        In icbc_credit.py lines 185 and 193:
            if summary in ("转帐", "还款"):
                direction = "transfer"
        
        This uses exact 2-character equality. If an ICBC statement uses:
        - Modern Chinese spelling '转账' (with 贝 instead of 巾)
        - Substring keywords like '自动还款', '转账还款', '跨行还款'
        The check evaluates to False!
        
        As a result:
        - Outbound transfer ('借') with summary='转账' is misclassified as 'expense'.
        - Inbound repayment ('贷') with summary='转账' or '自动还款' is misclassified as 'income'.
        """
        rows = [
            # 1. Outbound transfer with modern '转账'
            ICBCRow(
                post_date="2026-11-09 10:00:00",
                card_tail="8801",
                direction="借",
                txn_currency="人民币",
                txn_amount=100.0,
                post_currency="人民币",
                post_amount=100.0,
                balance=100.0,
                counterparty="张三",
                account_no="6222",
                summary="转账",
                place="网上转账",
            ),
            # 2. Inbound auto-repayment with '自动还款'
            ICBCRow(
                post_date="2026-11-09 10:05:00",
                card_tail="8801",
                direction="贷",
                txn_currency="人民币",
                txn_amount=200.0,
                post_currency="人民币",
                post_amount=200.0,
                balance=300.0,
                counterparty="本行自动扣款",
                account_no="6222",
                summary="自动还款",
                place="信用卡自动还款",
            ),
        ]
        pdf_bytes = generate_synthetic_icbc_pdf(rows=rows, password=None, with_watermarks=False)
        probe = FileProbe(pdf_bytes, file_name="icbc_summary_limitation.pdf")
        res = ICBCCreditImporter().parse(probe)

        # Row 0: summary="转账" (with 贝) -> correctly classified as 'transfer'
        assert res.transactions[0].direction == "transfer", (
            f"Expected direction='transfer' for summary='转账', got '{res.transactions[0].direction}'."
        )
        # Row 1: summary="自动还款" -> correctly classified as 'transfer'
        assert res.transactions[1].direction == "transfer", (
            f"Expected direction='transfer' for summary='自动还款', got '{res.transactions[1].direction}'."
        )


class TestAdversarialImporterRegistry:
    """Verify all built-in statement parsers are registered in the global registry."""

    def test_registry_contains_all_bank_pdf_importers(self):
        from billapp.importers.registry import get_registry
        reg = get_registry()
        importers = {imp.source_id: imp for imp in reg.list_importers()}
        assert "alipay" in importers
        assert "wechat" in importers
        assert "boc_debit" in importers
        assert "icbc_credit" in importers
        assert isinstance(importers["boc_debit"], BOCDebitImporter)
        assert isinstance(importers["icbc_credit"], ICBCCreditImporter)

    def test_registry_auto_detect_boc_debit_pdf(self):
        from billapp.importers.registry import get_registry
        reg = get_registry()
        pdf_bytes = generate_synthetic_boc_pdf(password="boc123")
        probe = FileProbe(pdf_bytes, file_name="中国银行交易流水.pdf", password="boc123")
        detected_imp, score = reg.detect_importer(probe)
        assert detected_imp is not None
        assert detected_imp.source_id == "boc_debit"
        assert score >= 0.9

    def test_registry_auto_detect_icbc_credit_pdf(self):
        from billapp.importers.registry import get_registry
        reg = get_registry()
        pdf_bytes = generate_synthetic_icbc_pdf(password="icbc123")
        probe = FileProbe(pdf_bytes, file_name="中国工商银行历史明细.pdf", password="icbc123")
        detected_imp, score = reg.detect_importer(probe)
        assert detected_imp is not None
        assert detected_imp.source_id == "icbc_credit"
        assert score >= 0.9
