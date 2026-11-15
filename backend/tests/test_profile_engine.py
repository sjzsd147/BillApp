"""Unit and integration tests for Declarative Bank Profile Engine and Built-in YAML Profiles."""

from __future__ import annotations

import io
from pathlib import Path
import pytest

from billapp.importers.base import FileProbe
from billapp.importers.profile_engine import (
    AmountConfig,
    BankProfile,
    ColumnMappingConfig,
    FieldMapping,
    ProfileImporter,
    TableDetectionConfig,
    get_builtin_profiles_dir,
    load_builtin_profiles,
)
import os
import sys

# Ensure project root and backend directory are in sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

try:
    from backend.tests.fixtures.synthetic_profile import (
        generate_synthetic_profile_csv,
        generate_synthetic_profile_xlsx,
    )
except ImportError:
    from tests.fixtures.synthetic_profile import (
        generate_synthetic_profile_csv,
        generate_synthetic_profile_xlsx,
    )


# ==============================================================================
# Helper YAML Profile Builders (No Guessed Built-in Statement Templates Shipped)
# ==============================================================================


def _make_signed_profile() -> BankProfile:
    return BankProfile.from_yaml(
        """
id: "custom_signed_debit"
name: "自定义借记卡交易明细"
bank: "招商银行"
account_kind: "debit"
file_type: "auto"
encoding: "auto"
delimiter: ","
currency: "CNY"
version: 1
table_detection:
  header_row_pattern: "交易日期.*交易金额"
  header_row_keywords: ["交易日期", "交易时间", "交易摘要", "交易金额", "账户余额"]
  data_start_offset: 1
  skip_row_patterns: ["^合计", "^小计", "^总计", "^[-=_]{4,}$"]
  stop_pattern: "^(合计|小计|总计|重要提示)"
metadata_extraction:
  card_tail:
    pattern: "(?:账号|卡号)[：:]\\\\s*(?:[A-Za-z0-9*]+)?(\\\\d{4})"
    group: 1
  holder_name:
    pattern: "(?:户名|姓名)[：:]\\\\s*([^\\\\s,，\\\\t\\\\r\\\\n]+)"
    group: 1
  currency:
    pattern: "币别[：:]\\\\s*([^\\\\s,，\\\\t\\\\r\\\\n]+)"
    group: 1
    default: "CNY"
column_mapping:
  date:
    header: ["交易日期", "记账日期", "日期"]
  time:
    header: ["交易时间", "时间"]
    optional: true
  amount:
    header: ["交易金额", "金额"]
  balance:
    header: ["账户余额", "余额"]
    optional: true
  description:
    header: ["交易摘要", "摘要"]
    optional: true
  counterparty:
    header: ["对方户名", "对方账户", "交易对方"]
    optional: true
amount_mode:
  type: "signed"
  negative_means: "expense"
  amount_col: "交易金额"
date_time:
  date_col: "交易日期"
  time_col: "交易时间"
  date_format: "%Y-%m-%d"
  time_format: "%H:%M:%S"
direction_rules:
  refund:
    keywords: ["退款", "退货", "冲正"]
    regex: ["(?i)refund", "退款"]
  transfer:
    keywords: ["还款", "理财", "提现", "转账到零钱"]
    regex: ["还款", "转账.*本人", "理财"]
"""
    )


def _make_split_profile() -> BankProfile:
    return BankProfile.from_yaml(
        """
id: "custom_split_debit"
name: "自定义借记卡对账单"
bank: "建设银行"
account_kind: "debit"
file_type: "auto"
encoding: "auto"
delimiter: ","
currency: "CNY"
version: 1
table_detection:
  header_row_pattern: "交易日期.*(?:借方金额|支出金额)"
  header_row_keywords: ["交易日期", "摘要", "借方金额", "贷方金额"]
  data_start_offset: 1
  skip_row_patterns: ["^合计", "^小计", "^总计", "^[-=_]{4,}$"]
  stop_pattern: "^(合计|小计|总计|重要提示)"
metadata_extraction:
  card_tail:
    pattern: "(?:卡号|账号)[：:]\\\\s*(?:[A-Za-z0-9*]+)?(\\\\d{4})"
    group: 1
  holder_name:
    pattern: "(?:户名|姓名)[：:]\\\\s*([^\\\\s,，\\\\t\\\\r\\\\n]+)"
    group: 1
column_mapping:
  date:
    header: ["交易日期", "记账日", "日期"]
  time:
    header: ["交易时间", "时间"]
    optional: true
  expense_amount:
    header: ["借方金额", "支出金额", "支出", "借方"]
  income_amount:
    header: ["贷方金额", "收入金额", "收入", "贷方"]
  balance:
    header: ["余额", "账户余额"]
    optional: true
  description:
    header: ["摘要", "交易摘要"]
    optional: true
  counterparty:
    header: ["对方账户", "对方户名", "交易对方"]
    optional: true
amount_mode:
  type: "split"
  expense_col: "借方金额"
  income_col: "贷方金额"
date_time:
  date_col: "交易日期"
  time_col: "交易时间"
  date_format: "%Y-%m-%d"
  time_format: "%H:%M:%S"
direction_rules:
  refund:
    keywords: ["退款", "退货"]
    regex: ["(?i)refund", "退款"]
  transfer:
    keywords: ["还款", "理财", "提现"]
    regex: ["还款", "转账.*本人", "理财"]
"""
    )


def _make_flag_profile() -> BankProfile:
    return BankProfile.from_yaml(
        """
id: "custom_flag_credit"
name: "自定义信用卡对账单"
bank: "平安银行"
account_kind: "credit"
file_type: "auto"
encoding: "auto"
delimiter: ","
currency: "CNY"
version: 1
table_detection:
  header_row_pattern: "交易日期.*(?:借贷|借贷标志)"
  header_row_keywords: ["交易日期", "交易金额", "借贷"]
  data_start_offset: 1
  skip_row_patterns: ["^合计", "^小计", "^总计", "^[-=_]{4,}$"]
  stop_pattern: "^(合计|小计|总计|重要提示)"
metadata_extraction:
  card_tail:
    pattern: "(?:卡号|账号)[：:]\\\\s*(?:[A-Za-z0-9*]+)?(\\\\d{4})"
    group: 1
  holder_name:
    pattern: "(?:姓名|户名)[：:]\\\\s*([^\\\\s,，\\\\t\\\\r\\\\n]+)"
    group: 1
column_mapping:
  date:
    header: ["交易日期", "记账日", "日期"]
  time:
    header: ["交易时间", "时间"]
    optional: true
  amount:
    header: ["交易金额", "金额", "记账金额"]
  direction:
    header: ["借贷", "借贷标志", "收支"]
  balance:
    header: ["账户余额", "余额"]
    optional: true
  description:
    header: ["交易摘要", "摘要"]
    optional: true
  counterparty:
    header: ["对方户名", "对方账户", "商户名称", "交易对方"]
    optional: true
amount_mode:
  type: "flag"
  amount_col: "交易金额"
  direction_col: "借贷"
  flag_mapping:
    "借": "expense"
    "支出": "expense"
    "消费": "expense"
    "贷": "income"
    "收入": "income"
    "还款": "transfer"
  default_direction: "expense"
date_time:
  date_col: "交易日期"
  time_col: "交易时间"
  date_format: "%Y-%m-%d"
  time_format: "%H:%M:%S"
direction_rules:
  refund:
    keywords: ["退款", "退货"]
    regex: ["(?i)refund", "退款"]
  transfer:
    keywords: ["还款", "理财"]
    regex: ["还款", "转账.*本人"]
"""
    )


# ==============================================================================
# 1. Bank Profile YAML Validation & Builtin Profiles (Feature 12 & 13)
# ==============================================================================


class TestBankProfileValidation:
    def test_no_guessed_builtin_profiles_shipped(self):
        profiles = load_builtin_profiles()
        assert profiles == {}

    def test_signed_profile_structure(self):
        signed = _make_signed_profile()
        assert signed.bank == "招商银行"
        assert signed.account_kind == "debit"
        assert signed.account_kind_full == "bank_debit"
        assert signed.amount_mode.type == "signed"
        assert signed.amount_mode.negative_means == "expense"

    def test_split_profile_structure(self):
        split = _make_split_profile()
        assert split.bank == "建设银行"
        assert split.account_kind == "debit"
        assert split.account_kind_full == "bank_debit"
        assert split.amount_mode.type == "split"
        assert split.amount_mode.expense_col == "借方金额"
        assert split.amount_mode.income_col == "贷方金额"

    def test_flag_profile_structure(self):
        flag = _make_flag_profile()
        assert flag.bank == "平安银行"
        assert flag.account_kind == "credit"
        assert flag.account_kind_full == "bank_credit"
        assert flag.amount_mode.type == "flag"
        assert flag.amount_mode.direction_col == "借贷"
        assert flag.amount_mode.flag_mapping["借"] == "expense"
        assert flag.amount_mode.flag_mapping["贷"] == "income"

    def test_invalid_mode_raises_error(self):
        yaml_content = """
        id: "invalid_mode"
        name: "Test"
        bank: "TestBank"
        account_kind: "debit"
        column_mapping:
          date: "交易日期"
        amount_mode:
          type: "nonexistent_mode"
        """
        with pytest.raises(Exception):
            BankProfile.from_yaml(yaml_content)

    def test_missing_date_column_raises_error(self):
        yaml_content = """
        id: "missing_date"
        name: "Test"
        bank: "TestBank"
        account_kind: "debit"
        column_mapping: {}
        amount_mode:
          type: "signed"
        """
        with pytest.raises(Exception):
            BankProfile.from_yaml(yaml_content)

    def test_profile_serialization_roundtrip(self):
        signed = _make_signed_profile()
        y_str = signed.to_yaml()
        loaded = BankProfile.from_yaml(y_str)
        assert loaded.id == signed.id
        assert loaded.amount_mode.type == signed.amount_mode.type


# ==============================================================================
# 2. Profile Importer Signed Amount Mode Tests (Feature 12 - signed)
# ==============================================================================


class TestProfileImporterSignedMode:
    def test_signed_csv_parsing(self):
        signed = _make_signed_profile()
        importer = ProfileImporter(signed)

        csv_bytes = generate_synthetic_profile_csv(mode="signed", bank_name="招商银行", card_tail="1234")
        probe = FileProbe(csv_bytes, file_name="自定义流水.csv")

        # Detection
        score = importer.detect(probe)
        assert score >= 0.8

        # Parse
        result = importer.parse(probe)
        assert len(result.transactions) == 3

        # Account check
        assert len(result.accounts) == 1
        acc = result.accounts[0]
        assert acc.bank == "招商银行"
        assert acc.tail == "1234"
        assert acc.kind == "bank_debit"

        # Row 1: -35.50 expense
        t0 = next(t for t in result.transactions if t.amount_cents == 3550 and t.direction == "expense")
        assert t0.counterparty == "美团外卖"
        assert t0.time_precision == "second"

        # Row 2: 5000.00 income
        t1 = next(t for t in result.transactions if t.amount_cents == 500000)
        assert t1.direction == "income"
        assert t1.counterparty == "科技有限公司"

        # Row 3: 35.50 with "退款" in description -> overridden to refund
        t2 = next(t for t in result.transactions if t.amount_cents == 3550 and t.direction == "refund")
        assert "退款" in t2.description

        # Self-checks
        assert len(result.self_checks) >= 1
        assert all(sc.passed for sc in result.self_checks)

        # Fingerprints
        for t in result.transactions:
            assert len(t.fingerprint) == 40


# ==============================================================================
# 3. Profile Importer Split Amount Mode Tests (Feature 12 - split)
# ==============================================================================


class TestProfileImporterSplitMode:
    def test_split_xlsx_parsing(self):
        split = _make_split_profile()
        importer = ProfileImporter(split)

        xlsx_bytes = generate_synthetic_profile_xlsx(mode="split", bank_name="建设银行", card_tail="5678")
        probe = FileProbe(xlsx_bytes, file_name="自定义流水.xlsx")

        score = importer.detect(probe)
        assert score >= 0.8

        result = importer.parse(probe)
        assert len(result.transactions) == 3

        assert len(result.accounts) == 1
        acc = result.accounts[0]
        assert acc.bank == "建设银行"
        assert acc.tail == "5678"
        assert acc.kind == "bank_debit"

        # Row 1: 100.00 income (提现到账)
        t_inc = next(t for t in result.transactions if t.amount_cents == 10000)
        assert t_inc.direction in ("income", "transfer")

        # Row 2: 55.00 expense (POS消费)
        t_exp = next(t for t in result.transactions if t.amount_cents == 5500 and t.direction == "expense")
        assert t_exp.counterparty == "联华超市"

        # Row 3: 55.00 refund (退款)
        t_ref = next(t for t in result.transactions if t.amount_cents == 5500 and t.direction == "refund")
        assert "退款" in t_ref.description


# ==============================================================================
# 4. Profile Importer Flag Amount Mode Tests (Feature 12 - flag)
# ==============================================================================


class TestProfileImporterFlagMode:
    def test_flag_csv_parsing(self):
        flag = _make_flag_profile()
        importer = ProfileImporter(flag)

        csv_bytes = generate_synthetic_profile_csv(mode="flag", bank_name="平安银行", card_tail="1234")
        probe = FileProbe(csv_bytes, file_name="自定义对账单.csv")

        score = importer.detect(probe)
        assert score >= 0.8

        result = importer.parse(probe)
        assert len(result.transactions) == 3

        assert len(result.accounts) == 1
        acc = result.accounts[0]
        assert acc.bank == "平安银行"
        assert acc.tail == "1234"
        assert acc.kind == "bank_credit"

        # Row 1: 35.50 '借' -> expense
        t_exp = next(t for t in result.transactions if t.amount_cents == 3550 and t.direction == "expense")
        assert t_exp.counterparty == "美团外卖"

        # Row 2: 5000.00 '贷' -> income
        t_inc = next(t for t in result.transactions if t.amount_cents == 500000)
        assert t_inc.direction == "income"

        # Row 3: 35.50 '贷' + "退款" -> refund
        t_ref = next(t for t in result.transactions if t.amount_cents == 3550 and t.direction == "refund")
        assert "退款" in t_ref.description


# ==============================================================================
# 5. Row Filtering, Summary Skipping & Boundary Tests
# ==============================================================================


class TestRowFilteringAndBoundaries:
    def test_skip_summary_rows_not_parsed_into_transactions(self):
        signed = _make_signed_profile()
        importer = ProfileImporter(signed)

        csv_bytes = generate_synthetic_profile_csv(mode="signed", bank_name="招商银行")
        probe = FileProbe(csv_bytes, file_name="statement.csv")
        result = importer.parse(probe)

        # Ensure summary row "合计: 支出 35.50 收入 5035.50" was not parsed
        for t in result.transactions:
            assert "合计" not in (t.description or "")
            assert "小计" not in (t.description or "")
            assert "总计" not in (t.description or "")
        assert len(result.transactions) == 3

    def test_preamble_metadata_regex_extraction(self):
        split = _make_split_profile()
        importer = ProfileImporter(split)

        text = "中国建设银行对账单\n户名：张三\n卡号：6217000000005678\n"
        tail, holder, p_start, p_end = importer._extract_preamble_metadata(text)
        assert tail == "5678"
        assert holder == "张三"
