"""
台股 ETF 資料載入器 — 使用 TWSE / TPEX 公開 API（不需要認證）

資料來源：
  1. isin.twse.com.tw            → TWSE 上市 ETF 代號、ISIN、掛牌日、CFI 分類
  2. TWSE TWT38U                 → 中文名稱
  3. TWSE STOCK_DAY              → 最新收盤價、成交量（TWSE 上市 ETF）
  4. TPEX stk_wn1430_result.php  → TPEX 上櫃 ETF 代號、名稱、收盤價（含債券/商品 ETF）

用法：
  python etf_db/twse_loader.py                # 只載入 ETF 基本資料 + TPEX 行情
  python etf_db/twse_loader.py --price        # 同時抓 TWSE 上市 ETF 逐檔行情（~3 分鐘）
  python etf_db/twse_loader.py --clean-stocks # 清除誤入的非 ETF 代號（不以 00 開頭）

━━━ 證交所 ETF 證券代號編碼原則（官方）━━━━━━━━━━━━━━━━━━━━
所有台灣 ETF 代號一律以「00」開頭（4~6 碼），尾碼字母標示類型：
  (無)=一般股票型(外幣K)  L=槓桿(外幣M)  R=反向(外幣S)
  B=債券(外幣C)  U=期貨/商品(外幣V)  A=主動股票  D=主動債券  T=股債平衡
範例：0050 006208 00631L 00632R 00679B 00713 00878 00980A
注意：6xxx 是普通股票、02xxxx 是 ETN，皆非 ETF。過濾務必用 "00" 而非 "0"。
參考：https://www.twse.com.tw/downloads/zh/ETF/ETFcode.pdf
"""

import argparse
import json
import logging
import os
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DB_PATH = Path(os.environ.get("ETF_DB_PATH", Path.home() / "Desktop" / "etf_master.db"))
SCHEMA_PATH = Path(__file__).parent / "schema.sql"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/html, */*",
    "Accept-Language": "zh-TW,zh;q=0.9",
}

# CFI 前綴 → 分類標籤
CFI_CATEGORY = {
    "CEOGEU": "股票ETF",
    "CEOGDU": "槓反ETF",
    "CEOILU": "主動ETF",
    "CEOJEU": "主動ETF",
    "CEOIEU": "主動ETF",
    "CEOIBU": "債券ETF",
    "CEOIGU": "債券ETF",
    "CEOGFU": "商品ETF",
    "CEOGCU": "商品ETF",
    "CEOGJU": "REITs",
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("twse_loader")


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def get_conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    # Ensure schema exists
    if SCHEMA_PATH.exists():
        conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        conn.commit()
    return conn


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def today_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# Step 1: ISIN page → ETF codes + metadata
# ---------------------------------------------------------------------------

def fetch_isin_etfs(session: requests.Session) -> list[dict]:
    """
    從 isin.twse.com.tw (strMode=2) 抓取所有 TWSE 上市 ETF。
    過濾條件：CFI code 以 CE 開頭（Collective investment vehicles, Exchange-traded）。
    """
    log.info("Step 1: 抓取 ISIN 頁面 (TWSE strMode=2)...")
    url = "https://isin.twse.com.tw/isin/e_C_public.jsp"
    resp = session.get(url, params={"strMode": "2"}, timeout=40)
    resp.encoding = "big5"
    soup = BeautifulSoup(resp.text, "lxml")
    table = soup.find("table", {"class": "h4"})
    if not table:
        log.warning("找不到 ISIN 頁面 table")
        return []

    etfs = []
    for tr in table.find_all("tr")[1:]:
        tds = [td.get_text(strip=True) for td in tr.find_all("td")]
        if len(tds) < 7:
            continue
        code_name = tds[0]
        isin_code = tds[1]
        listed_date = tds[2]
        cfi = tds[5]

        if not cfi.startswith("CE"):
            continue
        if "　" not in code_name:
            continue

        parts = code_name.split("　", 1)
        code = parts[0].strip()
        en_name = parts[1].strip() if len(parts) > 1 else ""

        # 官方編碼原則：台灣 ETF 代號一律以「00」開頭
        if not code.startswith("00") or len(code) > 7:
            continue

        # 推斷分類
        cfi6 = cfi[:6]
        category = CFI_CATEGORY.get(cfi6, "ETF")

        # 槓反判斷
        leverage_type = None
        if "DU" in cfi:
            leverage_type = "槓桿/反向"

        # 日期格式化 2003/06/30 → 2003-06-30
        inception = None
        if listed_date and "/" in listed_date:
            try:
                inception = listed_date.replace("/", "-")
            except Exception:
                pass

        etfs.append({
            "etf_code": code,
            "en_name": en_name,
            "isin": isin_code,
            "inception_date": inception,
            "cfi": cfi,
            "category": category,
            "leverage_type": leverage_type,
            "currency": "TWD",
        })

    log.info(f"  → ISIN 頁面找到 {len(etfs)} 檔 ETF (CE 開頭)")
    return etfs


# ---------------------------------------------------------------------------
# Step 2: TWT38U → 中文名稱 map
# ---------------------------------------------------------------------------

def fetch_chinese_names(session: requests.Session) -> dict[str, str]:
    """
    從 TWSE TWT38U 取得所有證券代號的中文名稱。
    回傳 {code: name} 字典。
    """
    log.info("Step 2: 抓取 TWT38U 中文名稱...")
    url = "https://www.twse.com.tw/fund/TWT38U"
    resp = session.get(url, params={"response": "json", "_": int(time.time() * 1000)}, timeout=20)
    data = resp.json()
    name_map: dict[str, str] = {}
    for row in data.get("data", []):
        if len(row) >= 3:
            code = str(row[1]).strip()
            name = str(row[2]).strip()
            if code and name:
                name_map[code] = name
    log.info(f"  → 取得 {len(name_map)} 筆中文名稱")
    return name_map


# ---------------------------------------------------------------------------
# Step 3: STOCK_DAY → 最新收盤價
# ---------------------------------------------------------------------------

def fetch_latest_price(session: requests.Session, etf_code: str) -> Optional[dict]:
    """
    從 TWSE STOCK_DAY 取得指定 ETF 本月最新收盤價。
    回傳 {'close': float, 'volume_k': int} 或 None。
    """
    today = datetime.now()
    date_str = today.strftime("%Y%m01")  # 本月第一天
    url = "https://www.twse.com.tw/exchangeReport/STOCK_DAY"
    try:
        resp = session.get(
            url,
            params={"response": "json", "stockNo": etf_code, "date": date_str, "_": int(time.time() * 1000)},
            timeout=10,
        )
        if resp.status_code != 200:
            return None
        data = resp.json()
        rows = data.get("data", [])
        if not rows:
            return None
        last = rows[-1]  # 最新一筆
        # fields: 日期, 成交股數, 成交金額, 開盤價, 最高價, 最低價, 收盤價, 漲跌價差, 成交筆數
        close_str = last[6].replace(",", "") if len(last) > 6 else ""
        vol_str = last[1].replace(",", "") if len(last) > 1 else ""
        close = float(close_str) if close_str and close_str not in ("--", "") else None
        vol = int(int(vol_str) / 1000) if vol_str and vol_str not in ("--", "") else None  # 股→張(千股)
        return {"close": close, "volume_k": vol}
    except Exception as exc:
        log.debug(f"  {etf_code} 行情失敗: {exc}")
        return None


# ---------------------------------------------------------------------------
# Step 3b: TPEX → OTC ETF list + prices (bond ETFs, commodity ETFs, etc.)
# ---------------------------------------------------------------------------

def _guess_category_from_code_name(code: str, name: str) -> str:
    if code.endswith("B") or "債" in name:
        return "債券ETF"
    if code.endswith("U") or any(k in name for k in ("黃金", "石油", "原油")):
        return "商品ETF"
    if code[-1] in ("L",) or any(k in name for k in ("正2", "2X", "Bull")):
        return "槓反ETF"
    if code[-1] in ("R",) or any(k in name for k in ("反1", "Bear", "BEAR")):
        return "槓反ETF"
    return "ETF"


def fetch_tpex_etfs(session: requests.Session) -> tuple[list[dict], list[dict]]:
    """
    從 TPEX stk_wn1430 API 抓取 OTC 上市 ETF 清單與行情。
    回傳 (profiles, snapshots)。
    """
    log.info("Step 3b: 抓取 TPEX 上櫃 ETF 清單與行情...")
    url = "https://www.tpex.org.tw/web/stock/aftertrading/otc_quotes_no1430/stk_wn1430_result.php"
    try:
        resp = session.get(url, params={"l": "zh-tw", "se": "EW"}, timeout=20)
        resp.raise_for_status()
        d = resp.json()
    except Exception as exc:
        log.warning(f"TPEX API 失敗: {exc}")
        return [], []

    t = d["tables"][0]
    raw_date = d.get("date", "")
    if len(raw_date) == 8:
        snap_date = f"{raw_date[:4]}-{raw_date[4:6]}-{raw_date[6:]}"
    else:
        snap_date = today_iso()

    profiles: list[dict] = []
    snapshots: list[dict] = []

    for row in t.get("data", []):
        if len(row) < 3:
            continue
        code = str(row[0]).strip()
        name = str(row[1]).strip()
        # 官方編碼原則：台灣 ETF 代號一律以「00」開頭（0050/006208/00631L/00679B/00980A…）
        # 注意：6xxx=普通股票、02xxxx=ETN，皆非 ETF，必須用 "00" 而非 "0" 過濾
        if not code.startswith("00") or len(code) > 7:
            continue

        close_str = str(row[2]).replace(",", "").strip() if len(row) > 2 else ""
        vol_str = str(row[7]).replace(",", "").strip() if len(row) > 7 else ""

        try:
            close = float(close_str) if close_str not in ("--", "", "N/A") else None
        except ValueError:
            close = None
        try:
            vol_k = int(int(vol_str) / 1000) if vol_str not in ("--", "", "N/A") else None
        except (ValueError, ZeroDivisionError):
            vol_k = None

        profiles.append({
            "etf_code": code,
            "etf_name": name,
            "category": _guess_category_from_code_name(code, name),
            "currency": "TWD",
            "en_name": name,
        })
        if close is not None:
            snapshots.append({"etf_code": code, "close": close, "volume_k": vol_k, "snap_date": snap_date})

    log.info(f"  → TPEX: {len(profiles)} 檔 ETF，{len(snapshots)} 筆行情 (日期: {snap_date})")
    return profiles, snapshots


# ---------------------------------------------------------------------------
# Step 4: upsert into DB
# ---------------------------------------------------------------------------

def upsert_etfs(conn: sqlite3.Connection, etfs: list[dict]) -> int:
    now = now_iso()
    n = 0
    for e in etfs:
        conn.execute(
            """
            INSERT INTO etf_profile
                (etf_code, etf_name, category, leverage_type, currency,
                 inception_date, raw_json, fetched_at)
            VALUES
                (:etf_code, :etf_name, :category, :leverage_type, :currency,
                 :inception_date, :raw_json, :fetched_at)
            ON CONFLICT(etf_code) DO UPDATE SET
                etf_name       = excluded.etf_name,
                category       = excluded.category,
                leverage_type  = excluded.leverage_type,
                currency       = excluded.currency,
                inception_date = excluded.inception_date,
                raw_json       = excluded.raw_json,
                fetched_at     = excluded.fetched_at
            """,
            {
                "etf_code": e["etf_code"],
                "etf_name": e.get("etf_name") or e.get("en_name", ""),
                "category": e.get("category"),
                "leverage_type": e.get("leverage_type"),
                "currency": e.get("currency", "TWD"),
                "inception_date": e.get("inception_date"),
                "raw_json": json.dumps(e, ensure_ascii=False),
                "fetched_at": now,
            },
        )
        n += 1
    conn.commit()
    return n


def upsert_snapshots(conn: sqlite3.Connection, snapshots: list[dict], default_date: Optional[str] = None) -> int:
    fallback = default_date or today_iso()
    n = 0
    for s in snapshots:
        conn.execute(
            """
            INSERT INTO etf_market_snapshot
                (etf_code, snapshot_date, close_price, volume_k)
            VALUES
                (:etf_code, :snapshot_date, :close_price, :volume_k)
            ON CONFLICT(etf_code, snapshot_date) DO UPDATE SET
                close_price = excluded.close_price,
                volume_k    = excluded.volume_k
            """,
            {
                "etf_code": s["etf_code"],
                "snapshot_date": s.get("snap_date", fallback),
                "close_price": s.get("close"),
                "volume_k": s.get("volume_k"),
            },
        )
        n += 1
    conn.commit()
    return n


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="從 TWSE 公開 API 載入台股 ETF 資料")
    parser.add_argument("--price", action="store_true", help="同時抓每檔最新收盤價（較慢）")
    parser.add_argument("--delay", type=float, default=0.4, help="行情請求間隔秒數 (default: 0.4)")
    parser.add_argument("--db", help="DB 路徑（覆蓋預設）")
    parser.add_argument("--clean-stocks", action="store_true",
                        help="清除 etf_profile 中代號不以 00 開頭的非 ETF 記錄（修復誤入的股票）")
    args = parser.parse_args()

    if args.db:
        global DB_PATH
        DB_PATH = Path(args.db)

    # ── 清除誤入的股票代號 ──────────────────────────────────────
    if args.clean_stocks:
        if not DB_PATH.exists():
            print(f"找不到 DB：{DB_PATH}")
            return
        conn = get_conn()
        cur = conn.execute(
            "SELECT COUNT(*) FROM etf_profile WHERE etf_code NOT LIKE '00%'"
        )
        n_bad = cur.fetchone()[0]
        if n_bad == 0:
            print("✅ etf_profile 中沒有非 ETF 代號，無需清理")
        else:
            print(f"→ 刪除 {n_bad} 筆非 ETF 代號（官方規則：ETF 一律以 00 開頭）...")
            conn.execute("DELETE FROM etf_profile WHERE etf_code NOT LIKE '00%'")
            # 同步清掉對應的行情快照
            conn.execute("DELETE FROM etf_market_snapshot WHERE etf_code NOT LIKE '00%'")
            conn.commit()
            remaining = conn.execute("SELECT COUNT(*) FROM etf_profile").fetchone()[0]
            print(f"✅ 清理完成，etf_profile 剩 {remaining} 筆（純 ETF）")
        conn.close()
        return

    print("=" * 55)
    print("  台股 ETF TWSE 公開資料載入器")
    print("=" * 55)

    session = requests.Session()
    session.headers.update(HEADERS)

    conn = get_conn()
    log.info(f"DB: {DB_PATH}")

    # Step 1: ISIN
    etfs = fetch_isin_etfs(session)
    if not etfs:
        print("✗ 無法取得 ISIN 資料，結束。")
        return

    # Step 2: Chinese names
    time.sleep(0.5)
    name_map = fetch_chinese_names(session)

    # Merge
    for e in etfs:
        zh_name = name_map.get(e["etf_code"])
        if zh_name:
            e["etf_name"] = zh_name

    # Upsert TWSE profiles
    n = upsert_etfs(conn, etfs)
    print(f"\n✅ 寫入 etf_profile (TWSE): {n} 筆")

    # Step 3b: TPEX
    time.sleep(0.5)
    tpex_profiles, tpex_snapshots = fetch_tpex_etfs(session)
    if tpex_profiles:
        n_tpex = upsert_etfs(conn, tpex_profiles)
        print(f"✅ 寫入 etf_profile (TPEX): {n_tpex} 筆")
    if tpex_snapshots:
        n_tpex_snap = upsert_snapshots(conn, tpex_snapshots)
        print(f"✅ 寫入 etf_market_snapshot (TPEX): {n_tpex_snap} 筆")

    # Step 4 (optional): TWSE per-ETF price
    if args.price:
        print(f"\n→ 開始抓行情（共 {len(etfs)} 檔，間隔 {args.delay}s）...")
        snapshots = []
        for i, e in enumerate(etfs):
            code = e["etf_code"]
            price = fetch_latest_price(session, code)
            if price:
                snapshots.append({"etf_code": code, **price})
                status = f"close={price['close']}"
            else:
                status = "skip"
            if (i + 1) % 20 == 0 or i == len(etfs) - 1:
                print(f"  [{i+1}/{len(etfs)}] {code} {status}")
            else:
                print(f"  [{i+1}/{len(etfs)}] {code} {status}", end="\r", flush=True)
            time.sleep(args.delay)

        n2 = upsert_snapshots(conn, snapshots)
        print(f"\n✅ 寫入 etf_market_snapshot: {n2} 筆")

    # Stats
    print()
    for table in ["etf_profile", "etf_market_snapshot", "stock_master",
                  "stock_etf_holdings", "etf_holdings"]:
        cnt = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        print(f"  {table:<30} {cnt:>6,} 筆")

    print("\n✅ 完成！可用指令：")
    print("  source etf_db/.venv/bin/activate")
    print("  python3 etf_db/query.py stats")
    print("  python3 etf_db/query.py search 高股息")
    print("  python3 etf_db/query.py etf 0050")
    conn.close()


if __name__ == "__main__":
    main()
