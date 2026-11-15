"""Importers package for bank and platform statement parsing."""

from billapp.core.models import AccountRef, ParseResult, SelfCheckResult, TransactionCreate
from billapp.importers.alipay import AlipayImporter
from billapp.importers.base import FileProbe, Importer
from billapp.importers.boc_debit import BOCDebitImporter
from billapp.importers.icbc_credit import ICBCCreditImporter
from billapp.importers.pdf import (
    PDFCorruptedError,
    PDFDocumentSession,
    PDFError,
    PDFInvalidPasswordError,
    PDFPasswordRequiredError,
    PasswordRequiredError,
    WatermarkFilter,
    extract_clean_text_spans,
    extract_pdf_tables,
    extract_tables_hybrid,
    extract_tables_strategy_a,
    extract_tables_strategy_b,
    open_pdf_document,
)
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
from billapp.importers.registry import ImporterRegistry, get_registry
from billapp.importers.wechat import WeChatImporter

__all__ = [
    "FileProbe",
    "Importer",
    "AccountRef",
    "ParseResult",
    "SelfCheckResult",
    "TransactionCreate",
    "AlipayImporter",
    "WeChatImporter",
    "BOCDebitImporter",
    "ICBCCreditImporter",
    "BankProfile",
    "ProfileImporter",
    "AmountConfig",
    "ColumnMappingConfig",
    "FieldMapping",
    "TableDetectionConfig",
    "get_builtin_profiles_dir",
    "load_builtin_profiles",
    "open_pdf_document",
    "extract_pdf_tables",
    "extract_tables_hybrid",
    "extract_tables_strategy_a",
    "extract_tables_strategy_b",
    "extract_clean_text_spans",
    "WatermarkFilter",
    "PDFDocumentSession",
    "PDFError",
    "PDFPasswordRequiredError",
    "PDFInvalidPasswordError",
    "PDFCorruptedError",
    "PasswordRequiredError",
    "ImporterRegistry",
    "get_registry",
]
