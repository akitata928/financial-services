-- Taiwan ETF Master Database Schema
-- Source: emega.com.tw ETFMaster API
-- All dates stored as ISO format strings YYYY-MM-DD

CREATE TABLE IF NOT EXISTS etf_profile (
    etf_code TEXT PRIMARY KEY,
    etf_name TEXT,
    issuer TEXT,
    category TEXT,
    etf_type TEXT,
    region TEXT,
    leverage_type TEXT,
    dividend_type TEXT,
    tracking_index TEXT,
    currency TEXT DEFAULT 'TWD',
    inception_date TEXT,
    expense_ratio REAL,
    aum_billion REAL,
    beneficiary_count INTEGER,
    raw_json TEXT,
    fetched_at TEXT
);

CREATE TABLE IF NOT EXISTS etf_market_snapshot (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    etf_code TEXT NOT NULL,
    snapshot_date TEXT NOT NULL,
    close_price REAL,
    nav REAL,
    premium_discount_pct REAL,
    volume_k INTEGER,
    ytd_return REAL,
    one_year_return REAL,
    UNIQUE(etf_code, snapshot_date)
);

CREATE TABLE IF NOT EXISTS stock_master (
    stock_code TEXT PRIMARY KEY,
    stock_name TEXT,
    industry TEXT,
    market TEXT,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS stock_etf_holdings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    stock_code TEXT NOT NULL,
    etf_code TEXT NOT NULL,
    etf_name TEXT,
    holding_weight_pct REAL,
    holding_shares INTEGER,
    snapshot_date TEXT NOT NULL,
    UNIQUE(stock_code, etf_code, snapshot_date)
);

CREATE TABLE IF NOT EXISTS etf_holdings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    etf_code TEXT NOT NULL,
    stock_code TEXT NOT NULL,
    stock_name TEXT,
    weight_pct REAL,
    shares INTEGER,
    market_value REAL,
    snapshot_date TEXT NOT NULL,
    UNIQUE(etf_code, stock_code, snapshot_date)
);

CREATE TABLE IF NOT EXISTS etf_stock_weight_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    etf_code TEXT NOT NULL,
    stock_code TEXT NOT NULL,
    record_date TEXT NOT NULL,
    weight_pct REAL,
    UNIQUE(etf_code, stock_code, record_date)
);

CREATE TABLE IF NOT EXISTS etf_nav_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    etf_code TEXT NOT NULL,
    record_date TEXT NOT NULL,
    nav REAL,
    UNIQUE(etf_code, record_date)
);

CREATE TABLE IF NOT EXISTS knowledge_chunks (
    chunk_id TEXT PRIMARY KEY,
    source_type TEXT,
    source_name TEXT,
    title TEXT,
    content TEXT,
    metadata_json TEXT,
    created_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_stock_etf_stock ON stock_etf_holdings(stock_code);
CREATE INDEX IF NOT EXISTS idx_stock_etf_etf ON stock_etf_holdings(etf_code);
CREATE INDEX IF NOT EXISTS idx_etf_holdings_etf ON etf_holdings(etf_code);
CREATE INDEX IF NOT EXISTS idx_etf_holdings_stock ON etf_holdings(stock_code);
CREATE INDEX IF NOT EXISTS idx_market_snapshot_code ON etf_market_snapshot(etf_code);
CREATE INDEX IF NOT EXISTS idx_nav_history_code ON etf_nav_history(etf_code);
