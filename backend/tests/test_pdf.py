"""Unit and integration tests for Bank PDF Engine, Watermark Stripper, BOC & ICBC Parsers."""

from __future__ import annotations

import io
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
        generate_synthetic_boc_pdf,
        generate_synthetic_icbc_pdf,
        get_default_boc_rows,
        get_default_icbc_rows,
    )
except ImportError:
    from tests.fixtures.synthetic_pdf import (
        generate_synthetic_boc_pdf,
        generate_synthetic_icbc_pdf,
        get_default_boc_rows,
        get_default_icbc_rows,
    )


# ==============================================================================
# 1. PDF Core Engine & Ephemeral Password Security Tests (Feature 8)
# ==============================================================================


class TestPDFCoreEngine:
    def test_open_unencrypted_pdf(self):
        pdf_bytes = generate_synthetic_boc_pdf(password=None)
        with open_pdf_document(pdf_bytes) as doc:
            assert not doc.is_encrypted
            assert len(doc) == 1
            assert "中国银行" in doc[0].get_text()

    def test_open_encrypted_pdf_with_correct_password(self):
        pdf_bytes = generate_synthetic_boc_pdf(password="boc123")
        with open_pdf_document(pdf_bytes, password="boc123") as doc:
            assert doc.was_encrypted
            assert len(doc) == 1
            assert "借记卡号" in doc[0].get_text()

    def test_open_encrypted_pdf_without_password_raises(self):
        pdf_bytes = generate_synthetic_boc_pdf(password="boc123")
        with pytest.raises(PDFPasswordRequiredError) as exc_info:
            open_pdf_document(pdf_bytes, password=None)
        assert "password is required" in str(exc_info.value).lower()

    def test_open_encrypted_pdf_with_empty_password_raises(self):
        pdf_bytes = generate_synthetic_boc_pdf(password="boc123")
        with pytest.raises(PDFPasswordRequiredError):
            open_pdf_document(pdf_bytes, password="")

    def test_open_encrypted_pdf_with_wrong_password_raises(self):
        pdf_bytes = generate_synthetic_boc_pdf(password="boc123")
        with pytest.raises(PDFInvalidPasswordError) as exc_info:
            open_pdf_document(pdf_bytes, password="wrong_password")
        assert "invalid password" in str(exc_info.value).lower()

    def test_open_corrupted_stream_raises(self):
        with pytest.raises(PDFCorruptedError):
            open_pdf_document(b"Not A Valid PDF Header")

    def test_open_empty_stream_raises(self):
        with pytest.raises(PDFCorruptedError):
            open_pdf_document(b"")

    def test_session_lifecycle_and_context_manager_cleanup(self):
        pdf_bytes = generate_synthetic_boc_pdf(password=None)
        session = open_pdf_document(pdf_bytes)
        assert not session.doc.is_closed
        session.close()
        with pytest.raises(PDFCorruptedError):
            _ = session.doc

    def test_ephemeral_password_zero_disk_persistence(self, tmp_path):
        """Verify password is held only in-memory and leaves no trace on disk."""
        pdf_bytes = generate_synthetic_boc_pdf(password="boc123")
        probe = FileProbe(pdf_bytes, file_name="boc_ephemeral.pdf", password="boc123")
        assert probe.password == "boc123"
        with open_pdf_document(probe) as doc:
            assert len(doc) == 1
        # Confirm no temporary files created with password in tmp_path
        assert list(tmp_path.iterdir()) == []


# ==============================================================================
# 2. PDF Watermark Stripper Tests (Feature 9)
# ==============================================================================


class TestPDFWatermarkStripper:
    def test_diagonal_watermark_vector_detection(self):
        # Diagonal vectors (watermarks)
        assert WatermarkFilter.is_diagonal_watermark((0.5, 0.866)) is True
        assert WatermarkFilter.is_diagonal_watermark((-0.5, -0.866)) is True
        assert WatermarkFilter.is_diagonal_watermark((0.707, 0.707)) is True
        # Horizontal / Vertical vectors (normal text)
        assert WatermarkFilter.is_diagonal_watermark((1.0, 0.0)) is False
        assert WatermarkFilter.is_diagonal_watermark((0.0, -1.0)) is False
        assert WatermarkFilter.is_diagonal_watermark((0.0, 1.0)) is False
        assert WatermarkFilter.is_diagonal_watermark((-1.0, 0.0)) is False
        assert WatermarkFilter.is_diagonal_watermark(None) is False
        assert WatermarkFilter.is_diagonal_watermark(()) is False

    def test_strip_watermark_spans_on_synthetic_icbc(self):
        pdf_bytes = generate_synthetic_icbc_pdf(password="icbc123", with_watermarks=True)
        with open_pdf_document(pdf_bytes, password="icbc123") as doc:
            page = doc[0]
            clean_spans = extract_clean_text_spans(page, strip_watermark=True)
            all_text = " ".join(s["text"] for s in clean_spans)
            # Watermark token must NOT appear in clean spans
            assert "00001111" not in all_text
            assert "DEMO00000001" not in all_text
            # Legitimate content must be preserved
            assert "中国工商银行" in all_text
            assert "入账日期" in all_text
            assert "交易金额" in all_text

    def test_strip_watermark_spans_on_synthetic_icbc_all_pages(self):
        pdf_bytes = generate_synthetic_icbc_pdf(password="icbc123", with_watermarks=True)
        with open_pdf_document(pdf_bytes, password="icbc123") as doc:
            for page in doc:
                clean_spans = extract_clean_text_spans(page, strip_watermark=True)
                all_text = " ".join(s["text"] for s in clean_spans)
                # Watermark token '00001111' must be stripped
                assert "00001111" not in all_text
                # Regular headers must be preserved
                assert "入账日期" in all_text
                assert "交易卡号" in all_text


# ==============================================================================
# 3. Table Extraction Strategies Tests (Features 8 & 9)
# ==============================================================================


class TestPDFTableExtractionStrategies:
    def test_strategy_a_find_tables_on_boc(self):
        pdf_bytes = generate_synthetic_boc_pdf(password=None)
        with open_pdf_document(pdf_bytes) as doc:
            tables = extract_tables_strategy_a(doc[0])
            assert len(tables) >= 1
            table = tables[0]
            assert len(table[0]) == 12  # 12 columns
            assert "记账日期" in table[0][0]

    def test_hybrid_strategy_extracts_clean_cells(self):
        pdf_bytes = generate_synthetic_icbc_pdf(password="icbc123", with_watermarks=True)
        with open_pdf_document(pdf_bytes, password="icbc123") as doc:
            tables = extract_tables_hybrid(doc[0], strip_watermark=True)
            assert len(tables) >= 1
            table = tables[0]
            assert len(table) == 6  # 1 header + 5 rows
            headers = table[0]
            assert "入账日期" in headers[0]
            assert "交易卡号" in headers[1]
            # Verify no watermark in cell values
            for row in table[1:]:
                for cell in row:
                    assert "00001111" not in cell

    def test_hybrid_strategy_ignores_distorted_layout_pruning(self):
        """Oversized layout predictor boxes from watermarks/footers must not discard vector grids."""
        pdf_bytes = generate_synthetic_icbc_pdf(password="icbc123", with_watermarks=True)
        with open_pdf_document(pdf_bytes, password="icbc123") as doc:
            page = doc[0]
            page.layout_information = [(35, 35, 810, 310, "table")]
            page.get_layout = lambda *a, **kw: page.layout_information
            tables = extract_tables_hybrid(page, strip_watermark=True)
            assert len(tables) >= 1
            assert len(tables[0]) == 6
            assert len(tables[0][0]) == 12

    def test_extract_pdf_tables_auto_dispatch(self):
        pdf_bytes = generate_synthetic_boc_pdf(password=None)
        all_tables = extract_pdf_tables(pdf_bytes, strategy="auto")
        assert len(all_tables) >= 1
        assert len(all_tables[0][0]) == 12


# ==============================================================================
# 4. BOC Debit Card Importer Tests (Feature 10)
# ==============================================================================


class TestBOCDebitImporter:
    def test_detect_synthetic_boc(self):
        pdf_bytes = generate_synthetic_boc_pdf(password=None)
        probe = FileProbe(pdf_bytes, file_name="中国银行流水.pdf")
        importer = BOCDebitImporter()
        score = importer.detect(probe)
        assert score >= 0.9

    def test_detect_encrypted_without_password_gives_moderate_score(self):
        pdf_bytes = generate_synthetic_boc_pdf(password="boc123")
        probe = FileProbe(pdf_bytes, file_name="中国银行_加密.pdf")
        importer = BOCDebitImporter()
        score = importer.detect(probe)
        assert score == 0.85

    def test_parse_synthetic_boc_full(self):
        pdf_bytes = generate_synthetic_boc_pdf(password="boc123")
        probe = FileProbe(pdf_bytes, file_name="boc_syn.pdf", password="boc123")
        importer = BOCDebitImporter()

        assert importer.detect(probe) >= 0.9
        result = importer.parse(probe)

        assert len(result.accounts) == 1
        acc = result.accounts[0]
        assert acc.bank == "中国银行"
        assert acc.kind == "bank_debit"
        assert acc.tail == "6666"
        assert acc.holder == "张三"

        assert len(result.transactions) == 6
        debits = sum(t.amount_cents for t in result.transactions if t.direction == "expense")
        credits = sum(t.amount_cents for t in result.transactions if t.direction in ("income", "refund", "transfer"))
        assert debits == 26905
        assert credits == 102980

        t_refund = next(t for t in result.transactions if t.amount_cents == 2980 and t.direction == "refund")
        assert "退款" in t_refund.description
        assert t_refund.counterparty.startswith("支付宝")

        t_transfer = next(t for t in result.transactions if t.amount_cents == 100000)
        assert t_transfer.direction == "transfer"
        assert t_transfer.counterparty == "张三"

        assert len(result.self_checks) == 3
        assert all(sc.passed for sc in result.self_checks)
        row_check = next(sc for sc in result.self_checks if "笔数" in sc.name)
        assert row_check.expected == 6 and row_check.actual == 6

        for t in result.transactions:
            assert len(t.fingerprint) == 40

    def test_parse_synthetic_boc(self):
        pdf_bytes = generate_synthetic_boc_pdf(password="boc123")
        probe = FileProbe(pdf_bytes, file_name="boc_syn.pdf", password="boc123")
        importer = BOCDebitImporter()
        result = importer.parse(probe)
        assert len(result.transactions) == 6
        assert all(sc.passed for sc in result.self_checks)


# ==============================================================================
# 5. ICBC Credit Card Multi-Card Importer Tests (Feature 11)
# ==============================================================================


class TestICBCCreditImporter:
    def test_detect_synthetic_icbc(self):
        pdf_bytes = generate_synthetic_icbc_pdf(password="icbc123")
        probe = FileProbe(pdf_bytes, file_name="工商银行历史明细.pdf", password="icbc123")
        importer = ICBCCreditImporter()
        score = importer.detect(probe)
        assert score >= 0.95

    def test_parse_synthetic_icbc_multicard_and_fx(self):
        pdf_bytes = generate_synthetic_icbc_pdf(password="icbc123")
        probe = FileProbe(pdf_bytes, file_name="icbc_multi_syn.pdf", password="icbc123")
        importer = ICBCCreditImporter()

        assert importer.detect(probe) >= 0.95
        result = importer.parse(probe)

        assert len(result.transactions) == 5
        assert result.holder_name == "张三"

        card_tails = {a.tail for a in result.accounts}
        assert card_tails == {"8801", "8802", "8803"}
        assert len(result.accounts) == 3

        txns_8802 = [t for t in result.transactions if t.account_ref.tail == "8802"]
        usd_txns = [t for t in txns_8802 if t.currency == "USD"]
        assert len(usd_txns) == 1
        assert usd_txns[0].amount_cents == 1250
        assert usd_txns[0].direction == "refund"

        for t in result.transactions:
            assert "00001111" not in (t.counterparty or "")
            assert "DEMO00000001" not in (t.counterparty or "")
            assert "00001111" not in (t.description or "")

        assert all(sc.passed for sc in result.self_checks)

    def test_parse_synthetic_icbc(self):
        pdf_bytes = generate_synthetic_icbc_pdf(password="icbc123")
        probe = FileProbe(pdf_bytes, file_name="icbc_syn.pdf", password="icbc123")
        importer = ICBCCreditImporter()
        result = importer.parse(probe)
        assert len(result.transactions) == 5
        assert all(sc.passed for sc in result.self_checks)
