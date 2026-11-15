"""Synthetic fixture generator package for BillApp E2E testing."""

from .synthetic_alipay import generate_synthetic_alipay_csv
from .synthetic_wechat import generate_synthetic_wechat_xlsx, generate_synthetic_wechat_csv
from .synthetic_pdf import generate_synthetic_boc_pdf, generate_synthetic_icbc_pdf
from .synthetic_profile import generate_synthetic_profile_csv, generate_synthetic_profile_xlsx

__all__ = [
    "generate_synthetic_alipay_csv",
    "generate_synthetic_wechat_xlsx",
    "generate_synthetic_wechat_csv",
    "generate_synthetic_boc_pdf",
    "generate_synthetic_icbc_pdf",
    "generate_synthetic_profile_csv",
    "generate_synthetic_profile_xlsx",
]
