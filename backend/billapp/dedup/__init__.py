"""Deduplication module containing L1 fingerprint, L2 refund linking, L3 cross-source matching, and L4 transfer pairing."""

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
from billapp.dedup.cross_source import (
    compute_match_score,
    compute_match_score_with_breakdown,
    compute_name_sim,
    match_cross_source,
    normalize_merchant,
)
from billapp.dedup.transfer import (
    compute_transfer_score,
    extract_bank_and_tail,
    pair_internal_transfers,
)
from billapp.dedup.engine import (
    DedupSummary,
    confirm_match_link,
    is_pair_blocked,
    reject_match_link,
    rollback_import_batch,
    run_dedup_pipeline,
)

__all__ = [
    # L1 Fingerprint
    "assign_fingerprints",
    "compute_fallback_fingerprint",
    "compute_order_fingerprint",
    # L2 Refunds
    "extract_alipay_parent_order_id",
    "link_in_memory_refunds",
    "link_in_source_refunds",
    # L3 Cross-Source Dedup
    "compute_match_score",
    "compute_match_score_with_breakdown",
    "compute_name_sim",
    "match_cross_source",
    "normalize_merchant",
    # L4 Internal Transfer
    "compute_transfer_score",
    "extract_bank_and_tail",
    "pair_internal_transfers",
    # Coordinator & Rejection Memory
    "DedupSummary",
    "confirm_match_link",
    "is_pair_blocked",
    "reject_match_link",
    "rollback_import_batch",
    "run_dedup_pipeline",
]
