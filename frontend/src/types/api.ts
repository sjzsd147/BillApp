/**
 * BillApp REST API Client TypeScript Definitions
 * Auto-generated and audited from M5 Backend Codebase
 * Compatible with BillApp Frontend Design Specification
 */

// ============================================================================
// Core Enums & Primitive Unions
// ============================================================================

export type AccountKind = 'bank_debit' | 'bank_credit' | 'wallet' | 'platform';
export type DirectionType = 'expense' | 'income' | 'transfer' | 'refund' | 'neutral';
export type TimePrecision = 'second' | 'minute' | 'day';
export type ChannelType = 'alipay' | 'tenpay' | 'jdpay' | 'meituan' | 'unionpay';
export type MatchKind = 'duplicate' | 'transfer' | 'refund';
export type MatchStatus = 'auto' | 'pending' | 'confirmed' | 'rejected';
export type CategorySource = 'manual' | 'user_rule' | 'builtin_rule' | 'platform' | 'llm' | 'none';

export type RuleField = 'counterparty' | 'merchant_norm' | 'description' | 'platform_category' | 'pay_method';
export type RulePatternType = 'contains' | 'exact' | 'prefix' | 'regex';

// ============================================================================
// Generic API Error Envelope
// ============================================================================

export interface ApiError {
  code: string;
  message: string;
  details?: any;
}

// ============================================================================
// System & Meta Endpoints (/api/health, /api/meta)
// ============================================================================

export interface HealthResponse {
  status: 'ok';
  version: string;
}

export interface MetaAccount {
  id: number;
  kind: AccountKind;
  bank: string;
  tail?: string | null;
  alias?: string | null;
}

export interface SystemMetaResponse {
  version: string;
  categories: CategoryItemOut[];
  accounts: MetaAccount[];
  pending_review_count: number;
  db_path: string;
}

// ============================================================================
// Import & Batches (/api/import/*)
// ============================================================================

export interface SelfCheckItem {
  name: string;
  expected: any;
  actual: any;
  passed: boolean;
  deviation?: number | null;
  description?: string | null;
}

export interface PreviewSampleRow {
  txn_time: string;
  direction: DirectionType;
  amount_cents: number;
  amount_display: string;
  counterparty?: string | null;
  description?: string | null;
  channel?: string | null;
}

export interface ImportPreviewItem {
  token: string;
  file_name: string;
  source: string;
  profile_id?: string | null;
  total_rows: number;
  need_password: boolean;
  need_mapping: boolean;
  accounts: Array<{
    kind: AccountKind;
    bank: string;
    tail?: string | null;
    alias?: string | null;
    holder?: string | null;
    currency: string;
  }>;
  self_checks: SelfCheckItem[];
  rows: PreviewSampleRow[];
  raw_rows?: string[][];
  preamble_rows?: string[][];
  detected_file_type?: string | null;
  warnings: string[];
  expires_at: string;
}

export interface ExtractRawResponse {
  file_name: string;
  file_type: string;
  raw_rows: string[][];
  preamble_rows?: string[][];
  total_extracted: number;
}

export interface CommitRequest {
  token: string;
  account_overrides?: Record<string, any> | null;
}

export interface DedupSummary {
  duplicates_auto: number;
  duplicates_pending: number;
  refunds_linked: number;
  transfers_paired: number;
  transfers_pending: number;
}

export interface ImportCommitResponse {
  batch_id: number;
  imported_count: number;
  duplicate_count: number;
  refund_count: number;
  transfer_count: number;
  rule_hits: number;
  unclassified_count: number;
  summary: DedupSummary;
}

export interface BatchItem {
  id: number;
  file_name: string;
  file_sha1: string;
  file_size: number;
  source: string;
  profile_id?: string | null;
  imported_at: string;
  row_count: number;
  period_start?: string | null;
  period_end?: string | null;
  self_check_json?: string | null;
  can_rollback: boolean;
}

export interface BatchRollbackResponse {
  batch_id: number;
  deleted_transactions: number;
  status: 'rolled_back' | 'not_found';
}

// ============================================================================
// Bank Statement Profiles (/api/profiles/*)
// ============================================================================

export interface ProfileSummary {
  id: string;
  name: string;
  bank: string;
  account_kind: string;
  file_type: string;
  is_builtin: boolean;
  created_at?: string | null;
  updated_at?: string | null;
}

export interface ProfileDetail extends ProfileSummary {
  yaml_content: string;
  parsed_config: Record<string, any>;
}

export interface ProfileCreateRequest {
  id: string;
  name: string;
  yaml_content: string;
}

export interface ProfileUpdateRequest {
  name?: string | null;
  yaml_content: string;
}

export interface ProfileTestResponse {
  success: boolean;
  parsed_count: number;
  sample_transactions: any[];
  self_checks: SelfCheckItem[];
  warnings: string[];
  errors: string[];
}

export interface SuggestMappingRequest {
  headers: string[];
  sample_rows?: string[][];
  preamble_rows?: string[][];
  file_name?: string;
  file_type?: string;
}

export interface SuggestMappingResponse {
  amount_mode: 'signed' | 'split' | 'flag';
  column_mapping: Record<string, string>;
  headers: string[];
  source?: 'llm' | 'heuristic';
  llm_status?: 'ok' | 'offline' | 'disabled' | 'error';
  llm_error?: string | null;
  confidence?: number;
  reason?: string | null;
  bank?: string | null;
  account_kind?: string | null;
  card_tail?: string | null;
  currency?: string | null;
  flag_expense_val?: string | null;
  flag_income_val?: string | null;
}

// ============================================================================
// Transactions (/api/transactions/*)
// ============================================================================

export interface MergedSourceItem {
  id: number;
  source: string;
  account_id: number;
  account_bank?: string | null;
  account_tail?: string | null;
  account_alias?: string | null;
  txn_time: string;
  amount_cents: number;
  currency: string;
  direction: DirectionType;
  counterparty?: string | null;
  description?: string | null;
  pay_method?: string | null;
  channel?: string | null;
}

export interface TransactionItem {
  id: number;
  batch_id: number;
  source: string;
  account_id: number;
  fund_account_id?: number | null;
  fund_account_bank?: string | null;
  fund_account_tail?: string | null;
  txn_time: string;
  time_precision: TimePrecision;
  post_date?: string | null;
  amount_cents: number;
  amount: number;                // Frontend alias for amount_cents
  amount_display: string;        // Formatted decimal e.g. "12.34"
  net_cents: number;             // Net amount after refund offset
  currency: string;
  direction: DirectionType;
  counterparty?: string | null;
  description?: string | null;
  pay_method?: string | null;
  channel?: string | null;
  merchant_norm?: string | null;
  order_id?: string | null;
  status?: string | null;
  platform_category?: string | null;
  category_id?: number | null;
  category_key?: string | null;
  category_name?: string | null;
  category_path?: string[] | null;
  category_source?: CategorySource | null;
  category_confidence?: number | null;
  refund_of?: number | null;
  refunded_cents: number;
  duplicate_of?: number | null;
  is_duplicate: boolean;
  merged_count?: number;
  merged_duplicates?: MergedSourceItem[];
  primary_source_txn?: MergedSourceItem | null;
  transfer_peer?: number | null;
  note?: string | null;
  excluded: number;              // 0 or 1
  account_bank?: string | null;
  account_tail?: string | null;
  account_alias?: string | null;
  // FX fields (Milestone 6)
  amount_cny?: number | null;
  fx_rate?: number | null;
  fx_rate_at?: string | null;
  fx_rate_source?: 'online' | 'manual' | 'none' | null;
}

export interface TransactionListResponse {
  items: TransactionItem[];
  total: number;
  page: number;
  page_size: number;
}

export interface TransactionListQueryParams {
  page?: number;
  page_size?: number;
  from?: string;
  to?: string;
  start_date?: string;
  end_date?: string;
  account_id?: number;
  fund_account_id?: number;
  source?: string;
  category_id?: number;
  direction?: DirectionType;
  min_cents?: number;
  max_cents?: number;
  is_duplicate?: boolean;
  status?: string;
  q?: string;
  search?: string;
  excluded?: number;
  unclassified?: boolean;
  currency?: string;
  fx?: 'missing' | string;
  order_by?: 'txn_time' | 'amount_cents' | 'amount_cny' | 'id';
  order_dir?: 'asc' | 'desc';
}

export interface RelatedTxnInfo {
  relation: 'refund_parent' | 'refund_child' | 'duplicate_primary' | 'duplicate_child' | 'transfer_peer';
  txn: Record<string, any>;
}

export interface TransactionDetailResponse {
  transaction: TransactionItem;
  related: RelatedTxnInfo[];
}

export interface RuleCandidate {
  name: string;
  category_id: number;
  field: RuleField;
  pattern_type: RulePatternType;
  pattern_value: string;
  priority: number;
}

export interface RuleProposal {
  candidate_rule: RuleCandidate;
  matched_total: number;
  applicable_count: number;
  sample_applicable_txn_ids: number[];
  prompt_message: string;
}

export interface TransactionPatchRequest {
  category_id?: number | null;
  note?: string | null;
  excluded?: number | null;
}

export interface TransactionPatchResponse {
  transaction: TransactionItem;
  rule_proposal?: RuleProposal | null;
}

export interface BulkPatchRequest {
  ids: number[];
  category_id?: number | null;
  excluded?: number | null;
  note?: string | null;
}

export interface BulkPatchResponse {
  updated_count: number;
}

// ============================================================================
// Review (待确认) Endpoints (/api/review/*)
// ============================================================================

export interface BreakdownScore {
  amount: number;
  tail: number;
  time: number;
  channel: number;
  name: number;
  penalty: number;
}

export interface ReviewTxnSide {
  id: number;
  source: string;
  source_label: string;
  txn_time: string;
  amount_cents: number;
  amount_display: string;
  counterparty?: string | null;
  description?: string | null;
  channel?: string | null;
  account_name?: string | null;
}

export interface ReviewItemOut {
  id: number;
  kind: 'duplicate' | 'transfer';
  score: number;
  status: string;
  breakdown: BreakdownScore;
  left: ReviewTxnSide;
  right: ReviewTxnSide;
  llm?: {
    verdict: string;
    reason: string;
    confidence: number;
  } | null;
  created_at: string;
}

export interface ReviewActionResponse {
  success: boolean;
  link_id: number;
  status: 'confirmed' | 'rejected';
}

export interface BulkConfirmRequest {
  link_ids: number[];
}

export interface BulkConfirmResponse {
  success: boolean;
  confirmed_count: number;
}

// ============================================================================
// Category Taxonomy (/api/categories/*)
// ============================================================================

export interface CategoryItemOut {
  id: number;
  name: string;
  direction: 'expense' | 'income';
  type: 'expense' | 'income';    // Frontend compatibility alias
  parent_id?: number | null;
  key?: string | null;
  icon?: string | null;
  color?: string | null;
  sort_order: number;
  is_builtin: number | boolean;
  rule_count: number;
  transaction_count: number;
  budget_cents?: number | null;
  path: string[];
  path_names: string[];
  children: CategoryItemOut[];
}

export interface CategoryCreateRequest {
  name: string;
  direction?: 'expense' | 'income';
  parent_id?: number | null;
  key?: string | null;
  icon?: string | null;
  color?: string | null;
  sort_order?: number;
}

export interface CategoryUpdateRequest {
  name?: string | null;
  icon?: string | null;
  color?: string | null;
  sort_order?: number | null;
  budget_cents?: number | null;
}

export interface CategoryDeleteResponse {
  ok: boolean;
  deleted_id: number;
  reassigned_to_id?: number | null;
}

// ============================================================================
// Rules Engine (/api/rules/*)
// ============================================================================

export interface RuleOut {
  id: number;
  name: string;
  category_id: number;
  category_name?: string | null;
  category_key?: string | null;
  priority: number;
  field: RuleField;
  pattern_type: RulePatternType;
  pattern_value: string;
  amount_min_cents?: number | null;
  amount_max_cents?: number | null;
  is_builtin: boolean;
  hit_count: number;
  created_at?: string | null;
}

export interface RuleCreateRequest {
  name?: string | null;
  category_id: number;
  priority?: number;
  field?: RuleField;
  pattern_type?: RulePatternType;
  pattern_value: string;
  amount_min_cents?: number | null;
  amount_max_cents?: number | null;
}

export interface RuleUpdateRequest {
  name?: string | null;
  category_id?: number | null;
  priority?: number | null;
  field?: RuleField | null;
  pattern_type?: RulePatternType | null;
  pattern_value?: string | null;
  amount_min_cents?: number | null;
  amount_max_cents?: number | null;
}

export interface MatchedTxnPreview {
  id: number;
  txn_time: string;
  counterparty?: string | null;
  merchant_norm?: string | null;
  description?: string | null;
  amount_cents: number;
  current_category_id?: number | null;
  current_category_name?: string | null;
  category_source?: string | null;
}

export interface RuleTestRequest {
  field: RuleField;
  pattern_type: RulePatternType;
  pattern_value: string;
  amount_min_cents?: number | null;
  amount_max_cents?: number | null;
  category_id?: number | null;
  limit?: number;
  exclude_manual?: boolean;
}

export interface RuleTestResponse {
  matched_count: number;
  manual_skipped_count: number;
  preview: MatchedTxnPreview[];
}

export interface RuleApplyProposalRequest {
  name?: string | null;
  category_id: number;
  field?: RuleField;
  pattern_type?: RulePatternType;
  pattern_value: string;
  priority?: number;
  apply_to_existing?: boolean;
  overwrite_manual?: boolean;
}

export interface RuleApplyProposalResponse {
  rule: RuleOut;
  matched_count: number;
  updated_count: number;
  skipped_manual_count: number;
}

export interface RuleSuggestionOut {
  brand: string;
  category_id: number;
  category_name: string;
  count: number;
  sample_counterparty: string;
}

// ============================================================================
// LLM Service (/api/llm/*)
// ============================================================================

export interface LLMClassifyRequest {
  batch_id?: number | null;
  transaction_ids?: number[] | null;
  limit?: number;
  holder_name?: string | null;
  update_db?: boolean;
}

export interface LLMClassifyItemOut {
  transaction_id: number;
  category_id?: number | null;
  category_key?: string | null;
  confidence: number;
  reason: string;
  updated: boolean;
}

export interface LLMClassifyResponse {
  success: boolean;
  status: 'ok' | 'offline' | 'disabled' | 'error';
  classified_count: number;
  results: LLMClassifyItemOut[];
  reason?: string | null;
}

export interface LLMReviewRequest {
  batch_id?: number | null;
  transaction_ids?: number[] | null;
  max_confidence?: number;
  limit?: number;
  holder_name?: string | null;
}

export interface LLMReviewItemOut {
  id: number;
  agree: boolean;
  suggested_category_id?: number | null;
  suggested_category_key?: string | null;
  confidence: number;
  reason: string;
}

export interface LLMReviewResponse {
  success: boolean;
  status: 'ok' | 'offline' | 'error';
  reviews: LLMReviewItemOut[];
  reason?: string | null;
}

export interface LLMTestRequest {
  base_url?: string | null;
  api_key?: string | null;
  model?: string | null;
  timeout_sec?: number;
}

export interface LLMTestResponse {
  ok: boolean;
  latency_ms: number;
  model: string;
  error?: string | null;
}

export interface LLMModelsRequest {
  base_url?: string | null;
  api_key?: string | null;
  timeout_sec?: number;
}

export interface LLMModelInfo {
  id: string;
  name?: string | null;
  owned_by?: string | null;
  description?: string | null;
}

export interface LLMModelsResponse {
  ok: boolean;
  models: LLMModelInfo[];
  error?: string | null;
}

export interface LLMStatusResponse {
  configured: boolean;
  enabled: boolean;
  base_url: string;
  model: string;
  cache_count: number;
}

// ============================================================================
// Statistics Engine (/api/stats/*)
// ============================================================================

export interface StatsSummaryPrev {
  expense_net: number;
  income: number;
  from: string;
  to: string;
}

export interface FxPendingAmount {
  currency: string;
  amount: number;
}

export interface StatsSummary {
  // Primary cents metrics
  total_expense_cents: number;
  total_income_cents: number;
  net_savings_cents: number;
  savings_rate: number;
  daily_average_cents: number;
  transaction_count: number;

  // Frontend design aliases (§8.2)
  expense_net: number;
  income: number;
  balance: number;
  daily_avg: number;
  from: string;
  to: string;
  days: number;
  expense_count: number;
  income_count: number;
  fx_included_count: number;
  fx_included_cny: number;
  fx_pending_count: number;
  fx_pending_amounts: FxPendingAmount[];
  prev?: StatsSummaryPrev | null;
}

export interface TrendItem {
  period: string; // 'YYYY-MM' or 'YYYY-MM-DD'
  expense_cents: number;
  income_cents: number;
  net_cents: number;
  transaction_count: number;
}

export interface CategoryStatNode {
  id?: number | null;
  key?: string | null;
  name: string;
  amount_cents: number;
  percentage: number;
  color?: string | null;
  icon?: string | null;
  children: CategoryStatNode[];
}

export interface CategoryStatsResponse {
  total_cents: number;
  direction: 'expense' | 'income';
  items: CategoryStatNode[];
}

export interface MerchantRankItem {
  merchant: string;
  expense_cents: number;
  transaction_count: number;
  percentage: number;
}

export interface MerchantStatsResponse {
  total_expense_cents: number;
  items: MerchantRankItem[];
  other_expense_cents: number;
  other_count: number;
}

export interface HeatmapItem {
  date: string; // 'YYYY-MM-DD'
  expense_cents: number;
  count: number;
}

export interface WeekdayHourHeatmapItem {
  weekday: number; // 1 (Mon) .. 7 (Sun)
  hour: number;    // 0 .. 23
  expense_cents: number;
  count: number;
}

export interface HeatmapResponse {
  mode: 'calendar' | 'weekday_hour';
  year?: number | null;
  calendar_items: HeatmapItem[];
  weekday_hour_items: WeekdayHourHeatmapItem[];
}

export interface AccountStatItem {
  account_id?: number | null;
  name: string;
  bank?: string | null;
  tail?: string | null;
  type: string;
  balance_cents?: number | null;
  total_in_cents: number;
  total_out_cents: number;
  net_cents: number;
  transaction_count: number;
  percentage: number;
}

export interface AccountStatsResponse {
  total_out_cents: number;
  total_in_cents: number;
  items: AccountStatItem[];
}

// ============================================================================
// Settings (/api/settings/*)
// ============================================================================

export interface GeneralSettings {
  table_density: 'standard' | 'compact';
  amount_privacy: boolean;
  remember_window: boolean;
  learning_suggestions_enabled: boolean;
}

export interface DedupSettings {
  auto_confirm_threshold: number;
  pending_threshold: number;
  second_window_minutes: number;
  day_window_debit_days: number;
  day_window_credit_days: number;
  llm_arbitrate_enabled: boolean;
  llm_auto_confirm: boolean;
  llm_auto_confirm_threshold: number;
}

export interface RefundSettings {
  mode: 'original_month' | 'refund_month';
}

export interface LLMSettingsOut {
  base_url: string;
  model: string;
  temperature: number;
  timeout_sec: number;
  enabled: boolean;
  task_classify_enabled: boolean;
  task_review_enabled: boolean;
  task_arbitrate_enabled: boolean;
  task_merchant_norm_enabled: boolean;
  task_suggest_profile_enabled: boolean;
  task_full_parse_enabled?: boolean;
  auto_confirm_dedup: boolean;
  auto_confirm_threshold: number;
  api_key_masked: string;
  has_api_key: boolean;
}

export interface LLMSettingsUpdate {
  base_url?: string | null;
  api_key?: string | null;
  model?: string | null;
  temperature?: number | null;
  timeout_sec?: number | null;
  enabled?: boolean | null;
  task_classify_enabled?: boolean | null;
  task_review_enabled?: boolean | null;
  task_arbitrate_enabled?: boolean | null;
  task_merchant_norm_enabled?: boolean | null;
  task_suggest_profile_enabled?: boolean | null;
  task_full_parse_enabled?: boolean | null;
  auto_confirm_dedup?: boolean | null;
  auto_confirm_threshold?: number | null;
}

export interface SettingsOut {
  general: GeneralSettings;
  dedup: DedupSettings;
  refund: RefundSettings;
  llm: LLMSettingsOut;
  db_path: string;
  db_size_bytes: number;
}

export interface SettingsUpdateRequest {
  general?: Partial<GeneralSettings> | null;
  dedup?: Partial<DedupSettings> | null;
  refund?: Partial<RefundSettings> | null;
  llm?: LLMSettingsUpdate | null;
}

// ============================================================================
// Accounts Management (/api/accounts/*)
// ============================================================================

export interface AccountOut {
  id: number;
  kind: AccountKind;
  bank: string;
  tail?: string | null;
  alias?: string | null;
  display_name: string;
  holder?: string | null;
  is_self: boolean;
  currency: string;
  created_at?: string | null;
  transaction_count: number;
  funded_count: number;
  computed_balance_cents: number;
  first_txn_time?: string | null;
  last_txn_time?: string | null;
}

export interface AccountUpdateRequest {
  alias?: string | null;
  holder?: string | null;
}

export interface AccountMergeRequest {
  source_id: number;
  target_id: number;
}

export interface AccountMergeResponse {
  ok: boolean;
  migrated_count: number;
  merged_into_id: number;
}

// ============================================================================
// Foreign Exchange (FX) (/api/fx/*)
// ============================================================================

export type FxSource = 'online' | 'manual' | 'none';

export interface FxRateItem {
  id: number;
  currency: string;
  rate: number;
  effective_date: string;
  source: 'online' | 'manual';
  created_at: string;
  updated_at: string;
  note?: string | null;
}

export interface FxPendingByCurrency {
  currency: string;
  count: number;
  amount_cents: number;
}

export interface FxRatesResponse {
  rates: FxRateItem[];
  currencies: string[];
  pending_count: number;
  pending_by_currency: FxPendingByCurrency[];
}

export interface FxRefreshResponse {
  total_missing: number;
  refreshed_count: number;
}

export interface FxManualRateRequest {
  currency: string;
  rate: number;
  effective_date: string;
  note?: string | null;
  apply_to_all_missing?: boolean;
}

export interface FxManualRateResponse {
  success: boolean;
  rate?: FxRateItem | null;
  updated_transactions: number;
}

// ============================================================================
// Data Backup, Restore & Reset (/api/backup, /api/restore, /api/data/reset)
// ============================================================================

export interface RestoreResponse {
  success: boolean;
  transactions_count: number;
  accounts_count: number;
}

export interface ResetDataRequest {
  confirm_text: string;
  keep_rules?: boolean;
  keep_settings?: boolean;
}

export interface ResetDataResponse {
  success: boolean;
  message: string;
}
