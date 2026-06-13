"""
emega ETFMaster 持倉抓取器 — Cookie 注入版

一次性步驟（只需做一次）：
  1. Chrome 開啟 https://www.emega.com.tw/etfmaster/index.do
  2. 按 F12 → Network 分頁 → 重新整理頁面
  3. 點任一個請求 → Headers → 找到 "Cookie:" 那一行
  4. 複製整串 cookie 值（很長，從 JSESSIONID= 開始）
  5. 存成檔案：echo '貼上cookie字串' > ~/.etf_session_cookie

之後每次執行：
  python etf_db/emega_holdings.py --discover           # 找到正確 API endpoint
  python etf_db/emega_holdings.py --etf-list           # 抓 ETF 清單（343 檔，補充欄位）
  python etf_db/emega_holdings.py --holdings-all       # 抓全部持倉（559 股 × ~0.4s）
  python etf_db/emega_holdings.py --holdings 2330      # 抓單支股票的 ETF 持倉
  python etf_db/emega_holdings.py --etf-weight 0050    # 抓單一 ETF 的成分股權重
  python etf_db/emega_holdings.py --etf-weight-all     # 抓全部 ETF 成分股（慢，數百檔）
"""

import argparse
import json
import logging
import os
import re
import sqlite3
import time
from datetime import date, datetime
from pathlib import Path
from typing import Optional

import requests
from bs4 import BeautifulSoup

# ─────────────────────────────────────────────
# 設定
# ─────────────────────────────────────────────
DB_PATH = Path(os.environ.get("ETF_DB_PATH", Path.home() / "Desktop" / "etf_master.db"))
COOKIE_PATH = Path(os.environ.get("EMEGA_COOKIE_PATH", Path.home() / ".etf_session_cookie"))
ENDPOINTS_CACHE = Path(__file__).parent / "emega_endpoints.json"

BASE = "https://www.emega.com.tw/etfmaster"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("emega_holdings")


# ─────────────────────────────────────────────
# Session 建立（注入 Cookie）
# ─────────────────────────────────────────────
def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json, text/html, */*",
        "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8",
        "Referer": f"{BASE}/index.do",
        "X-Requested-With": "XMLHttpRequest",
    })

    if COOKIE_PATH.exists():
        raw = COOKIE_PATH.read_text(encoding="utf-8").strip()
        if raw:
            s.headers["Cookie"] = raw
            log.info(f"✓ 已載入 cookie（{len(raw)} 字元，來自 {COOKIE_PATH}）")
        else:
            log.warning("⚠ cookie 檔案是空的")
    else:
        log.warning(
            f"⚠ 找不到 cookie 檔案 {COOKIE_PATH}\n"
            "  請依照以下步驟取得 cookie：\n"
            "  1. Chrome 開啟 https://www.emega.com.tw/etfmaster/index.do\n"
            "  2. F12 → Network → 重新整理\n"
            "  3. 點任一請求 → Headers → 複製 Cookie: 的值\n"
            f"  4. echo '貼上cookie' > {COOKIE_PATH}"
        )
    return s


# ─────────────────────────────────────────────
# DB 連線
# ─────────────────────────────────────────────
def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


# ─────────────────────────────────────────────
# 1. Endpoint 自動探索
# ─────────────────────────────────────────────
ENDPOINT_CANDIDATES = {
    "etf_list": [
        f"{BASE}/api/etf",
        f"{BASE}/api/ETF",
        f"{BASE}/api/getEtfList",
        f"{BASE}/etfList.do",
        f"{BASE}/index.do?action=getList",
        f"{BASE}/index.do?action=apiEtf",
    ],
    "stock_list": [
        f"{BASE}/api/stocks",
        f"{BASE}/api/stockList",
        f"{BASE}/stockRefEtf/api/stocks",
        f"{BASE}/stockRefEtf/index.do?action=getStockList",
    ],
    "stock_holdings": [
        # 個股代號由 {code} 替換
        f"{BASE}/api/stocksEtfShare?stockCode={{code}}",
        f"{BASE}/stockRefEtf/api/stocksEtfShare?stockCode={{code}}",
        f"{BASE}/stockRefEtf/index.do?action=queryByStock&stockCode={{code}}",
        f"{BASE}/api/stockEtf?stockCode={{code}}",
        f"{BASE}/stockRefEtf/api/query?stockCode={{code}}",
    ],
    "etf_weight": [
        # ETF 代號由 {code} 替換
        f"{BASE}/api/etfStocksWeight?etfCode={{code}}",
        f"{BASE}/etfStocksWeight/api/query?etfCode={{code}}",
        f"{BASE}/api/etfHolding?etfCode={{code}}",
        f"{BASE}/api/holdings?etfCode={{code}}",
        f"{BASE}/index.do?action=getHoldings&etfCode={{code}}",
    ],
}

PROBE_STOCK = "2330"
PROBE_ETF   = "0050"


def _is_json_list(resp: requests.Response, min_items: int = 1) -> bool:
    """回應是否為含資料的 JSON 陣列"""
    if resp.status_code != 200:
        return False
    ct = resp.headers.get("Content-Type", "")
    if "json" not in ct and "javascript" not in ct:
        try:
            text = resp.text.strip()
            if not (text.startswith("[") or text.startswith("{")):
                return False
        except Exception:
            return False
    try:
        data = resp.json()
        if isinstance(data, list):
            return len(data) >= min_items
        if isinstance(data, dict):
            # 可能包在 data / list / result 欄位
            for key in ("data", "list", "result", "rows", "etfList", "stockList"):
                if isinstance(data.get(key), list) and len(data[key]) >= min_items:
                    return True
        return False
    except Exception:
        return False


def discover_endpoints(s: requests.Session, force: bool = False) -> dict:
    """探索各 API endpoint，結果快取到 emega_endpoints.json"""
    if not force and ENDPOINTS_CACHE.exists():
        cached = json.loads(ENDPOINTS_CACHE.read_text())
        log.info(f"✓ 使用快取 endpoint（{ENDPOINTS_CACHE}，使用 --discover --force 強制重新探索）")
        return cached

    found = {}
    log.info("開始探索 emega API endpoints...")

    # ETF 清單 & 股票清單（無需參數）
    for role in ("etf_list", "stock_list"):
        for url in ENDPOINT_CANDIDATES[role]:
            try:
                r = s.get(url, timeout=8)
                log.debug(f"  {url} → {r.status_code}")
                if _is_json_list(r, min_items=10):
                    found[role] = url
                    log.info(f"  ✅ {role}: {url}")
                    break
            except Exception as e:
                log.debug(f"  {url} → error: {e}")
        else:
            log.warning(f"  ✗ {role}: 所有候選都失敗")

    # 需要代號的 endpoint
    for role, probe in (("stock_holdings", PROBE_STOCK), ("etf_weight", PROBE_ETF)):
        for template in ENDPOINT_CANDIDATES[role]:
            url = template.format(code=probe)
            try:
                r = s.get(url, timeout=8)
                log.debug(f"  {url} → {r.status_code}")
                if _is_json_list(r, min_items=1):
                    found[role] = template   # 存 template（含 {code}）
                    log.info(f"  ✅ {role}: {template}")
                    break
            except Exception as e:
                log.debug(f"  {url} → error: {e}")
        else:
            log.warning(f"  ✗ {role}: 所有候選都失敗")

    ENDPOINTS_CACHE.write_text(json.dumps(found, ensure_ascii=False, indent=2))
    log.info(f"探索完成，找到 {len(found)}/4 個 endpoint，快取至 {ENDPOINTS_CACHE}")
    return found


# ─────────────────────────────────────────────
# 2. ETF 清單（補充 emega 的額外欄位）
# ─────────────────────────────────────────────
EMEGA_ETF_MAP = {
    # 常見 emega ETF JSON 欄位名稱 → schema 欄位
    "etfId":              "etf_code",
    "etf_id":             "etf_code",
    "stockId":            "etf_code",
    "etfCode":            "etf_code",
    "etfName":            "etf_name",
    "etf_name":           "etf_name",
    "stockName":          "etf_name",
    "issuer":             "issuer",
    "fundManager":        "issuer",
    "assetMgr":           "issuer",
    "category":           "category",
    "etfCategory":        "category",
    "etfType":            "etf_type",
    "region":             "region",
    "leverageType":       "leverage_type",
    "dividendFreq":       "dividend_type",
    "dividendType":       "dividend_type",
    "trackingIndex":      "tracking_index",
    "indexName":          "tracking_index",
    "expenseRatio":       "expense_ratio",
    "totalExpenseRatio":  "expense_ratio",
    "ter":                "expense_ratio",
    "aumBillion":         "aum_billion",
    "totalNetAsset":      "aum_billion",
    "aum":                "aum_billion",
    "beneficiaryNum":     "beneficiary_count",
    "holderCount":        "beneficiary_count",
    "inceptionDate":      "inception_date",
    "listingDate":        "inception_date",
    "nav":                "nav",
    "closePrice":         "close_price",
    "premiumDiscount":    "premium_discount_pct",
}


def _map_etf_row(raw: dict) -> dict:
    """把 emega JSON 欄位對應到 schema 欄位"""
    out = {}
    for src_key, dst_key in EMEGA_ETF_MAP.items():
        if src_key in raw and dst_key not in out:
            out[dst_key] = raw[src_key]
    return out


def fetch_etf_list(s: requests.Session, endpoints: dict) -> list[dict]:
    url = endpoints.get("etf_list")
    if not url:
        log.warning("etf_list endpoint 未知，跳過")
        return []
    r = s.get(url, timeout=15)
    r.raise_for_status()
    data = r.json()
    if isinstance(data, list):
        return data
    for key in ("data", "list", "result", "etfList"):
        if isinstance(data.get(key), list):
            return data[key]
    return []


def upsert_etf_profiles(conn: sqlite3.Connection, rows: list[dict]):
    today = date.today().isoformat()
    updated = 0
    for raw in rows:
        mapped = _map_etf_row(raw)
        code = mapped.get("etf_code", "").strip()
        if not code:
            continue
        conn.execute("""
            INSERT INTO etf_profile
                (etf_code, etf_name, issuer, category, etf_type, region,
                 leverage_type, dividend_type, tracking_index,
                 expense_ratio, aum_billion, beneficiary_count, inception_date,
                 raw_json, fetched_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(etf_code) DO UPDATE SET
                etf_name        = COALESCE(excluded.etf_name,        etf_profile.etf_name),
                issuer          = COALESCE(excluded.issuer,          etf_profile.issuer),
                category        = COALESCE(excluded.category,        etf_profile.category),
                etf_type        = COALESCE(excluded.etf_type,        etf_profile.etf_type),
                region          = COALESCE(excluded.region,          etf_profile.region),
                leverage_type   = COALESCE(excluded.leverage_type,   etf_profile.leverage_type),
                dividend_type   = COALESCE(excluded.dividend_type,   etf_profile.dividend_type),
                tracking_index  = COALESCE(excluded.tracking_index,  etf_profile.tracking_index),
                expense_ratio   = COALESCE(excluded.expense_ratio,   etf_profile.expense_ratio),
                aum_billion     = COALESCE(excluded.aum_billion,     etf_profile.aum_billion),
                beneficiary_count = COALESCE(excluded.beneficiary_count, etf_profile.beneficiary_count),
                inception_date  = COALESCE(excluded.inception_date,  etf_profile.inception_date),
                raw_json        = excluded.raw_json,
                fetched_at      = excluded.fetched_at
        """, (
            code,
            mapped.get("etf_name"),
            mapped.get("issuer"),
            mapped.get("category"),
            mapped.get("etf_type"),
            mapped.get("region"),
            mapped.get("leverage_type"),
            mapped.get("dividend_type"),
            mapped.get("tracking_index"),
            mapped.get("expense_ratio"),
            mapped.get("aum_billion"),
            mapped.get("beneficiary_count"),
            mapped.get("inception_date"),
            json.dumps(raw, ensure_ascii=False),
            today,
        ))
        updated += 1
    conn.commit()
    log.info(f"✅ etf_profile upsert: {updated} 筆")


# ─────────────────────────────────────────────
# 3. 個股 → ETF 持倉（反查表）
# ─────────────────────────────────────────────
HOLDINGS_MAP = {
    "etfId":           "etf_code",
    "etfCode":         "etf_code",
    "etf_code":        "etf_code",
    "etfName":         "etf_name",
    "etf_name":        "etf_name",
    "holdingWeight":   "holding_weight_pct",
    "weight":          "holding_weight_pct",
    "weightPct":       "holding_weight_pct",
    "holdingWeightPct":"holding_weight_pct",
    "holding_weight":  "holding_weight_pct",
    "shares":          "holding_shares",
    "holdingShares":   "holding_shares",
    "holding_shares":  "holding_shares",
}


def fetch_stock_holdings(
    s: requests.Session,
    endpoints: dict,
    stock_code: str,
) -> list[dict]:
    """抓取個股被哪些 ETF 持有"""
    template = endpoints.get("stock_holdings")
    if not template:
        log.warning("stock_holdings endpoint 未知")
        return []
    url = template.format(code=stock_code)
    try:
        r = s.get(url, timeout=10)
        if r.status_code != 200:
            return []
        data = r.json()
        if isinstance(data, list):
            return data
        for key in ("data", "list", "result", "etfList"):
            if isinstance(data.get(key), list):
                return data[key]
    except Exception as e:
        log.debug(f"  {stock_code} 持倉查詢失敗: {e}")
    return []


def upsert_stock_holdings(
    conn: sqlite3.Connection,
    stock_code: str,
    rows: list[dict],
):
    today = date.today().isoformat()
    inserted = 0
    for raw in rows:
        etf_code = (
            raw.get("etfId") or raw.get("etfCode") or raw.get("etf_code", "")
        ).strip()
        if not etf_code:
            continue
        weight = None
        for k in ("holdingWeight", "weight", "weightPct", "holdingWeightPct", "holding_weight"):
            if k in raw:
                try:
                    weight = float(str(raw[k]).replace("%", "").replace(",", ""))
                except (ValueError, TypeError):
                    pass
                break
        shares = None
        for k in ("shares", "holdingShares", "holding_shares"):
            if k in raw:
                try:
                    shares = int(str(raw[k]).replace(",", ""))
                except (ValueError, TypeError):
                    pass
                break
        etf_name = raw.get("etfName") or raw.get("etf_name") or ""
        conn.execute("""
            INSERT INTO stock_etf_holdings
                (stock_code, etf_code, etf_name, holding_weight_pct, holding_shares, snapshot_date)
            VALUES (?,?,?,?,?,?)
            ON CONFLICT(stock_code, etf_code, snapshot_date) DO UPDATE SET
                etf_name          = excluded.etf_name,
                holding_weight_pct= excluded.holding_weight_pct,
                holding_shares    = excluded.holding_shares
        """, (stock_code, etf_code, etf_name, weight, shares, today))
        inserted += 1
    conn.commit()
    return inserted


def fetch_holdings_all(
    s: requests.Session,
    endpoints: dict,
    conn: sqlite3.Connection,
    delay: float = 0.4,
):
    """批次抓全部已知個股的 ETF 持倉"""
    # 從 DB 讀已知股票清單
    stock_codes = [
        r[0] for r in conn.execute(
            "SELECT DISTINCT stock_code FROM stock_master ORDER BY stock_code"
        ).fetchall()
    ]
    if not stock_codes:
        # 若 stock_master 是空的，用已有的持倉資料裡的股票代號
        stock_codes = [
            r[0] for r in conn.execute(
                "SELECT DISTINCT stock_code FROM stock_etf_holdings ORDER BY stock_code"
            ).fetchall()
        ]
    if not stock_codes:
        log.warning(
            "stock_master 是空的，請先跑 twse_loader.py 或用 --stock-list 抓股票清單"
        )
        return

    # 讀取進度 checkpoint
    ckpt_path = Path(__file__).parent / "emega_holdings_checkpoint.json"
    done: set = set()
    if ckpt_path.exists():
        done = set(json.loads(ckpt_path.read_text()).get("done", []))
        log.info(f"  ← 從 checkpoint 繼續（已完成 {len(done)} / {len(stock_codes)}）")

    total_inserted = 0
    for i, code in enumerate(stock_codes):
        if code in done:
            continue
        rows = fetch_stock_holdings(s, endpoints, code)
        n = upsert_stock_holdings(conn, code, rows)
        total_inserted += n
        done.add(code)

        if (i + 1) % 20 == 0:
            ckpt_path.write_text(json.dumps({"done": list(done)}))
            pct = len(done) / len(stock_codes) * 100
            log.info(f"  進度 {len(done)}/{len(stock_codes)} ({pct:.0f}%)，已插入 {total_inserted} 筆持倉")

        time.sleep(delay)

    ckpt_path.write_text(json.dumps({"done": list(done)}))
    log.info(f"✅ 持倉批次完成：{total_inserted} 筆寫入 stock_etf_holdings")


# ─────────────────────────────────────────────
# 4. ETF → 成分股權重
# ─────────────────────────────────────────────
ETF_WEIGHT_MAP = {
    "stockId":    "stock_code",
    "stockCode":  "stock_code",
    "stock_code": "stock_code",
    "stockName":  "stock_name",
    "weight":     "weight_pct",
    "weightPct":  "weight_pct",
    "holdingWeight": "weight_pct",
    "shares":     "shares",
    "holdingShares": "shares",
    "marketValue": "market_value",
    "mktVal":      "market_value",
}


def fetch_etf_weight(
    s: requests.Session,
    endpoints: dict,
    etf_code: str,
) -> list[dict]:
    template = endpoints.get("etf_weight")
    if not template:
        log.warning("etf_weight endpoint 未知")
        return []
    url = template.format(code=etf_code)
    try:
        r = s.get(url, timeout=10)
        if r.status_code != 200:
            return []
        data = r.json()
        if isinstance(data, list):
            return data
        for key in ("data", "list", "result", "holdings", "stockList"):
            if isinstance(data.get(key), list):
                return data[key]
    except Exception as e:
        log.debug(f"  {etf_code} 權重查詢失敗: {e}")
    return []


def upsert_etf_holdings(
    conn: sqlite3.Connection,
    etf_code: str,
    rows: list[dict],
):
    today = date.today().isoformat()
    inserted = 0
    for raw in rows:
        stock_code = (
            raw.get("stockId") or raw.get("stockCode") or raw.get("stock_code", "")
        ).strip()
        if not stock_code:
            continue
        weight = None
        for k in ("weight", "weightPct", "holdingWeight", "weight_pct"):
            if k in raw:
                try:
                    weight = float(str(raw[k]).replace("%", "").replace(",", ""))
                except (ValueError, TypeError):
                    pass
                break
        shares = None
        for k in ("shares", "holdingShares"):
            if k in raw:
                try:
                    shares = int(str(raw[k]).replace(",", ""))
                except (ValueError, TypeError):
                    pass
                break
        mkt_val = None
        for k in ("marketValue", "mktVal", "market_value"):
            if k in raw:
                try:
                    mkt_val = float(str(raw[k]).replace(",", ""))
                except (ValueError, TypeError):
                    pass
                break
        stock_name = raw.get("stockName") or raw.get("stock_name") or ""

        conn.execute("""
            INSERT INTO etf_holdings
                (etf_code, stock_code, stock_name, weight_pct, shares, market_value, snapshot_date)
            VALUES (?,?,?,?,?,?,?)
            ON CONFLICT(etf_code, stock_code, snapshot_date) DO UPDATE SET
                stock_name  = excluded.stock_name,
                weight_pct  = excluded.weight_pct,
                shares      = excluded.shares,
                market_value= excluded.market_value
        """, (etf_code, stock_code, stock_name, weight, shares, mkt_val, today))

        # 同時寫歷史權重
        conn.execute("""
            INSERT INTO etf_stock_weight_history (etf_code, stock_code, record_date, weight_pct)
            VALUES (?,?,?,?)
            ON CONFLICT(etf_code, stock_code, record_date) DO UPDATE SET
                weight_pct = excluded.weight_pct
        """, (etf_code, stock_code, today, weight))

        inserted += 1
    conn.commit()
    return inserted


def fetch_etf_weight_all(
    s: requests.Session,
    endpoints: dict,
    conn: sqlite3.Connection,
    delay: float = 0.4,
):
    etf_codes = [
        r[0] for r in conn.execute(
            "SELECT etf_code FROM etf_profile ORDER BY etf_code"
        ).fetchall()
    ]
    ckpt_path = Path(__file__).parent / "emega_weight_checkpoint.json"
    done: set = set()
    if ckpt_path.exists():
        done = set(json.loads(ckpt_path.read_text()).get("done", []))
        log.info(f"  ← 從 checkpoint 繼續（已完成 {len(done)} / {len(etf_codes)}）")

    total = 0
    for i, code in enumerate(etf_codes):
        if code in done:
            continue
        rows = fetch_etf_weight(s, endpoints, code)
        n = upsert_etf_holdings(conn, code, rows)
        total += n
        done.add(code)
        if (i + 1) % 20 == 0:
            ckpt_path.write_text(json.dumps({"done": list(done)}))
            log.info(f"  進度 {len(done)}/{len(etf_codes)}，已插入 {total} 筆")
        time.sleep(delay)

    ckpt_path.write_text(json.dumps({"done": list(done)}))
    log.info(f"✅ ETF 成分股批次完成：{total} 筆寫入 etf_holdings")


# ─────────────────────────────────────────────
# 主程式
# ─────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser(description="emega ETFMaster 持倉抓取器")
    ap.add_argument("--discover",       action="store_true", help="探索 API endpoints")
    ap.add_argument("--force",          action="store_true", help="強制重新探索（忽略快取）")
    ap.add_argument("--etf-list",       action="store_true", help="抓 emega ETF 清單（補充欄位）")
    ap.add_argument("--holdings",       metavar="CODE",      help="抓單支股票的 ETF 持倉（例：2330）")
    ap.add_argument("--holdings-all",   action="store_true", help="批次抓全部個股持倉")
    ap.add_argument("--etf-weight",     metavar="CODE",      help="抓單一 ETF 成分股權重（例：0050）")
    ap.add_argument("--etf-weight-all", action="store_true", help="批次抓全部 ETF 成分股")
    ap.add_argument("--delay",          type=float, default=0.4, help="請求間隔秒數（預設 0.4）")
    ap.add_argument("--check-cookie",   action="store_true", help="只檢查 cookie 是否有效")
    args = ap.parse_args()

    s    = make_session()
    conn = get_conn()

    # 只檢查 cookie
    if args.check_cookie:
        log.info("測試 cookie 有效性...")
        try:
            r = s.get(f"{BASE}/index.do", timeout=10)
            if r.status_code == 200 and "etf" in r.text.lower():
                log.info("✅ cookie 有效，頁面正常載入")
            else:
                log.warning(f"⚠ 頁面回應異常（status {r.status_code}），可能 cookie 已過期")
        except Exception as e:
            log.error(f"✗ 連線失敗: {e}")
        return

    # 探索 endpoints
    eps = discover_endpoints(s, force=args.force)

    if not eps and not (args.discover):
        log.error("無法找到任何 endpoint，請確認 cookie 是否有效")
        log.error(f"執行 python etf_db/emega_holdings.py --check-cookie 檢查")
        return

    if args.discover:
        print("\n找到的 Endpoints：")
        for k, v in eps.items():
            print(f"  {k:20s}: {v}")
        return

    # 執行各任務
    if args.etf_list:
        log.info("抓取 emega ETF 清單...")
        rows = fetch_etf_list(s, eps)
        log.info(f"  取得 {len(rows)} 筆")
        upsert_etf_profiles(conn, rows)

    if args.holdings:
        log.info(f"抓取 {args.holdings} 的 ETF 持倉...")
        rows = fetch_stock_holdings(s, eps, args.holdings)
        n = upsert_stock_holdings(conn, args.holdings, rows)
        log.info(f"✅ {args.holdings} → {len(rows)} 筆，寫入 {n} 筆")
        # 印出結果
        cur = conn.execute("""
            SELECT etf_code, etf_name, holding_weight_pct, snapshot_date
            FROM stock_etf_holdings
            WHERE stock_code = ?
            ORDER BY holding_weight_pct DESC NULLS LAST
            LIMIT 30
        """, (args.holdings,))
        print(f"\n{args.holdings} 被以下 ETF 持有：")
        print(f"{'ETF代號':8} {'ETF名稱':24} {'持股比例':>8}  {'資料日期'}")
        print("-" * 58)
        for row in cur.fetchall():
            w = f"{row['holding_weight_pct']:.2f}%" if row['holding_weight_pct'] else "—"
            print(f"{row['etf_code']:8} {(row['etf_name'] or ''):24} {w:>8}  {row['snapshot_date']}")

    if args.holdings_all:
        log.info("批次抓取全部個股持倉（可隨時 Ctrl+C 中斷，下次從斷點繼續）")
        fetch_holdings_all(s, eps, conn, delay=args.delay)

    if args.etf_weight:
        log.info(f"抓取 {args.etf_weight} 的成分股...")
        rows = fetch_etf_weight(s, eps, args.etf_weight)
        n = upsert_etf_holdings(conn, args.etf_weight, rows)
        log.info(f"✅ {args.etf_weight} → {len(rows)} 筆，寫入 {n} 筆")
        cur = conn.execute("""
            SELECT stock_code, stock_name, weight_pct
            FROM etf_holdings
            WHERE etf_code = ?
            ORDER BY weight_pct DESC NULLS LAST
            LIMIT 30
        """, (args.etf_weight,))
        print(f"\n{args.etf_weight} 持倉成分股（前 30）：")
        print(f"{'代號':8} {'名稱':18} {'權重':>8}")
        print("-" * 38)
        for row in cur.fetchall():
            w = f"{row['weight_pct']:.2f}%" if row['weight_pct'] else "—"
            print(f"{row['stock_code']:8} {(row['stock_name'] or ''):18} {w:>8}")

    if args.etf_weight_all:
        log.info("批次抓取全部 ETF 成分股（可隨時 Ctrl+C 中斷，下次從斷點繼續）")
        fetch_etf_weight_all(s, eps, conn, delay=args.delay)

    conn.close()


if __name__ == "__main__":
    main()
