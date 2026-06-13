"""
emega ETFMaster → SQLite ETL Pipeline

Usage:
  python etl.py --init          # create DB and run full initial load
  python etl.py --etf           # refresh ETF list only
  python etl.py --stocks        # refresh stock master list only
  python etl.py --holdings 2330 # fetch holdings for one stock
  python etl.py --holdings-all  # fetch all stocks (slow, rate limited)
  python etl.py --etf-weight 0050  # fetch ETF→stock weight for one ETF
  python etl.py --etf-weight-all   # fetch weights for all ETFs
  python etl.py --discover      # auto-discover API endpoint paths
  python etl.py --dry-run --init   # print what would be fetched without writing
"""

import argparse
import json
import logging
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import requests

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

BASE_URL = "https://www.emega.com.tw/etfmaster"

DB_PATH = Path(os.environ.get("ETF_DB_PATH", Path.home() / "Desktop" / "etf_master.db"))
SCHEMA_PATH = Path(__file__).parent / "schema.sql"
CHECKPOINT_PATH = Path(__file__).parent / "etl_checkpoint.json"
ERROR_LOG_PATH = Path(__file__).parent / "etl_errors.log"

DEFAULT_DELAY = 0.3  # seconds between requests

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-TW,zh;q=0.9,en-US;q=0.8,en;q=0.7",
    "Referer": "https://www.emega.com.tw/etfmaster/",
}

# ---------------------------------------------------------------------------
# Field mapping: emega API keys → schema columns
# ---------------------------------------------------------------------------

EMEGA_ETF_FIELD_MAP = {
    # ETF code
    "etfId": "etf_code",
    "etf_id": "etf_code",
    "stockId": "etf_code",
    "stock_id": "etf_code",
    "etfCode": "etf_code",
    "etf_code": "etf_code",
    "code": "etf_code",
    # ETF name
    "etfName": "etf_name",
    "etf_name": "etf_name",
    "stockName": "etf_name",
    "stock_name": "etf_name",
    "name": "etf_name",
    "fullName": "etf_name",
    # Issuer / fund manager
    "issuer": "issuer",
    "fundManager": "issuer",
    "assetMgr": "issuer",
    "managerName": "issuer",
    "company": "issuer",
    # Category
    "category": "category",
    "etfCategory": "category",
    "type_category": "category",
    "classType": "category",
    # ETF type
    "etfType": "etf_type",
    "etf_type": "etf_type",
    "productType": "etf_type",
    # Region
    "region": "region",
    "investRegion": "region",
    "market": "region",
    # Leverage type
    "leverageType": "leverage_type",
    "leverage_type": "leverage_type",
    "leverageFactor": "leverage_type",
    # Dividend type / frequency
    "dividendFreq": "dividend_type",
    "dividendType": "dividend_type",
    "dividend_type": "dividend_type",
    "dividendPolicy": "dividend_type",
    # Tracking index
    "trackingIndex": "tracking_index",
    "indexName": "tracking_index",
    "tracking_index": "tracking_index",
    "benchmarkIndex": "tracking_index",
    "indexCode": "tracking_index",
    # Currency
    "currency": "currency",
    "tradeCurrency": "currency",
    # Inception / listing date
    "inceptionDate": "inception_date",
    "listingDate": "inception_date",
    "inception_date": "inception_date",
    "ipoDate": "inception_date",
    "foundDate": "inception_date",
    # Expense ratio
    "expenseRatio": "expense_ratio",
    "totalExpenseRatio": "expense_ratio",
    "expense_ratio": "expense_ratio",
    "ter": "expense_ratio",
    # AUM
    "aumBillion": "aum_billion",
    "totalNetAsset": "aum_billion",
    "aum_billion": "aum_billion",
    "netAsset": "aum_billion",
    "fundSize": "aum_billion",
    # Beneficiary count
    "beneficiaryNum": "beneficiary_count",
    "beneficiaryCount": "beneficiary_count",
    "holderCount": "beneficiary_count",
}

EMEGA_STOCK_FIELD_MAP = {
    "stockId": "stock_code",
    "stock_id": "stock_code",
    "stockCode": "stock_code",
    "stock_code": "stock_code",
    "code": "stock_code",
    "stockName": "stock_name",
    "stock_name": "stock_name",
    "name": "stock_name",
    "industry": "industry",
    "industryType": "industry",
    "sector": "industry",
    "market": "market",
    "exchange": "market",
    "listedMarket": "market",
}

# Candidate URL patterns for API discovery
ETF_LIST_CANDIDATES = [
    f"{BASE_URL}/api/etf",
    f"{BASE_URL}/api/ETF",
    f"{BASE_URL}/api/etfList",
    f"{BASE_URL}/api/getEtfList",
    f"{BASE_URL}/etfList.do?action=getList",
    f"{BASE_URL}/index.do?action=apiEtf",
    f"{BASE_URL}/api/v1/etf",
    f"{BASE_URL}/api/fund/etf",
    f"{BASE_URL}/etf/api/list",
]

STOCK_LIST_CANDIDATES = [
    f"{BASE_URL}/api/stocks",
    f"{BASE_URL}/api/stock",
    f"{BASE_URL}/api/stockList",
    f"{BASE_URL}/api/getStockList",
    f"{BASE_URL}/stock/api/list",
    f"{BASE_URL}/api/v1/stocks",
]

STOCK_ETF_CANDIDATES = [
    f"{BASE_URL}/stockRefEtf/api/stocksEtfShare",
    f"{BASE_URL}/api/stocksEtfShare",
    f"{BASE_URL}/api/stockEtfShare",
    f"{BASE_URL}/api/stockRefEtf",
    f"{BASE_URL}/stockEtf/api/share",
    f"{BASE_URL}/api/v1/stocksEtfShare",
]

ETF_WEIGHT_CANDIDATES = [
    f"{BASE_URL}/api/etfStocksWeight",
    f"{BASE_URL}/api/etfWeight",
    f"{BASE_URL}/api/getEtfWeight",
    f"{BASE_URL}/etfWeight/api/list",
    f"{BASE_URL}/api/v1/etfStocksWeight",
    f"{BASE_URL}/api/etfHoldings",
]

# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("etl")

file_handler = logging.FileHandler(ERROR_LOG_PATH, encoding="utf-8")
file_handler.setLevel(logging.WARNING)
file_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
logger.addHandler(file_handler)


# ---------------------------------------------------------------------------
# HTTP Session
# ---------------------------------------------------------------------------

def make_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(HEADERS)
    return session


# ---------------------------------------------------------------------------
# Endpoint discovery
# ---------------------------------------------------------------------------

def _probe_url(session: requests.Session, url: str, param_name: Optional[str] = None,
               param_value: Optional[str] = None, min_items: int = 5) -> bool:
    """Return True if the URL returns a JSON array with at least min_items elements."""
    params = {}
    if param_name and param_value:
        params[param_name] = param_value
    try:
        resp = session.get(url, params=params, timeout=10)
        if resp.status_code != 200:
            return False
        ct = resp.headers.get("Content-Type", "")
        if "json" not in ct and not resp.text.strip().startswith(("[", "{")):
            return False
        data = resp.json()
        # Accept list directly or {"data": [...]} or {"result": [...]}
        if isinstance(data, list):
            return len(data) >= min_items
        if isinstance(data, dict):
            for key in ("data", "result", "list", "items", "etfList", "stockList"):
                if key in data and isinstance(data[key], list) and len(data[key]) >= min_items:
                    return True
        return False
    except Exception:
        return False


def discover_endpoints(session: requests.Session) -> dict:
    """Try multiple URL patterns to find working API endpoints. Returns dict of role→url."""
    print("=== Discovering API endpoints ===")
    found = {}

    print("Probing ETF list endpoints...")
    for url in ETF_LIST_CANDIDATES:
        print(f"  trying {url} ...", end=" ", flush=True)
        if _probe_url(session, url, min_items=10):
            print("OK")
            found["etf_list"] = url
            break
        print("no")

    print("Probing stock list endpoints...")
    for url in STOCK_LIST_CANDIDATES:
        print(f"  trying {url} ...", end=" ", flush=True)
        if _probe_url(session, url, min_items=5):
            print("OK")
            found["stock_list"] = url
            break
        print("no")

    print("Probing stock→ETF holdings endpoints...")
    for url in STOCK_ETF_CANDIDATES:
        print(f"  trying {url} ...", end=" ", flush=True)
        if _probe_url(session, url, param_name="stockCode", param_value="2330", min_items=1):
            print("OK")
            found["stock_etf"] = url
            break
        print("no")

    print("Probing ETF→stock weight endpoints...")
    for url in ETF_WEIGHT_CANDIDATES:
        print(f"  trying {url} ...", end=" ", flush=True)
        if _probe_url(session, url, param_name="etfCode", param_value="0050", min_items=1):
            print("OK")
            found["etf_weight"] = url
            break
        print("no")

    if found:
        print("\nDiscovered endpoints:")
        for role, url in found.items():
            print(f"  {role}: {url}")
    else:
        print("\nNo endpoints discovered. The API may require authentication or the site structure has changed.")

    return found


# ---------------------------------------------------------------------------
# Checkpoint helpers
# ---------------------------------------------------------------------------

def load_checkpoint() -> dict:
    if CHECKPOINT_PATH.exists():
        try:
            return json.loads(CHECKPOINT_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def save_checkpoint(data: dict) -> None:
    try:
        existing = load_checkpoint()
        existing.update(data)
        CHECKPOINT_PATH.write_text(json.dumps(existing, indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception as exc:
        logger.warning(f"Failed to save checkpoint: {exc}")


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------

def get_connection() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    if not SCHEMA_PATH.exists():
        logger.error(f"Schema file not found: {SCHEMA_PATH}")
        sys.exit(1)
    schema = SCHEMA_PATH.read_text(encoding="utf-8")
    conn.executescript(schema)
    conn.commit()
    logger.info(f"Database initialized at {DB_PATH}")


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def today_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# Data normalization helpers
# ---------------------------------------------------------------------------

def _extract_list(data: Any) -> list:
    """Extract a list from various API response shapes."""
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("data", "result", "list", "items", "etfList", "stockList",
                    "etfs", "stocks", "records"):
            val = data.get(key)
            if isinstance(val, list):
                return val
    return []


def _map_fields(record: dict, field_map: dict) -> dict:
    """Map API field names to schema column names. Unknown fields are discarded."""
    out = {}
    for api_key, api_val in record.items():
        schema_col = field_map.get(api_key)
        if schema_col and schema_col not in out:
            out[schema_col] = api_val
    return out


def _safe_float(val: Any) -> Optional[float]:
    if val is None or val == "" or val == "--":
        return None
    try:
        return float(str(val).replace(",", "").replace("%", "").strip())
    except (ValueError, TypeError):
        return None


def _safe_int(val: Any) -> Optional[int]:
    if val is None or val == "" or val == "--":
        return None
    try:
        return int(str(val).replace(",", "").strip())
    except (ValueError, TypeError):
        return None


def _normalize_date(val: Any) -> Optional[str]:
    if not val or val == "--":
        return None
    s = str(val).strip()
    # Already ISO
    if len(s) == 10 and s[4] == "-":
        return s
    # YYYYMMDD
    if len(s) == 8 and s.isdigit():
        return f"{s[:4]}-{s[4:6]}-{s[6:]}"
    # Try common separators
    for fmt in ("%Y/%m/%d", "%d/%m/%Y", "%Y.%m.%d", "%Y%m%d"):
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%d")
        except ValueError:
            pass
    return s  # Return as-is if we can't parse


# ---------------------------------------------------------------------------
# ETF profile fetch & upsert
# ---------------------------------------------------------------------------

def fetch_etf_list(session: requests.Session, url: str) -> list:
    """Fetch the full ETF list from emega API."""
    logger.info(f"Fetching ETF list from {url}")
    try:
        resp = session.get(url, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        records = _extract_list(data)
        logger.info(f"Fetched {len(records)} ETF records")
        return records
    except Exception as exc:
        logger.error(f"Failed to fetch ETF list: {exc}")
        return []


def upsert_etf_profile(conn: sqlite3.Connection, records: list, dry_run: bool = False) -> int:
    """Upsert ETF profile records. Returns count of upserted rows."""
    today = today_iso()
    now = now_iso()
    upserted = 0
    snapshot_rows = []

    for rec in records:
        mapped = _map_fields(rec, EMEGA_ETF_FIELD_MAP)
        if not mapped.get("etf_code"):
            # Try to find any code-like key as fallback
            for key in rec:
                if "id" in key.lower() or "code" in key.lower():
                    mapped["etf_code"] = str(rec[key]).strip()
                    break
        if not mapped.get("etf_code"):
            continue

        etf_code = str(mapped["etf_code"]).strip()

        row = {
            "etf_code": etf_code,
            "etf_name": mapped.get("etf_name"),
            "issuer": mapped.get("issuer"),
            "category": mapped.get("category"),
            "etf_type": mapped.get("etf_type"),
            "region": mapped.get("region"),
            "leverage_type": mapped.get("leverage_type"),
            "dividend_type": mapped.get("dividend_type"),
            "tracking_index": mapped.get("tracking_index"),
            "currency": mapped.get("currency", "TWD") or "TWD",
            "inception_date": _normalize_date(mapped.get("inception_date")),
            "expense_ratio": _safe_float(mapped.get("expense_ratio")),
            "aum_billion": _safe_float(mapped.get("aum_billion")),
            "beneficiary_count": _safe_int(mapped.get("beneficiary_count")),
            "raw_json": json.dumps(rec, ensure_ascii=False),
            "fetched_at": now,
        }

        if dry_run:
            print(f"  [DRY-RUN] Would upsert ETF profile: {etf_code} — {row.get('etf_name')}")
            upserted += 1
            continue

        conn.execute(
            """
            INSERT INTO etf_profile
                (etf_code, etf_name, issuer, category, etf_type, region,
                 leverage_type, dividend_type, tracking_index, currency,
                 inception_date, expense_ratio, aum_billion, beneficiary_count,
                 raw_json, fetched_at)
            VALUES
                (:etf_code, :etf_name, :issuer, :category, :etf_type, :region,
                 :leverage_type, :dividend_type, :tracking_index, :currency,
                 :inception_date, :expense_ratio, :aum_billion, :beneficiary_count,
                 :raw_json, :fetched_at)
            ON CONFLICT(etf_code) DO UPDATE SET
                etf_name = excluded.etf_name,
                issuer = excluded.issuer,
                category = excluded.category,
                etf_type = excluded.etf_type,
                region = excluded.region,
                leverage_type = excluded.leverage_type,
                dividend_type = excluded.dividend_type,
                tracking_index = excluded.tracking_index,
                currency = excluded.currency,
                inception_date = excluded.inception_date,
                expense_ratio = excluded.expense_ratio,
                aum_billion = excluded.aum_billion,
                beneficiary_count = excluded.beneficiary_count,
                raw_json = excluded.raw_json,
                fetched_at = excluded.fetched_at
            """,
            row,
        )
        upserted += 1

        # Also build a market snapshot row from whatever price data is in the record
        close_price = _safe_float(rec.get("closePrice") or rec.get("close") or rec.get("price"))
        nav = _safe_float(rec.get("nav") or rec.get("NAV") or rec.get("netAssetValue"))
        pd_pct = _safe_float(rec.get("premiumDiscount") or rec.get("pricePremium"))
        vol = _safe_int(rec.get("volume") or rec.get("tradeVolume"))
        ytd = _safe_float(rec.get("ytdReturn") or rec.get("ytd_return") or rec.get("returnYtd"))
        one_yr = _safe_float(rec.get("oneYearReturn") or rec.get("return1Y") or rec.get("return_1y"))

        if any(v is not None for v in (close_price, nav, pd_pct, vol, ytd, one_yr)):
            snapshot_rows.append({
                "etf_code": etf_code,
                "snapshot_date": today,
                "close_price": close_price,
                "nav": nav,
                "premium_discount_pct": pd_pct,
                "volume_k": vol,
                "ytd_return": ytd,
                "one_year_return": one_yr,
            })

    if not dry_run:
        for snap in snapshot_rows:
            conn.execute(
                """
                INSERT INTO etf_market_snapshot
                    (etf_code, snapshot_date, close_price, nav, premium_discount_pct,
                     volume_k, ytd_return, one_year_return)
                VALUES
                    (:etf_code, :snapshot_date, :close_price, :nav, :premium_discount_pct,
                     :volume_k, :ytd_return, :one_year_return)
                ON CONFLICT(etf_code, snapshot_date) DO UPDATE SET
                    close_price = excluded.close_price,
                    nav = excluded.nav,
                    premium_discount_pct = excluded.premium_discount_pct,
                    volume_k = excluded.volume_k,
                    ytd_return = excluded.ytd_return,
                    one_year_return = excluded.one_year_return
                """,
                snap,
            )
        conn.commit()

    return upserted


# ---------------------------------------------------------------------------
# Stock master fetch & upsert
# ---------------------------------------------------------------------------

def fetch_stock_list(session: requests.Session, url: str) -> list:
    logger.info(f"Fetching stock list from {url}")
    try:
        resp = session.get(url, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        records = _extract_list(data)
        logger.info(f"Fetched {len(records)} stock records")
        return records
    except Exception as exc:
        logger.error(f"Failed to fetch stock list: {exc}")
        return []


def upsert_stock_master(conn: sqlite3.Connection, records: list, dry_run: bool = False) -> int:
    now = now_iso()
    upserted = 0
    for rec in records:
        mapped = _map_fields(rec, EMEGA_STOCK_FIELD_MAP)
        if not mapped.get("stock_code"):
            for key in rec:
                if "id" in key.lower() or "code" in key.lower():
                    mapped["stock_code"] = str(rec[key]).strip()
                    break
        if not mapped.get("stock_code"):
            continue

        row = {
            "stock_code": str(mapped["stock_code"]).strip(),
            "stock_name": mapped.get("stock_name"),
            "industry": mapped.get("industry"),
            "market": mapped.get("market"),
            "updated_at": now,
        }

        if dry_run:
            print(f"  [DRY-RUN] Would upsert stock: {row['stock_code']} — {row.get('stock_name')}")
            upserted += 1
            continue

        conn.execute(
            """
            INSERT INTO stock_master (stock_code, stock_name, industry, market, updated_at)
            VALUES (:stock_code, :stock_name, :industry, :market, :updated_at)
            ON CONFLICT(stock_code) DO UPDATE SET
                stock_name = excluded.stock_name,
                industry = excluded.industry,
                market = excluded.market,
                updated_at = excluded.updated_at
            """,
            row,
        )
        upserted += 1

    if not dry_run:
        conn.commit()

    return upserted


# ---------------------------------------------------------------------------
# Stock → ETF holdings fetch
# ---------------------------------------------------------------------------

def fetch_stock_holdings(session: requests.Session, url: str, stock_code: str) -> list:
    """Fetch which ETFs hold a given stock."""
    try:
        resp = session.get(url, params={"stockCode": stock_code}, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        return _extract_list(data)
    except Exception as exc:
        logger.warning(f"Failed to fetch holdings for stock {stock_code}: {exc}")
        return []


def upsert_stock_etf_holdings(
    conn: sqlite3.Connection,
    stock_code: str,
    records: list,
    dry_run: bool = False,
) -> int:
    today = today_iso()
    upserted = 0

    for rec in records:
        # Field names vary across sites — try common patterns
        etf_code = (
            rec.get("etfId") or rec.get("etfCode") or rec.get("etf_code")
            or rec.get("etfId") or rec.get("code")
        )
        if not etf_code:
            continue

        etf_name = rec.get("etfName") or rec.get("etf_name") or rec.get("name")
        weight = _safe_float(
            rec.get("holdingWeightPct") or rec.get("weight") or rec.get("proportion")
            or rec.get("ratio") or rec.get("holdRatio")
        )
        shares = _safe_int(
            rec.get("holdingShares") or rec.get("shares") or rec.get("holdShares")
        )

        row = {
            "stock_code": stock_code,
            "etf_code": str(etf_code).strip(),
            "etf_name": etf_name,
            "holding_weight_pct": weight,
            "holding_shares": shares,
            "snapshot_date": today,
        }

        if dry_run:
            print(f"  [DRY-RUN] Would upsert stock_etf_holdings: {stock_code} → {etf_code}")
            upserted += 1
            continue

        conn.execute(
            """
            INSERT INTO stock_etf_holdings
                (stock_code, etf_code, etf_name, holding_weight_pct, holding_shares, snapshot_date)
            VALUES
                (:stock_code, :etf_code, :etf_name, :holding_weight_pct, :holding_shares, :snapshot_date)
            ON CONFLICT(stock_code, etf_code, snapshot_date) DO UPDATE SET
                etf_name = excluded.etf_name,
                holding_weight_pct = excluded.holding_weight_pct,
                holding_shares = excluded.holding_shares
            """,
            row,
        )
        upserted += 1

    if not dry_run:
        conn.commit()

    return upserted


def fetch_stock_holdings_all(
    conn: sqlite3.Connection,
    session: requests.Session,
    url: str,
    stock_codes: list,
    delay: float = DEFAULT_DELAY,
    batch_size: int = 50,
    dry_run: bool = False,
) -> dict:
    """Fetch holdings for all stocks with rate limiting and checkpointing."""
    checkpoint = load_checkpoint()
    done = set(checkpoint.get("stock_holdings_done", []))
    errors = checkpoint.get("stock_holdings_errors", [])
    total = len(stock_codes)
    success_count = 0
    error_count = 0

    print(f"Fetching stock→ETF holdings for {total} stocks (already done: {len(done)})")

    for i, code in enumerate(stock_codes):
        if code in done:
            continue

        if i % batch_size == 0 and i > 0:
            save_checkpoint({
                "stock_holdings_done": list(done),
                "stock_holdings_errors": errors,
                "stock_holdings_last_updated": now_iso(),
            })

        print(f"  [{i+1}/{total}] stock {code} ...", end=" ", flush=True)

        if dry_run:
            print("[DRY-RUN]")
            done.add(code)
            success_count += 1
            continue

        records = fetch_stock_holdings(session, url, code)
        if records is not None:
            n = upsert_stock_etf_holdings(conn, code, records)
            print(f"{n} ETF(s)")
            done.add(code)
            success_count += 1
        else:
            print("ERROR")
            errors.append({"code": code, "ts": now_iso()})
            error_count += 1

        time.sleep(delay)

    save_checkpoint({
        "stock_holdings_done": list(done),
        "stock_holdings_errors": errors,
        "stock_holdings_last_updated": now_iso(),
    })

    print(f"Done: {success_count} success, {error_count} errors")
    return {"success": success_count, "errors": error_count}


# ---------------------------------------------------------------------------
# ETF → stock weight fetch
# ---------------------------------------------------------------------------

def fetch_etf_weight(session: requests.Session, url: str, etf_code: str) -> list:
    """Fetch holdings/weight breakdown for a given ETF."""
    try:
        resp = session.get(url, params={"etfCode": etf_code}, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        return _extract_list(data)
    except Exception as exc:
        logger.warning(f"Failed to fetch weight for ETF {etf_code}: {exc}")
        return []


def upsert_etf_holdings(
    conn: sqlite3.Connection,
    etf_code: str,
    records: list,
    dry_run: bool = False,
) -> int:
    today = today_iso()
    upserted = 0

    for rec in records:
        stock_code = (
            rec.get("stockId") or rec.get("stockCode") or rec.get("stock_code")
            or rec.get("id") or rec.get("code")
        )
        if not stock_code:
            continue

        stock_name = rec.get("stockName") or rec.get("stock_name") or rec.get("name")
        weight = _safe_float(
            rec.get("weight") or rec.get("weightPct") or rec.get("proportion")
            or rec.get("ratio") or rec.get("holdRatio") or rec.get("holdingRatio")
        )
        shares = _safe_int(
            rec.get("shares") or rec.get("holdShares") or rec.get("holdingShares")
        )
        mkt_val = _safe_float(
            rec.get("marketValue") or rec.get("market_value") or rec.get("holdValue")
        )

        row = {
            "etf_code": etf_code,
            "stock_code": str(stock_code).strip(),
            "stock_name": stock_name,
            "weight_pct": weight,
            "shares": shares,
            "market_value": mkt_val,
            "snapshot_date": today,
        }

        if dry_run:
            print(f"  [DRY-RUN] Would upsert etf_holdings: {etf_code} → {stock_code}")
            upserted += 1
            continue

        conn.execute(
            """
            INSERT INTO etf_holdings
                (etf_code, stock_code, stock_name, weight_pct, shares, market_value, snapshot_date)
            VALUES
                (:etf_code, :stock_code, :stock_name, :weight_pct, :shares, :market_value, :snapshot_date)
            ON CONFLICT(etf_code, stock_code, snapshot_date) DO UPDATE SET
                stock_name = excluded.stock_name,
                weight_pct = excluded.weight_pct,
                shares = excluded.shares,
                market_value = excluded.market_value
            """,
            row,
        )

        # Also insert into etf_stock_weight_history for time-series
        conn.execute(
            """
            INSERT INTO etf_stock_weight_history (etf_code, stock_code, record_date, weight_pct)
            VALUES (:etf_code, :stock_code, :record_date, :weight_pct)
            ON CONFLICT(etf_code, stock_code, record_date) DO UPDATE SET
                weight_pct = excluded.weight_pct
            """,
            {
                "etf_code": etf_code,
                "stock_code": str(stock_code).strip(),
                "record_date": today,
                "weight_pct": weight,
            },
        )
        upserted += 1

    if not dry_run:
        conn.commit()

    return upserted


def fetch_etf_weight_all(
    conn: sqlite3.Connection,
    session: requests.Session,
    url: str,
    etf_codes: list,
    delay: float = DEFAULT_DELAY,
    batch_size: int = 50,
    dry_run: bool = False,
) -> dict:
    """Fetch ETF→stock weights for all ETFs with rate limiting."""
    checkpoint = load_checkpoint()
    done = set(checkpoint.get("etf_weight_done", []))
    errors = checkpoint.get("etf_weight_errors", [])
    total = len(etf_codes)
    success_count = 0
    error_count = 0

    print(f"Fetching ETF→stock weights for {total} ETFs (already done: {len(done)})")

    for i, code in enumerate(etf_codes):
        if code in done:
            continue

        if i % batch_size == 0 and i > 0:
            save_checkpoint({
                "etf_weight_done": list(done),
                "etf_weight_errors": errors,
                "etf_weight_last_updated": now_iso(),
            })

        print(f"  [{i+1}/{total}] ETF {code} ...", end=" ", flush=True)

        if dry_run:
            print("[DRY-RUN]")
            done.add(code)
            success_count += 1
            continue

        records = fetch_etf_weight(session, url, code)
        n = upsert_etf_holdings(conn, code, records)
        print(f"{n} stock(s)")
        done.add(code)
        success_count += 1

        time.sleep(delay)

    save_checkpoint({
        "etf_weight_done": list(done),
        "etf_weight_errors": errors,
        "etf_weight_last_updated": now_iso(),
    })

    print(f"Done: {success_count} success, {error_count} errors")
    return {"success": success_count, "errors": error_count}


# ---------------------------------------------------------------------------
# URL resolution helpers — use discovered or defaults
# ---------------------------------------------------------------------------

_endpoint_cache: dict = {}


def get_endpoint(role: str, session: requests.Session) -> Optional[str]:
    """Return the working URL for a role, discovering if needed."""
    global _endpoint_cache
    if not _endpoint_cache:
        _endpoint_cache = load_checkpoint().get("discovered_endpoints", {})

    if role in _endpoint_cache:
        return _endpoint_cache[role]

    # Fallback defaults (first candidate)
    defaults = {
        "etf_list": ETF_LIST_CANDIDATES[0],
        "stock_list": STOCK_LIST_CANDIDATES[0],
        "stock_etf": STOCK_ETF_CANDIDATES[0],
        "etf_weight": ETF_WEIGHT_CANDIDATES[0],
    }
    return defaults.get(role)


# ---------------------------------------------------------------------------
# High-level orchestration
# ---------------------------------------------------------------------------

def run_full_init(conn: sqlite3.Connection, session: requests.Session, dry_run: bool = False) -> None:
    """Initial full load: ETF list + stock master."""
    print("\n--- Step 1: Discover endpoints ---")
    endpoints = discover_endpoints(session)
    save_checkpoint({"discovered_endpoints": endpoints})
    global _endpoint_cache
    _endpoint_cache = endpoints

    print("\n--- Step 2: ETF profiles ---")
    etf_url = endpoints.get("etf_list", ETF_LIST_CANDIDATES[0])
    records = fetch_etf_list(session, etf_url)
    if records:
        n = upsert_etf_profile(conn, records, dry_run=dry_run)
        print(f"Upserted {n} ETF profiles")
    else:
        print("WARNING: No ETF records returned from API")

    print("\n--- Step 3: Stock master ---")
    stock_url = endpoints.get("stock_list", STOCK_LIST_CANDIDATES[0])
    stocks = fetch_stock_list(session, stock_url)
    if stocks:
        n = upsert_stock_master(conn, stocks, dry_run=dry_run)
        print(f"Upserted {n} stock records")
    else:
        print("WARNING: No stock records returned from API")

    save_checkpoint({"last_full_init": now_iso()})
    print("\nInitial load complete.")


def run_etf_refresh(conn: sqlite3.Connection, session: requests.Session, dry_run: bool = False) -> None:
    etf_url = get_endpoint("etf_list", session)
    records = fetch_etf_list(session, etf_url)
    if records:
        n = upsert_etf_profile(conn, records, dry_run=dry_run)
        print(f"Upserted {n} ETF profiles")
    else:
        print("WARNING: No ETF records returned")


def run_stocks_refresh(conn: sqlite3.Connection, session: requests.Session, dry_run: bool = False) -> None:
    stock_url = get_endpoint("stock_list", session)
    stocks = fetch_stock_list(session, stock_url)
    if stocks:
        n = upsert_stock_master(conn, stocks, dry_run=dry_run)
        print(f"Upserted {n} stock records")
    else:
        print("WARNING: No stock records returned")


def run_holdings_one(
    conn: sqlite3.Connection,
    session: requests.Session,
    stock_code: str,
    dry_run: bool = False,
) -> None:
    url = get_endpoint("stock_etf", session)
    records = fetch_stock_holdings(session, url, stock_code)
    n = upsert_stock_etf_holdings(conn, stock_code, records, dry_run=dry_run)
    print(f"Upserted {n} holdings for stock {stock_code}")


def run_holdings_all(
    conn: sqlite3.Connection,
    session: requests.Session,
    delay: float = DEFAULT_DELAY,
    dry_run: bool = False,
) -> None:
    url = get_endpoint("stock_etf", session)
    # Get stock codes from DB, fallback to fetching from API
    rows = conn.execute("SELECT stock_code FROM stock_master ORDER BY stock_code").fetchall()
    if rows:
        stock_codes = [r["stock_code"] for r in rows]
    else:
        logger.info("No stocks in DB, fetching stock list first")
        stock_url = get_endpoint("stock_list", session)
        stocks = fetch_stock_list(session, stock_url)
        upsert_stock_master(conn, stocks)
        rows = conn.execute("SELECT stock_code FROM stock_master ORDER BY stock_code").fetchall()
        stock_codes = [r["stock_code"] for r in rows]

    fetch_stock_holdings_all(conn, session, url, stock_codes, delay=delay, dry_run=dry_run)


def run_etf_weight_one(
    conn: sqlite3.Connection,
    session: requests.Session,
    etf_code: str,
    dry_run: bool = False,
) -> None:
    url = get_endpoint("etf_weight", session)
    records = fetch_etf_weight(session, url, etf_code)
    n = upsert_etf_holdings(conn, etf_code, records, dry_run=dry_run)
    print(f"Upserted {n} holdings for ETF {etf_code}")


def run_etf_weight_all(
    conn: sqlite3.Connection,
    session: requests.Session,
    delay: float = DEFAULT_DELAY,
    dry_run: bool = False,
) -> None:
    url = get_endpoint("etf_weight", session)
    rows = conn.execute("SELECT etf_code FROM etf_profile ORDER BY etf_code").fetchall()
    if not rows:
        print("No ETFs in DB. Run --etf first.")
        return
    etf_codes = [r["etf_code"] for r in rows]
    fetch_etf_weight_all(conn, session, url, etf_codes, delay=delay, dry_run=dry_run)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="emega ETFMaster → SQLite ETL Pipeline",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--init", action="store_true", help="Create DB and run full initial load")
    parser.add_argument("--etf", action="store_true", help="Refresh ETF list only")
    parser.add_argument("--stocks", action="store_true", help="Refresh stock master list only")
    parser.add_argument("--holdings", metavar="STOCK_CODE", help="Fetch holdings for one stock")
    parser.add_argument("--holdings-all", action="store_true", help="Fetch all stock holdings")
    parser.add_argument("--etf-weight", metavar="ETF_CODE", help="Fetch ETF→stock weight for one ETF")
    parser.add_argument("--etf-weight-all", action="store_true", help="Fetch weights for all ETFs")
    parser.add_argument("--discover", action="store_true", help="Auto-discover API endpoint paths")
    parser.add_argument("--delay", type=float, default=DEFAULT_DELAY, help=f"Rate-limit delay (default: {DEFAULT_DELAY}s)")
    parser.add_argument("--dry-run", action="store_true", help="Print what would be fetched without writing to DB")
    parser.add_argument("--db", metavar="PATH", help="Override DB path")

    args = parser.parse_args()

    if args.db:
        global DB_PATH
        DB_PATH = Path(args.db)

    if args.dry_run:
        print(f"[DRY-RUN MODE] DB path: {DB_PATH}")

    session = make_session()

    # Always ensure DB exists
    conn = get_connection()
    init_db(conn)

    if args.discover:
        endpoints = discover_endpoints(session)
        save_checkpoint({"discovered_endpoints": endpoints})
        return

    if args.init:
        run_full_init(conn, session, dry_run=args.dry_run)
        return

    if args.etf:
        run_etf_refresh(conn, session, dry_run=args.dry_run)
        return

    if args.stocks:
        run_stocks_refresh(conn, session, dry_run=args.dry_run)
        return

    if args.holdings:
        run_holdings_one(conn, session, args.holdings, dry_run=args.dry_run)
        return

    if args.holdings_all:
        run_holdings_all(conn, session, delay=args.delay, dry_run=args.dry_run)
        return

    if args.etf_weight:
        run_etf_weight_one(conn, session, args.etf_weight, dry_run=args.dry_run)
        return

    if args.etf_weight_all:
        run_etf_weight_all(conn, session, delay=args.delay, dry_run=args.dry_run)
        return

    parser.print_help()


if __name__ == "__main__":
    main()
