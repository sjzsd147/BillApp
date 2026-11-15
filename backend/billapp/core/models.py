"""Pydantic schemas and Data Transfer Objects for core billing domain."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Dict, List, Literal, Optional, Tuple, Union
from pydantic import BaseModel, ConfigDict, Field

# Core Domain Enums & Literals
AccountKind = Literal["bank_debit", "bank_credit", "wallet", "platform"]
DirectionType = Literal["expense", "income", "transfer", "refund", "neutral"]
TimePrecision = Literal["second", "minute", "day"]
ChannelType = Literal["alipay", "tenpay", "jdpay", "meituan", "unionpay"]
MatchKind = Literal["duplicate", "transfer", "refund"]
MatchStatus = Literal["auto", "pending", "confirmed", "rejected"]
CategorySource = Literal["manual", "user_rule", "builtin_rule", "platform", "llm", "none"]


# --- Account Models ---
class AccountBase(BaseModel):
    kind: AccountKind
    bank: str
    tail: Optional[str] = None
    alias: Optional[str] = None
    holder: Optional[str] = None
    currency: str = "CNY"


class AccountCreate(AccountBase):
    pass


class Account(AccountBase):
    model_config = ConfigDict(from_attributes=True)
    id: int
    created_at: Optional[str] = None


class AccountRef(BaseModel):
    id: Optional[int] = None
    kind: AccountKind
    bank: Optional[str] = None
    tail: Optional[str] = None
    alias: Optional[str] = None
    holder: Optional[str] = None


# --- Self-Check Models ---
class SelfCheckResult(BaseModel):
    name: str
    expected: Any
    actual: Any
    passed: bool
    deviation: Optional[float] = None
    description: Optional[str] = None


# --- Import Batch Models ---
class ImportBatchBase(BaseModel):
    file_name: str
    file_sha1: str
    file_size: int
    source: str
    profile_id: Optional[str] = None
    row_count: int = 0
    period_start: Optional[str] = None
    period_end: Optional[str] = None
    self_check_json: Optional[str] = None


class ImportBatchCreate(ImportBatchBase):
    pass


class ImportBatch(ImportBatchBase):
    model_config = ConfigDict(from_attributes=True)
    id: int
    imported_at: str


# --- Transaction Models ---
class TransactionBase(BaseModel):
    source: str
    txn_time: str
    time_precision: TimePrecision = "second"
    post_date: Optional[str] = None
    amount_cents: int = Field(ge=0)
    currency: str = "CNY"
    direction: DirectionType
    counterparty: Optional[str] = None
    description: Optional[str] = None
    pay_method: Optional[str] = None
    channel: Optional[ChannelType] = None
    merchant_norm: Optional[str] = None
    order_id: Optional[str] = None
    merchant_order_id: Optional[str] = None
    status: Optional[str] = None
    platform_category: Optional[str] = None
    raw_json: Optional[str] = None
    fingerprint: Optional[str] = None
    category_id: Optional[int] = None
    category_source: Optional[CategorySource] = None
    category_confidence: Optional[float] = 1.0
    refund_of: Optional[int] = None
    refunded_cents: int = Field(default=0, ge=0)
    duplicate_of: Optional[int] = None
    transfer_peer: Optional[int] = None
    note: Optional[str] = None
    excluded: int = 0
    # FX fields (Milestone 6)
    amount_cny: Optional[int] = None
    fx_rate: Optional[float] = None
    fx_rate_at: Optional[str] = None
    fx_rate_source: Optional[Literal["online", "manual", "none"]] = None


class TransactionCreate(TransactionBase):
    batch_id: Optional[int] = None
    account_id: Optional[int] = None
    fund_account_id: Optional[int] = None
    # In-source account reference helpers before DB insertion:
    account_ref: Optional[AccountRef] = None
    fund_account_ref: Optional[AccountRef] = None


class Transaction(TransactionBase):
    model_config = ConfigDict(from_attributes=True)
    id: int
    batch_id: int
    account_id: int
    fund_account_id: Optional[int] = None
    fingerprint: str


class TransactionOut(Transaction):
    amount_display: str = "0.00"
    net_cents: int = 0
    category_name: Optional[str] = None


# --- Match Link Models ---
class MatchLinkBase(BaseModel):
    kind: MatchKind
    primary_id: int
    secondary_id: int
    score: float
    features_json: Optional[str] = None
    llm_verdict: Optional[str] = None
    llm_reason: Optional[str] = None
    status: MatchStatus = "pending"


class MatchLinkCreate(MatchLinkBase):
    pass


class MatchLink(MatchLinkBase):
    model_config = ConfigDict(from_attributes=True)
    id: int
    created_at: str
    updated_at: str


# --- Parse Result DTO ---
class ParseResult(BaseModel):
    transactions: List[TransactionCreate] = Field(default_factory=list)
    accounts: List[AccountCreate] = Field(default_factory=list)
    holder_name: Optional[str] = None
    period: Optional[Tuple[Optional[str], Optional[str]]] = None
    self_checks: List[SelfCheckResult] = Field(default_factory=list)
    warnings: List[str] = Field(default_factory=list)
