-- SQLite Core Schema for Bill Management System
-- WAL mode and foreign key integrity

PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;

-- 1. 统一账户表 (accounts)
CREATE TABLE IF NOT EXISTS accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL CHECK(kind IN ('bank_debit', 'bank_credit', 'wallet', 'platform')),
    bank TEXT NOT NULL,                     -- e.g. "中国银行", "工商银行", "支付宝", "微信支付"
    tail TEXT,                              -- 尾号后4位 (e.g. "8801", "6666", "1234")，平台或现金账户可为空
    alias TEXT,                             -- 用户自定义别名 (e.g. "日常消费卡", "Visa美元卡")
    holder TEXT,                            -- 户名 (e.g. "张三")
    currency TEXT NOT NULL DEFAULT 'CNY',   -- 主币种
    created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    UNIQUE(kind, bank, tail)
);
CREATE INDEX IF NOT EXISTS idx_accounts_bank_tail ON accounts(bank, tail);
CREATE UNIQUE INDEX IF NOT EXISTS idx_accounts_unique_notail ON accounts(kind, bank) WHERE tail IS NULL;

-- 2. 导入批次表 (import_batches)
CREATE TABLE IF NOT EXISTS import_batches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_name TEXT NOT NULL,
    file_sha1 TEXT NOT NULL UNIQUE,         -- 文件 SHA1，防止同一文件重复导入
    file_size INT NOT NULL,
    source TEXT NOT NULL,                   -- 'alipay', 'wechat', 'boc_debit', 'icbc_credit', 'profile:<id>'
    profile_id TEXT,                        -- 采用的 Profile ID
    imported_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    row_count INT NOT NULL DEFAULT 0,
    period_start TEXT,
    period_end TEXT,
    self_check_json TEXT                    -- 自检项序列化 JSON：[{"name": "...", "expected": ..., "actual": ..., "passed": true}]
);
CREATE INDEX IF NOT EXISTS idx_import_batches_sha1 ON import_batches(file_sha1);

-- 3. 交易明细主表 (transactions)
CREATE TABLE IF NOT EXISTS transactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id INT NOT NULL REFERENCES import_batches(id) ON DELETE CASCADE,
    source TEXT NOT NULL,                   -- 'alipay', 'wechat', 'boc_debit', 'icbc_credit', 'profile:<id>'
    account_id INT NOT NULL REFERENCES accounts(id),        -- 账单主体账户
    fund_account_id INT REFERENCES accounts(id),           -- 平台出资卡账户 (如支付宝使用工行8801支付)
    
    -- 时间与记账
    txn_time TEXT NOT NULL,                 -- 交易时间 'YYYY-MM-DD HH:MM:SS'
    time_precision TEXT NOT NULL CHECK(time_precision IN ('second', 'minute', 'day')),
    post_date TEXT,                         -- 银行记账日 'YYYY-MM-DD'
    
    -- 金额与币种 (以分为单位整型存储，彻底杜绝浮点精度丢失)
    amount_cents INT NOT NULL CHECK(amount_cents >= 0),
    currency TEXT NOT NULL DEFAULT 'CNY',
    direction TEXT NOT NULL CHECK(direction IN ('expense', 'income', 'transfer', 'refund', 'neutral')),
    
    -- 商户与摘要
    counterparty TEXT,                      -- 交易对方 / 户名
    description TEXT,                       -- 商品说明 / 摘要
    pay_method TEXT,                        -- 原始收付款方式
    channel TEXT CHECK(channel IS NULL OR channel IN ('alipay', 'tenpay', 'jdpay', 'meituan', 'unionpay')),
    merchant_norm TEXT,                     -- 归一化商户名 (清洗掉渠道前缀与公司后缀)
    
    -- 平台流水号
    order_id TEXT,                          -- 交易订单号
    merchant_order_id TEXT,                 -- 商户订单号
    status TEXT,                            -- 原始交易状态 (如 "交易成功", "交易关闭", "退款成功")
    platform_category TEXT,                 -- 平台自带分类 (如 "餐饮美食")
    raw_json TEXT,                          -- 原始行数据 JSON 快照
    
    -- 判重指纹
    fingerprint TEXT NOT NULL UNIQUE,       -- L1 指纹哈希
    
    -- 分类管线
    category_id INT REFERENCES categories(id) ON DELETE SET NULL,
    category_source TEXT CHECK(category_source IS NULL OR category_source IN ('manual', 'user_rule', 'builtin_rule', 'platform', 'llm', 'none')),
    category_confidence REAL DEFAULT 1.0,
    
    -- 关联关系统计
    refund_of INT REFERENCES transactions(id) ON DELETE SET NULL,     -- 关联的原交易 ID (退款冲抵)
    refunded_cents INT NOT NULL DEFAULT 0 CHECK(refunded_cents >= 0), -- 该笔支出已退款金额 (分)
    duplicate_of INT REFERENCES transactions(id) ON DELETE SET NULL,  -- L3 跨来源匹配关联的主记录 (平台为主，银行为从)
    transfer_peer INT REFERENCES transactions(id) ON DELETE SET NULL, -- L4 内部转账对端记录 ID
    
    note TEXT,
    excluded INT NOT NULL DEFAULT 0 CHECK(excluded IN (0, 1)),        -- 是否排除在统计之外

    -- 外币与汇率 (D12 / C-5 / §8.6)
    amount_cny INT,                                                   -- 折算人民币 (分)，CNY交易等于 amount_cents；外币缺失为 NULL
    fx_rate REAL,                                                     -- 交易时汇率 (1 外币 = ? CNY)
    fx_rate_at TEXT,                                                  -- 汇率获取/生效时间
    fx_rate_source TEXT CHECK(fx_rate_source IS NULL OR fx_rate_source IN ('online', 'manual', 'none'))
);
CREATE INDEX IF NOT EXISTS idx_transactions_batch_id ON transactions(batch_id);
CREATE INDEX IF NOT EXISTS idx_transactions_txn_time ON transactions(txn_time);
CREATE INDEX IF NOT EXISTS idx_transactions_account ON transactions(account_id);
CREATE INDEX IF NOT EXISTS idx_transactions_fund_account ON transactions(fund_account_id);
CREATE INDEX IF NOT EXISTS idx_transactions_direction ON transactions(direction);
CREATE INDEX IF NOT EXISTS idx_transactions_duplicate_of ON transactions(duplicate_of);
CREATE INDEX IF NOT EXISTS idx_transactions_transfer_peer ON transactions(transfer_peer);
CREATE INDEX IF NOT EXISTS idx_transactions_refund_of ON transactions(refund_of);
CREATE INDEX IF NOT EXISTS idx_transactions_order_id ON transactions(order_id);
CREATE INDEX IF NOT EXISTS idx_transactions_fingerprint ON transactions(fingerprint);

-- 4. 匹配关系表 (match_links)
CREATE TABLE IF NOT EXISTS match_links (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL CHECK(kind IN ('duplicate', 'transfer', 'refund')),
    primary_id INT NOT NULL REFERENCES transactions(id) ON DELETE CASCADE,
    secondary_id INT NOT NULL REFERENCES transactions(id) ON DELETE CASCADE,
    score REAL NOT NULL,
    features_json TEXT,                     -- 匹配特征详情 JSON (时间差、金额、渠道、商户相似度)
    llm_verdict TEXT,                       -- 'same', 'different', 'uncertain'
    llm_reason TEXT,
    status TEXT NOT NULL CHECK(status IN ('auto', 'pending', 'confirmed', 'rejected')),
    created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);
CREATE INDEX IF NOT EXISTS idx_match_links_status ON match_links(status);
CREATE INDEX IF NOT EXISTS idx_match_links_primary ON match_links(primary_id);
CREATE INDEX IF NOT EXISTS idx_match_links_secondary ON match_links(secondary_id);

-- 5. 配对拒绝黑名单表 (match_blocklist)
CREATE TABLE IF NOT EXISTS match_blocklist (
    a_id INT NOT NULL REFERENCES transactions(id) ON DELETE CASCADE,
    b_id INT NOT NULL REFERENCES transactions(id) ON DELETE CASCADE,
    kind TEXT NOT NULL CHECK(kind IN ('duplicate', 'transfer', 'refund')),
    created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    PRIMARY KEY(a_id, b_id, kind)
);

-- 6. 分类表 (categories)
CREATE TABLE IF NOT EXISTS categories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    key TEXT UNIQUE,                                  -- 稳定英文 Key (e.g. 'food', 'food.dining')
    name TEXT NOT NULL,
    parent_id INT REFERENCES categories(id) ON DELETE CASCADE,
    direction TEXT NOT NULL CHECK(direction IN ('expense', 'income')),
    icon TEXT,
    color TEXT,                                       -- 颜色十六进制 (用于看板/图表)
    sort_order INT NOT NULL DEFAULT 0,
    is_builtin INT NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_categories_parent ON categories(parent_id);
CREATE INDEX IF NOT EXISTS idx_categories_key ON categories(key);

-- 7. 自动分类规则表 (rules)
CREATE TABLE IF NOT EXISTS rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    category_id INT NOT NULL REFERENCES categories(id) ON DELETE CASCADE,
    priority INT NOT NULL DEFAULT 100,      -- 数值越小优先级越高
    field TEXT NOT NULL CHECK(field IN ('counterparty', 'merchant_norm', 'description', 'platform_category', 'pay_method')),
    pattern_type TEXT NOT NULL CHECK(pattern_type IN ('exact', 'contains', 'regex', 'prefix')),
    pattern_value TEXT NOT NULL,
    amount_min_cents INT,
    amount_max_cents INT,
    is_builtin INT NOT NULL DEFAULT 0,
    hit_count INT NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);
CREATE INDEX IF NOT EXISTS idx_rules_priority ON rules(priority);
CREATE INDEX IF NOT EXISTS idx_rules_category ON rules(category_id);

-- 8. LLM 缓存表 (llm_cache)
CREATE TABLE IF NOT EXISTS llm_cache (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_type TEXT NOT NULL CHECK(task_type IN ('classify', 'review', 'arbitrate', 'merchant_norm', 'suggest_profile')),
    input_hash TEXT NOT NULL UNIQUE,        -- SHA256 of input prompt
    response_json TEXT NOT NULL,
    model TEXT NOT NULL,
    prompt_tokens INT DEFAULT 0,
    completion_tokens INT DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);
CREATE INDEX IF NOT EXISTS idx_llm_cache_hash ON llm_cache(input_hash);

-- 9. 系统配置表 (settings)
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,                    -- JSON 序列化存储
    updated_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

-- 10. 自定义银行配置表 (bank_profiles)
CREATE TABLE IF NOT EXISTS bank_profiles (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    yaml_content TEXT NOT NULL,
    is_builtin INT NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

-- 11. 汇率缓存表 (fx_rates)
CREATE TABLE IF NOT EXISTS fx_rates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    currency TEXT NOT NULL,
    rate REAL NOT NULL,
    effective_date TEXT NOT NULL,
    source TEXT NOT NULL CHECK(source IN ('online', 'manual')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    note TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_fx_rates_curr_date ON fx_rates(currency, effective_date);
