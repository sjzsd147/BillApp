"""Adversarial stress-test suite for Milestone 2 Declarative Profile Engine (Feature 12 & 13).

Probing areas:
1. Profile Validation Robustness (malformed YAML, schema errors, conflicting modes)
2. Amount Modes Edge Cases (signed, split, flag, zero amounts, formatting, scientific notation, dashes)
3. Row Filtering & Preamble Extraction (false positive '合计' counterparty, stop pattern boundary, regex edge cases)
4. Semantic Direction Overrides (refund vs transfer interactions)
5. Multi-page repeated header and boundary leaks
"""

from __future__ import annotations

import io
from datetime import datetime
from decimal import Decimal
from pathlib import Path
import pytest
import yaml
from pydantic import ValidationError

from billapp.core.models import ParseResult, TransactionCreate
from billapp.core.money import to_cents
from billapp.importers.base import FileProbe
from billapp.importers.profile_engine import (
    AmountConfig,
    BankProfile,
    ColumnMappingConfig,
    FieldMapping,
    MetadataExtractionConfig,
    PeriodRegexRule,
    PreambleRegexRule,
    ProfileImporter,
    TableDetectionConfig,
)


# ==============================================================================
# Helper to build minimal valid BankProfile YAML or dict
# ==============================================================================

def make_profile_dict(
    amount_mode_type: str = "signed",
    negative_means: str = "expense",
    extra_amount_fields: dict | None = None,
    column_mapping_overrides: dict | None = None,
    table_detection_overrides: dict | None = None,
    metadata_overrides: dict | None = None,
) -> dict:
    amt_cfg: dict = {
        "type": amount_mode_type,
        "negative_means": negative_means,
    }
    if extra_amount_fields:
        amt_cfg.update(extra_amount_fields)

    col_map: dict = {
        "date": "交易日期",
        "time": "交易时间",
        "counterparty": "交易对方",
        "description": "交易摘要",
    }
    if amount_mode_type == "signed":
        col_map["amount"] = "交易金额"
    elif amount_mode_type == "split":
        col_map["expense_amount"] = "支出金额"
        col_map["income_amount"] = "收入金额"
    elif amount_mode_type == "flag":
        col_map["amount"] = "交易金额"
        col_map["direction"] = "借贷"

    if column_mapping_overrides:
        col_map.update(column_mapping_overrides)

    tbl_cfg: dict = {
        "header_row_pattern": "交易日期",
        "data_start_offset": 1,
        "skip_row_patterns": ["^合计", "^小计", "^总计"],
        "stop_pattern": "^(合计|小计|总计|重要提示)",
    }
    if table_detection_overrides:
        tbl_cfg.update(table_detection_overrides)

    return {
        "id": "adversarial_bank",
        "name": "Adversarial Bank",
        "bank": "测试银行",
        "account_kind": "debit",
        "file_type": "csv",
        "encoding": "utf-8",
        "delimiter": ",",
        "currency": "CNY",
        "version": 1,
        "table_detection": tbl_cfg,
        "metadata_extraction": metadata_overrides or {},
        "column_mapping": col_map,
        "amount_mode": amt_cfg,
    }


# ==============================================================================
# 1. Profile Validation Robustness
# ==============================================================================

class TestProfileValidationRobustness:
    """Stress-test BankProfile Pydantic schema with invalid, malformed, or hostile inputs."""

    def test_malformed_yaml_syntax(self):
        """Invalid YAML syntax should raise yaml.YAMLError or ValueError."""
        bad_yamls = [
            ": : : invalid yaml",
            "key: [unclosed list",
            "foo: 'bar\n  bad indent",
        ]
        for bad_y in bad_yamls:
            with pytest.raises((yaml.YAMLError, ValueError)):
                BankProfile.from_yaml(bad_y)

    def test_non_dict_yaml(self):
        """YAML documents that decode to scalars or lists must be rejected."""
        non_dicts = [
            "123",
            '"just a string"',
            "- item1\n- item2",
            "null",
            "",
        ]
        for nd in non_dicts:
            with pytest.raises(ValueError, match="must decode to a dictionary"):
                BankProfile.from_yaml(nd)

    @pytest.mark.parametrize(
        "missing_field",
        ["id", "name", "bank", "account_kind", "column_mapping", "amount_mode"],
    )
    def test_missing_required_fields_raises_validation_error(self, missing_field: str):
        """Omitting any top-level mandatory field in BankProfile raises ValidationError."""
        d = make_profile_dict()
        del d[missing_field]
        with pytest.raises(ValidationError) as exc_info:
            BankProfile(**d)
        errors = exc_info.value.errors()
        assert any(missing_field in str(e["loc"]) for e in errors)

    @pytest.mark.parametrize(
        "field,invalid_val",
        [
            ("account_kind", "savings"),
            ("account_kind", "credit_card"),
            ("file_type", "pdf_or_csv"),
            ("file_type", "docx"),
        ],
    )
    def test_invalid_literal_fields_raise_validation_error(self, field: str, invalid_val: str):
        """Literals like account_kind and file_type must strictly conform to allowed enum values."""
        d = make_profile_dict()
        d[field] = invalid_val
        with pytest.raises(ValidationError):
            BankProfile(**d)

    def test_amount_mode_invalid_type(self):
        """AmountConfig type must be one of signed, split, flag."""
        d = make_profile_dict()
        d["amount_mode"]["type"] = "hybrid_custom"
        with pytest.raises(ValidationError):
            BankProfile(**d)

    def test_amount_mode_invalid_negative_means(self):
        """negative_means must be either 'expense' or 'income'."""
        d = make_profile_dict()
        d["amount_mode"]["negative_means"] = "neutral"
        with pytest.raises(ValidationError):
            BankProfile(**d)

    def test_amount_mode_split_defaults(self):
        """Split mode without explicit column names defaults to '支出金额' and '收入金额'."""
        d = make_profile_dict("split")
        d["amount_mode"].pop("expense_col", None)
        d["amount_mode"].pop("income_col", None)
        prof = BankProfile(**d)
        assert prof.amount_mode.expense_col == "支出金额"
        assert prof.amount_mode.income_col == "收入金额"

    def test_amount_mode_flag_defaults(self):
        """Flag mode without explicit direction_col or flag_mapping fills standard defaults."""
        d = make_profile_dict("flag")
        d["amount_mode"].pop("direction_col", None)
        d["amount_mode"].pop("flag_mapping", None)
        prof = BankProfile(**d)
        assert prof.amount_mode.direction_col == "借贷"
        assert "借" in prof.amount_mode.flag_mapping
        assert "贷" in prof.amount_mode.flag_mapping

    def test_conflicting_amount_mode_at_parse_time(self):
        """If profile defines split mode but header only has single amount column, parser fails fast."""
        d = make_profile_dict("split")
        prof = BankProfile(**d)
        importer = ProfileImporter(prof)

        csv_content = "交易日期,交易时间,交易金额,交易对方\n2026-11-09,10:00:00,50.00,超市\n"
        probe = FileProbe(csv_content.encode("utf-8"), file_name="statement.csv")
        with pytest.raises(ValueError, match="Split mode requires expense and income columns"):
            importer.parse(probe)

    def test_signed_mode_missing_amount_column_at_parse_time(self):
        """If profile defines signed mode but header lacks amount column, parser fails fast."""
        d = make_profile_dict("signed")
        prof = BankProfile(**d)
        importer = ProfileImporter(prof)

        csv_content = "交易日期,交易时间,交易对方,交易摘要\n2026-11-09,10:00:00,超市,买菜\n"
        probe = FileProbe(csv_content.encode("utf-8"), file_name="statement.csv")
        with pytest.raises(ValueError, match="Signed mode requires amount column"):
            importer.parse(probe)


# ==============================================================================
# 2. Amount Modes Edge Cases
# ==============================================================================

class TestAmountModesEdgeCases:
    """Stress-test signed, split, and flag modes with boundary monetary numbers and formats."""

    def test_signed_negative_means_expense(self):
        """Signed mode with negative_means='expense': negative is expense, positive is income."""
        d = make_profile_dict("signed", negative_means="expense")
        prof = BankProfile(**d)
        importer = ProfileImporter(prof)

        csv_text = (
            "交易日期,交易时间,交易金额,交易对方,交易摘要\n"
            "2026-11-09,10:00:00,-123.45,美团,午餐\n"
            "2026-11-09,11:00:00,500.00,公司,劳务报酬\n"
            "2026-11-09,12:00:00,0.00,银行,结息\n"
        )
        probe = FileProbe(csv_text.encode("utf-8"), file_name="test.csv")
        res = importer.parse(probe)
        assert len(res.transactions) == 3

        t_exp = res.transactions[0]
        assert t_exp.amount_cents == 12345
        assert t_exp.direction == "expense"

        t_inc = res.transactions[1]
        assert t_inc.amount_cents == 50000
        assert t_inc.direction == "income"

        t_zero = res.transactions[2]
        assert t_zero.amount_cents == 0
        assert t_zero.direction == "neutral"

    def test_signed_negative_means_income(self):
        """Signed mode with negative_means='income': negative is income, positive is expense."""
        d = make_profile_dict("signed", negative_means="income")
        prof = BankProfile(**d)
        importer = ProfileImporter(prof)

        csv_text = (
            "交易日期,交易时间,交易金额,交易对方,交易摘要\n"
            "2026-11-09,10:00:00,-200.00,公司,薪资\n"
            "2026-11-09,11:00:00,150.00,商户,购物\n"
        )
        probe = FileProbe(csv_text.encode("utf-8"), file_name="test.csv")
        res = importer.parse(probe)
        assert len(res.transactions) == 2

        t_inc = res.transactions[0]
        assert t_inc.amount_cents == 20000
        assert t_inc.direction == "income"

        t_exp = res.transactions[1]
        assert t_exp.amount_cents == 15000
        assert t_exp.direction == "expense"

    def test_signed_thousand_separators_and_symbols(self):
        """Amount column with commas, currency symbols, and spaces should parse accurately."""
        d = make_profile_dict("signed", negative_means="expense")
        prof = BankProfile(**d)
        importer = ProfileImporter(prof)

        csv_text = (
            "交易日期,交易时间,交易金额,交易对方,交易摘要\n"
            "2026-11-09,10:00:00,\"-¥1,234.56\",商场,购物\n"
            "2026-11-09,11:00:00,\"12,345,678.90\",公司,转账\n"
            "2026-11-09,12:00:00,\"$ 99.99\",海淘,海外购\n"
        )
        probe = FileProbe(csv_text.encode("utf-8"), file_name="test.csv")
        res = importer.parse(probe)
        assert len(res.transactions) == 3
        assert res.transactions[0].amount_cents == 123456
        assert res.transactions[0].direction == "expense"
        assert res.transactions[1].amount_cents == 1234567890
        assert res.transactions[1].direction == "income"
        assert res.transactions[2].amount_cents == 9999
        assert res.transactions[2].direction == "income"

    def test_split_mode_both_columns_have_numbers(self):
        """Split mode with both expense and income columns populated in the same row."""
        d = make_profile_dict("split")
        prof = BankProfile(**d)
        importer = ProfileImporter(prof)

        csv_text = (
            "交易日期,交易时间,支出金额,收入金额,交易对方,交易摘要\n"
            "2026-11-09,10:00:00,100.00,40.00,超市,扣款与部分抵扣\n"
            "2026-11-09,11:00:00,30.00,80.00,平台,返现抵扣\n"
            "2026-11-09,12:00:00,50.00,50.00,平账,平账记录\n"
        )
        probe = FileProbe(csv_text.encode("utf-8"), file_name="test.csv")
        res = importer.parse(probe)
        assert len(res.transactions) == 3

        # Row 1: expense 100 - income 40 = net expense 60
        t0 = res.transactions[0]
        assert t0.amount_cents == 6000
        assert t0.direction == "expense"

        # Row 2: income 80 - expense 30 = net income 50
        t1 = res.transactions[1]
        assert t1.amount_cents == 5000
        assert t1.direction == "income"

        # Row 3: expense 50 - income 50 = net 0
        t2 = res.transactions[2]
        assert t2.amount_cents == 0
        # NOTE: When exp_cents == inc_cents > 0, direction currently evaluates to 'expense'
        assert t2.direction in ("expense", "neutral")

    def test_split_mode_empty_or_zero(self):
        """Split mode with empty strings and '0.00' handles zero cash flows gracefully."""
        d = make_profile_dict("split")
        prof = BankProfile(**d)
        importer = ProfileImporter(prof)

        csv_text = (
            "交易日期,交易时间,支出金额,收入金额,交易对方,交易摘要\n"
            "2026-11-09,10:00:00,35.50,,美团,外卖\n"
            "2026-11-09,11:00:00,,500.00,公司,兼职\n"
            "2026-11-09,12:00:00,,0.00,测试,零元记录\n"
        )
        probe = FileProbe(csv_text.encode("utf-8"), file_name="test.csv")
        res = importer.parse(probe)
        assert len(res.transactions) == 3
        assert res.transactions[0].amount_cents == 3550
        assert res.transactions[0].direction == "expense"
        assert res.transactions[1].amount_cents == 50000
        assert res.transactions[1].direction == "income"
        assert res.transactions[2].amount_cents == 0
        assert res.transactions[2].direction == "neutral"

    def test_to_cents_double_dash_bug_demonstration(self):
        """VULNERABILITY PROBE: to_cents('--') crashes with ValueError instead of returning 0 cents."""
        # Single hyphen is handled
        assert to_cents("-") == 0
        # But double dash '--' (standard bank placeholder for empty cells) raises ValueError!
        with pytest.raises(ValueError, match="Invalid monetary value: '--'"):
            to_cents("--")

    def test_scientific_notation_parsing_fixed(self):
        """Regression guard (B-08): to_cents('1e3') parses scientific notation accurately as 1000.00 yuan."""
        assert to_cents("1e3") == 100000
        assert to_cents("1.5e2") == 15000
        assert to_cents("-2.5E+1") == -2500

    def test_flag_mode_unexpected_flag_and_whitespace(self):
        """Flag mode handles whitespace padding and falls back to default_direction on unknown flags."""
        d = make_profile_dict("flag")
        d["amount_mode"]["default_direction"] = "expense"
        prof = BankProfile(**d)
        importer = ProfileImporter(prof)

        csv_text = (
            "交易日期,交易时间,交易金额,借贷,交易对方,交易摘要\n"
            "2026-11-09,10:00:00,50.00, 借 ,超市,买菜\n"
            "2026-11-09,11:00:00,100.00, 贷 ,公司,奖金\n"
            "2026-11-09,12:00:00,30.00,UNKNOWN_FLAG,未知,未知流向\n"
            "2026-11-09,13:00:00,0.00,借,零元,零元测试\n"
        )
        probe = FileProbe(csv_text.encode("utf-8"), file_name="test.csv")
        res = importer.parse(probe)
        assert len(res.transactions) == 4

        # Whitespace padded ' 借 ' -> expense
        assert res.transactions[0].amount_cents == 5000
        assert res.transactions[0].direction == "expense"

        # Whitespace padded ' 贷 ' -> income
        assert res.transactions[1].amount_cents == 10000
        assert res.transactions[1].direction == "income"

        # Unknown flag -> falls back to default_direction 'expense'
        assert res.transactions[2].amount_cents == 3000
        assert res.transactions[2].direction == "expense"

        # Zero amount -> neutral regardless of flag
        assert res.transactions[3].amount_cents == 0
        assert res.transactions[3].direction == "neutral"

    def test_flag_mode_case_sensitivity_vulnerability(self):
        """VULNERABILITY PROBE: flag_mapping lookup is case-sensitive, misclassifying lowercase 'cr' as expense."""
        d = make_profile_dict("flag")
        d["amount_mode"]["flag_mapping"] = {"CR": "income", "DR": "expense"}
        d["amount_mode"]["default_direction"] = "expense"
        prof = BankProfile(**d)
        importer = ProfileImporter(prof)

        csv_text = "交易日期,交易时间,交易金额,借贷\n2026-11-09,10:00:00,100.00,cr\n"
        probe = FileProbe(csv_text.encode("utf-8"), file_name="test.csv")
        res = importer.parse(probe)
        # Because 'cr' does not match 'CR', it silently falls back to default_direction ('expense')!
        assert res.transactions[0].direction == "expense"

    def test_split_mode_equal_amounts_yields_expense_instead_of_neutral_bug(self):
        """VULNERABILITY PROBE: In split mode, exp_cents == inc_cents > 0 produces 0 cents with direction='expense'."""
        d = make_profile_dict("split")
        prof = BankProfile(**d)
        importer = ProfileImporter(prof)

        csv_text = "交易日期,交易时间,支出金额,收入金额\n2026-11-09,10:00:00,50.00,50.00\n"
        probe = FileProbe(csv_text.encode("utf-8"), file_name="test.csv")
        res = importer.parse(probe)
        t = res.transactions[0]
        assert t.amount_cents == 0
        # Bug: Should be 'neutral' like other 0-cent transactions, but evaluates to 'expense'
        assert t.direction == "expense"


# ==============================================================================
# 3. Row Filtering & Preamble Extraction
# ==============================================================================

class TestRowFilteringAndPreambleExtraction:
    """Stress-test row filtering (avoiding false positive skip on genuine transactions) and regex extraction."""

    def test_summary_row_with_date_parsed_as_phantom_transaction_bug(self):
        """VULNERABILITY PROBE: If summary row contains a date, ^合计 anchor fails and summary is parsed as transaction."""
        d = make_profile_dict("signed")
        prof = BankProfile(**d)
        importer = ProfileImporter(prof)

        csv_text = (
            "交易日期,交易时间,交易金额,交易对方,交易摘要\n"
            "2026-11-08,10:00:00,-50.00,商户A,正常交易\n"
            "2026-11-09,00:00:00,50.00,合计,总计支出50\n"  # Summary row formatted with date
        )
        probe = FileProbe(csv_text.encode("utf-8"), file_name="summary_date.csv")
        res = importer.parse(probe)
        # Bug: Summary row is parsed as a real transaction!
        assert len(res.transactions) == 2
        assert res.transactions[1].counterparty == "合计"
        assert res.transactions[1].amount_cents == 5000

    def test_genuine_transactions_with_heji_in_counterparty_not_skipped_when_date_first(self):
        """When date is column 0, transactions with '合计' in merchant name are NOT skipped by ^合计."""
        d = make_profile_dict("signed")
        prof = BankProfile(**d)
        importer = ProfileImporter(prof)

        csv_text = (
            "测试银行个人对账单\n"
            "卡号: 6214000000001234\n"
            "户名: 张三\n"
            "----------------------------------------\n"
            "交易日期,交易时间,交易金额,交易对方,交易摘要\n"
            "2026-11-09,10:00:00,-50.00,合计百货超市,日常购物\n"
            "2026-11-09,11:00:00,-20.00,小计文具店,办公用品\n"
            "2026-11-09,12:00:00,-88.00,总计生鲜市场,买菜\n"
            "2026-11-09,13:00:00,1000.00,普通商户,正常入账\n"
            "合计: 支出 158.00 收入 1000.00\n"
        )
        probe = FileProbe(csv_text.encode("utf-8"), file_name="heji_test.csv")
        res = importer.parse(probe)

        # All 4 transactions must be parsed!
        assert len(res.transactions) == 4
        counterparties = [t.counterparty for t in res.transactions]
        assert "合计百货超市" in counterparties
        assert "小计文具店" in counterparties
        assert "总计生鲜市场" in counterparties
        assert "普通商户" in counterparties

        # Real summary row must NOT be parsed as a transaction
        for t in res.transactions:
            assert t.counterparty != "合计: 支出 158.00 收入 1000.00"

    def test_column0_counterparty_heji_premature_termination_bug(self):
        """VULNERABILITY PROBE: When counterparty is col 0, merchant '合计百货超市' prematurely terminates parsing."""
        d = make_profile_dict(
            "signed",
            column_mapping_overrides={"counterparty": "对方户名", "date": "交易日期", "amount": "金额"},
            table_detection_overrides={
                "header_row_pattern": "对方户名",
                "stop_pattern": "^(合计|小计|总计)",
            },
        )
        prof = BankProfile(**d)
        importer = ProfileImporter(prof)

        # Counterparty is first column; merchant name starts with '合计'
        csv_data = "对方户名,交易日期,金额\n合计百货超市,2026-11-09,-50.00\n正常商户,2026-11-09,-20.00\n"
        probe = FileProbe(csv_data.encode("utf-8"), file_name="col0_test.csv")

        # Parser stops prematurely on '合计百货超市', resulting in 0 parsed rows!
        with pytest.raises(ValueError, match="No valid transaction rows parsed"):
            importer.parse(probe)

    def test_genuine_transactions_with_heji_in_description_not_skipped(self):
        """Transactions with '合计' in description (e.g. '餐饮消费合计') MUST NOT be skipped."""
        d = make_profile_dict("signed")
        prof = BankProfile(**d)
        importer = ProfileImporter(prof)

        csv_text = (
            "交易日期,交易时间,交易金额,交易对方,交易摘要\n"
            "2026-11-09,10:00:00,-35.00,美团,餐饮小计优惠后合计\n"
            "2026-11-09,11:00:00,-10.00,微信,总计费用补差\n"
        )
        probe = FileProbe(csv_text.encode("utf-8"), file_name="desc_test.csv")
        res = importer.parse(probe)
        assert len(res.transactions) == 2
        assert "合计" in res.transactions[0].description
        assert "总计" in res.transactions[1].description

    def test_stop_pattern_cleanly_stops_without_junk_rows(self):
        """Table parsing terminates immediately when stop_pattern is hit, dropping footer notes."""
        d = make_profile_dict("signed")
        prof = BankProfile(**d)
        importer = ProfileImporter(prof)

        csv_text = (
            "交易日期,交易时间,交易金额,交易对方,交易摘要\n"
            "2026-11-08,10:00:00,-50.00,商户A,正常交易1\n"
            "2026-11-09,11:00:00,-60.00,商户B,正常交易2\n"
            "重要提示: 请妥善保管您的对账单，如有疑问请致电客服 95555\n"
            "2026-11-10,12:00:00,-999.00,垃圾行,此行在提示之后不应被解析\n"
            "注：最终解释权归银行所有\n"
        )
        probe = FileProbe(csv_text.encode("utf-8"), file_name="stop_test.csv")
        res = importer.parse(probe)
        assert len(res.transactions) == 2
        assert [t.counterparty for t in res.transactions] == ["商户A", "商户B"]

    def test_repeated_header_is_skipped_in_multipage_statements(self):
        """Regression test: repeated header rows in multi-page statements are skipped cleanly."""
        d = make_profile_dict("signed")
        prof = BankProfile(**d)
        importer = ProfileImporter(prof)

        csv_text = (
            "交易日期,交易时间,交易金额,交易对方,交易摘要\n"
            "2026-11-08,10:00:00,-50.00,商户A,正常交易1\n"
            "交易日期,交易时间,交易金额,交易对方,交易摘要\n"  # Repeated header from page 2
            "2026-11-09,11:00:00,-60.00,商户B,正常交易2\n"
        )
        probe = FileProbe(csv_text.encode("utf-8"), file_name="repeat_header.csv")
        res = importer.parse(probe)

        assert len(res.transactions) == 2
        assert [t.counterparty for t in res.transactions] == ["商户A", "商户B"]

    def test_preamble_metadata_extraction_edge_cases(self):
        """Test regex metadata extraction with unusual card formats, spaces, and periods."""
        d = make_profile_dict(
            metadata_overrides={
                "card_tail": {
                    "pattern": r"(?:卡号|账号)[：:\s]+(?:[A-Za-z0-9*\- ]+)?(\d{4})",
                    "group": 1,
                },
                "holder_name": {
                    "pattern": r"(?:户名|客户姓名)[：:\s]+([^\s,，\t\r\n]+)",
                    "group": 1,
                },
                "period": {
                    "pattern": r"账单周期[：:\s]+(\d{4}[-/]\d{2}[-/]\d{2})\s*(?:至|-|~)\s*(\d{4}[-/]\d{2}[-/]\d{2})",
                    "start_group": 1,
                    "end_group": 2,
                },
            }
        )
        prof = BankProfile(**d)
        importer = ProfileImporter(prof)

        # Case 1: Masked with hyphens and spaces
        preamble1 = (
            "中国测试银行电子对账单\n"
            "客户姓名: 欧阳六六\n"
            "卡号: 6214-8502-****-9876\n"
            "账单周期: 2026/10/01 至 2026/10/31\n"
        )
        tail, holder, p_start, p_end = importer._extract_preamble_metadata(preamble1)
        assert tail == "9876"
        assert holder == "欧阳六六"
        assert p_start == "2026/10/01"
        assert p_end == "2026/10/31"

        # Case 2: Missing fields entirely
        preamble2 = "未知账单无任何元数据\n"
        tail2, holder2, p_start2, p_end2 = importer._extract_preamble_metadata(preamble2)
        assert tail2 is None
        assert holder2 is None
        assert p_start2 is None
        assert p_end2 is None


# ==============================================================================
# 4. Semantic Direction Overrides
# ==============================================================================

class TestSemanticDirectionOverrides:
    """Stress-test keyword & regex direction overrides (refund & transfer)."""

    def test_refund_keyword_override_only_on_inbound_credits(self):
        """Regression guard (B-07): refund keywords promote inbound credits to 'refund' without corrupting outgoing expenses."""
        d = make_profile_dict("signed", negative_means="expense")
        prof = BankProfile(**d)
        importer = ProfileImporter(prof)

        csv_text = (
            "交易日期,交易时间,交易金额,交易对方,交易摘要\n"
            "2026-11-09,10:00:00,35.50,美团,退款\n"
            "2026-11-09,11:00:00,-20.00,商户,消费含退款保障险\n"
            "2026-11-09,12:00:00,100.00,商户,ONLINE REFUND\n"
            "2026-11-09,13:00:00,50.00,信用卡中心,退货还款冲正\n"
        )
        probe = FileProbe(csv_text.encode("utf-8"), file_name="refund.csv")
        res = importer.parse(probe)
        assert len(res.transactions) == 4
        assert res.transactions[0].direction == "refund"
        assert res.transactions[1].direction == "expense"
        assert res.transactions[2].direction == "refund"
        # Transfer keywords must NOT overwrite an already-classified refund
        assert res.transactions[3].direction == "refund"

    def test_transfer_keyword_override(self):
        """Keywords like '还款', '理财', '提现' override direction to 'transfer'."""
        d = make_profile_dict("signed", negative_means="expense")
        prof = BankProfile(**d)
        importer = ProfileImporter(prof)

        csv_text = (
            "交易日期,交易时间,交易金额,交易对方,交易摘要\n"
            "2026-11-09,10:00:00,-1000.00,信用卡中心,信用卡还款\n"
            "2026-11-09,11:00:00,-5000.00,天天基金,理财买入\n"
            "2026-11-09,12:00:00,200.00,微信支付,微信零钱提现\n"
        )
        probe = FileProbe(csv_text.encode("utf-8"), file_name="transfer.csv")
        res = importer.parse(probe)
        assert len(res.transactions) == 3
        for t in res.transactions:
            assert t.direction == "transfer"
