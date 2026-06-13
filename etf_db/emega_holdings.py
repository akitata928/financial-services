"""
emega ETFMaster 持倉抓取器 — 正式版（已知 API endpoint）

emega 使用 Spring + CSRF 保護。所有 API 都是 POST + JSON body，
回應格式為 {code:'1', data:'<JSON字串>'}（data 需要再 parse 一次）。

已知 API（從 stockRefEtf/index.js 解析）：
  POST /etfmaster/api/stocks         → 全部股票清單（autocomplete）
  POST /etfmaster/api/dataset        → 個股行情（body: JSON 陣列 stockIds）
  POST /etfmaster/api/stocksEtfShare → 個股反查 ETF（body: JSON 陣列 stockIds）

━━━ 一次性步驟：取得 Cookie ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
1. Chrome 開 https://www.emega.com.tw/etfmaster/stockRefEtf/index.do
2. F12 → Network → Command+R 重新整理
3. 點 index.do → Headers → Request Headers → 複製 cookie: 的值
4. cat > ~/.etf_session_cookie << 'END'
   貼上cookie
   END

━━━ 使用 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  python etf_db/emega_holdings.py --check-cookie     # 驗證 cookie + CSRF
  python etf_db/emega_holdings.py --probe 2330       # 印出原始 JSON（確認欄位）
  python etf_db/emega_holdings.py --stock-list       # 抓全部股票清單入庫
  python etf_db/emega_holdings.py --holdings 2330    # 抓單股反查 ETF
  python etf_db/emega_holdings.py --holdings-all     # 批次抓全部個股（控速）
  python etf_db/emega_holdings.py --dump-js --context 18   # 除錯：看 JS ajax 區塊
"""

import argparse
import json
import logging
import os
import re
import sqlite3
import time
from datetime import date
from pathlib import Path
from typing import Optional, List

import requests
from bs4 import BeautifulSoup

# ─────────────────────────────────────────────
# 設定
# ─────────────────────────────────────────────
DB_PATH = Path(os.environ.get("ETF_DB_PATH", Path.home() / "Desktop" / "etf_master.db"))
COOKIE_PATH = Path(os.environ.get("EMEGA_COOKIE_PATH", Path.home() / ".etf_session_cookie"))

BASE = "https://www.emega.com.tw/etfmaster"

# 已知 API endpoint（POST + JSON）
API_STOCKS          = f"{BASE}/api/stocks"
API_DATASET         = f"{BASE}/api/dataset"
API_STOCKS_ETFSHARE = f"{BASE}/api/stocksEtfShare"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("emega")


# ─────────────────────────────────────────────
# Session + CSRF
# ─────────────────────────────────────────────
def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8",
        "Referer": f"{BASE}/stockRefEtf/index.do",
        "Origin": "https://www.emega.com.tw",
        "X-Requested-With": "XMLHttpRequest",
    })
    if COOKIE_PATH.exists():
        raw = COOKIE_PATH.read_text(encoding="utf-8").strip()
        if raw:
            s.headers["Cookie"] = raw
            log.info(f"✓ 已載入 cookie（{len(raw)} 字元）")
        else:
            log.warning("⚠ cookie 檔案是空的")
    else:
        log.warning(f"⚠ 找不到 cookie 檔 {COOKIE_PATH}（見檔頭說明取得）")
    return s


def fetch_csrf_token(s: requests.Session) -> Optional[str]:
    """從頁面 <meta name='_csrf'> 取得 token，設定到正確的 CSRF header。"""
    for page in (f"{BASE}/stockRefEtf/index.do", f"{BASE}/index.do"):
        try:
            r = s.get(page, timeout=10)
            if r.status_code != 200:
                continue
            soup = BeautifulSoup(r.text, "lxml")
            token_meta = soup.find("meta", {"name": "_csrf"})
            header_meta = soup.find("meta", {"name": "_csrf_header"})
            if token_meta and token_meta.get("content"):
                token = token_meta["content"]
                header_name = (header_meta.get("content") if header_meta else None) or "X-XSRF-TOKEN"
                s.headers[header_name] = token
                # 同時設常見的兩種，保險
                s.headers["X-CSRF-TOKEN"] = token
                log.info(f"✓ CSRF token 取得成功（header={header_name}, {token[:16]}...）")
                return token
        except Exception as e:
            log.debug(f"CSRF 取得失敗 ({page}): {e}")
    log.warning("⚠ 無法取得 CSRF token")
    return None


# ─────────────────────────────────────────────
# POST 呼叫 + 雙層解析
# ─────────────────────────────────────────────
def emega_post(s: requests.Session, url: str, payload=None) -> Optional[list]:
    """
    POST 到 emega API，處理 {code:'1', data:'<JSON字串>'} 雙層編碼。
    回傳已 parse 的 list/dict，失敗回傳 None。
    """
    try:
        if payload is None:
            r = s.post(url, timeout=15)
        else:
            r = s.post(
                url,
                data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                timeout=15,
            )
        if r.status_code != 200:
            log.debug(f"  {url} → HTTP {r.status_code}")
            return None
        outer = r.json()
        code = str(outer.get("code", ""))
        if code != "1":
            log.debug(f"  {url} → code={code}, msg={outer.get('msg')}")
            return None
        data = outer.get("data")
        # data 可能是 JSON 字串（雙層編碼），也可能已是物件
        if isinstance(data, str):
            try:
                data = json.loads(data)
            except json.JSONDecodeError:
                pass
        return data
    except Exception as e:
        log.debug(f"  POST {url} 失敗: {e}")
        return None


# ─────────────────────────────────────────────
# DB
# ─────────────────────────────────────────────
def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


# ─────────────────────────────────────────────
# 中文欄位輔助
# ─────────────────────────────────────────────
def _get(d: dict, *keys, default=None):
    """從 dict 取值，支援多個候選 key（含中英文）"""
    for k in keys:
        if k in d and d[k] not in (None, "", "-"):
            return d[k]
    return default


def _to_float(v):
    if v is None:
        return None
    try:
        return float(str(v).replace("%", "").replace(",", "").strip())
    except (ValueError, TypeError):
        return None


def _to_int(v):
    if v is None:
        return None
    try:
        return int(float(str(v).replace(",", "").strip()))
    except (ValueError, TypeError):
        return None


# ─────────────────────────────────────────────
# 1. 股票清單（POST api/stocks）
# ─────────────────────────────────────────────
def fetch_stock_list(s: requests.Session) -> List[dict]:
    data = emega_post(s, API_STOCKS)
    if not data:
        return []
    if isinstance(data, dict):
        for k in ("list", "data", "stocks", "rows"):
            if isinstance(data.get(k), list):
                data = data[k]
                break
    return data if isinstance(data, list) else []


def _extract_code_name(raw):
    """從一筆股票資料抽取 (代號, 名稱)，支援 dict 或 list 格式"""
    if isinstance(raw, dict):
        code = _get(raw, "股票代號", "stockId", "stockCode", "code", "id", "value")
        name = _get(raw, "股票名稱", "stockName", "name", "label", "text", default="")
        return code, name
    if isinstance(raw, (list, tuple)) and raw:
        # 常見格式 [代號, 名稱] 或 [代號, 名稱, ...]
        code = raw[0]
        name = raw[1] if len(raw) > 1 else ""
        return code, name
    if isinstance(raw, str):
        # 可能是 "2330 台積電" 或 "2330,台積電"
        parts = re.split(r"[\s,]+", raw.strip(), maxsplit=1)
        return (parts[0], parts[1] if len(parts) > 1 else "")
    return None, None


def upsert_stock_master(conn: sqlite3.Connection, rows: List[dict]) -> int:
    today = date.today().isoformat()
    n = 0
    for raw in rows:
        code, name = _extract_code_name(raw)
        if not code:
            continue
        code = str(code).strip()
        # 過濾非股票代號（保留 4-6 位數字開頭）
        if not re.match(r"^\d{4,6}", code):
            continue
        conn.execute("""
            INSERT INTO stock_master (stock_code, stock_name, updated_at)
            VALUES (?,?,?)
            ON CONFLICT(stock_code) DO UPDATE SET
                stock_name = COALESCE(excluded.stock_name, stock_master.stock_name),
                updated_at = excluded.updated_at
        """, (code, str(name).strip(), today))
        n += 1
    conn.commit()
    return n


# ─────────────────────────────────────────────
# 2. 個股反查 ETF（POST api/stocksEtfShare, body=[code,...]）
# ─────────────────────────────────────────────
def fetch_stock_holdings(s: requests.Session, stock_codes: List[str]) -> List[dict]:
    """送一批股票代號，回傳這些股票被哪些 ETF 持有的資料"""
    data = emega_post(s, API_STOCKS_ETFSHARE, payload=stock_codes)
    if not data:
        return []
    if isinstance(data, dict):
        for k in ("list", "data", "rows"):
            if isinstance(data.get(k), list):
                data = data[k]
                break
    return data if isinstance(data, list) else []


def _ensure_holdings_columns(conn: sqlite3.Connection):
    """確保 stock_etf_holdings 有 ytd_return 欄位（舊 DB 自動補上）"""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(stock_etf_holdings)").fetchall()}
    if "ytd_return" not in cols:
        conn.execute("ALTER TABLE stock_etf_holdings ADD COLUMN ytd_return REAL")
        conn.commit()
        log.info("  + 已新增 ytd_return 欄位")


def upsert_stock_holdings(conn: sqlite3.Connection, stock_code: str, rows: List[dict]) -> int:
    _ensure_holdings_columns(conn)
    today = date.today().isoformat()
    # 動態 key：emega 回應把查詢股票代號嵌在 key 裡（pct_2330 / share_2330）
    pct_key   = f"pct_{stock_code}"
    share_key = f"share_{stock_code}"
    n = 0
    for raw in rows:
        etf_code = _get(raw, "etfId", "ETF代號", "etfCode", "etf_code")
        etf_name = _get(raw, "etfName", "ETF名稱", "etf_name", default="")
        # 權重：優先用精確的 pct_<code>，否則 totalPct（含 %）
        weight = _to_float(_get(raw, pct_key, "totalPct", "總持股權重%"))
        # 張數：totalShare 或 share_<code>
        shares = _to_float(_get(raw, "totalShare", share_key, "總持有張數"))
        ytd    = _to_float(_get(raw, "ytdReward", "ytd_return"))
        if not etf_code:
            m = re.match(r"^(\d{4,6}[A-Z]?)", str(etf_name))
            etf_code = m.group(1) if m else None
            if not etf_code:
                continue
        etf_code = str(etf_code).strip()
        # ETF 名稱清掉尾端重複的代號（"富邦科技  0052" → "富邦科技"）
        clean_name = re.sub(r"\s+\d{4,6}[A-Z]?\s*$", "", str(etf_name)).strip()
        conn.execute("""
            INSERT INTO stock_etf_holdings
                (stock_code, etf_code, etf_name, holding_weight_pct, holding_shares, ytd_return, snapshot_date)
            VALUES (?,?,?,?,?,?,?)
            ON CONFLICT(stock_code, etf_code, snapshot_date) DO UPDATE SET
                etf_name           = excluded.etf_name,
                holding_weight_pct = excluded.holding_weight_pct,
                holding_shares     = excluded.holding_shares,
                ytd_return         = excluded.ytd_return
        """, (stock_code, etf_code, clean_name, weight,
              int(shares) if shares is not None else None, ytd, today))
        n += 1
    conn.commit()
    return n


def fetch_holdings_all(s: requests.Session, conn: sqlite3.Connection,
                       delay: float = 0.4, batch: int = 1):
    """批次抓全部股票的 ETF 持倉，可斷點續傳"""
    stock_codes = [r[0] for r in conn.execute(
        "SELECT stock_code FROM stock_master ORDER BY stock_code").fetchall()]
    if not stock_codes:
        log.warning("stock_master 空的，請先跑 --stock-list")
        return

    ckpt = Path(__file__).parent / "emega_holdings_checkpoint.json"
    done = set()
    if ckpt.exists():
        done = set(json.loads(ckpt.read_text()).get("done", []))
        log.info(f"  ← 續傳（已完成 {len(done)}/{len(stock_codes)}）")

    todo = [c for c in stock_codes if c not in done]
    total = 0
    for i in range(0, len(todo), batch):
        chunk = todo[i:i + batch]
        rows = fetch_stock_holdings(s, chunk)
        # 依 stock_code 分組寫入
        if batch == 1:
            n = upsert_stock_holdings(conn, chunk[0], rows)
            total += n
        else:
            # 多股一次回傳，需依回傳資料的股票代號分組
            by_stock = {}
            for r in rows:
                sc = str(_get(r, "查詢代號", "股票代號", "stockId", default=chunk[0]))
                by_stock.setdefault(sc, []).append(r)
            for sc, rs in by_stock.items():
                total += upsert_stock_holdings(conn, sc, rs)
        done.update(chunk)

        if (i // batch + 1) % 20 == 0:
            ckpt.write_text(json.dumps({"done": list(done)}))
            log.info(f"  進度 {len(done)}/{len(stock_codes)}，已寫入 {total} 筆")
        time.sleep(delay)

    ckpt.write_text(json.dumps({"done": list(done)}))
    log.info(f"✅ 完成：{total} 筆寫入 stock_etf_holdings")


# ─────────────────────────────────────────────
# 除錯：dump JS / probe 原始 JSON
# ─────────────────────────────────────────────
def dump_js(s: requests.Session, context: int = 0):
    js_url = f"{BASE}/resources/js/front/stockRefEtf/index.js?rn=0126"
    try:
        r = s.get(js_url, timeout=15)
        print(f"Status: {r.status_code}, Size: {len(r.text)}")
        if r.status_code != 200:
            print(r.text[:500]); return
        lines = r.text.splitlines()
        if context > 0:
            for i, line in enumerate(lines, 1):
                if "$.ajax" in line or "ajax(" in line:
                    print(f"\n─── ajax @ L{i} ───")
                    for j in range(i - 1, min(i - 1 + context, len(lines))):
                        print(f"L{j+1:4d}: {lines[j].strip()[:140]}")
        else:
            for i, line in enumerate(lines, 1):
                if any(k in line.lower() for k in ("api", "url:", "type:", "data:", "ajax")):
                    print(f"L{i:4d}: {line.strip()[:140]}")
    except Exception as e:
        print(f"Error: {e}")


def probe(s: requests.Session, stock_code: str):
    """印出 stocksEtfShare 的原始回應，確認欄位名稱"""
    print(f"\n=== POST {API_STOCKS_ETFSHARE}  body=[{stock_code!r}] ===")
    try:
        r = s.post(
            API_STOCKS_ETFSHARE,
            data=json.dumps([stock_code]).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            timeout=15,
        )
        print(f"HTTP {r.status_code}")
        print(f"Raw (前 800 字):\n{r.text[:800]}\n")
        outer = r.json()
        print(f"外層 keys: {list(outer.keys())}, code={outer.get('code')}")
        data = outer.get("data")
        if isinstance(data, str):
            data = json.loads(data)
        if isinstance(data, list) and data:
            print(f"\ndata 是 list，共 {len(data)} 筆")
            print(f"第一筆 keys: {list(data[0].keys())}")
            print(f"第一筆內容: {json.dumps(data[0], ensure_ascii=False, indent=2)}")
        else:
            print(f"data 型別: {type(data)}, 內容: {str(data)[:300]}")
    except Exception as e:
        print(f"Error: {e}")


# ─────────────────────────────────────────────
# main
# ─────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser(description="emega ETFMaster 持倉抓取器")
    ap.add_argument("--check-cookie", action="store_true")
    ap.add_argument("--dump-js",      action="store_true")
    ap.add_argument("--context",      type=int, default=0)
    ap.add_argument("--probe",        metavar="CODE", help="印出原始 JSON 確認欄位")
    ap.add_argument("--stock-list",   action="store_true", help="抓全部股票清單入庫")
    ap.add_argument("--holdings",     metavar="CODE", help="抓單股反查 ETF")
    ap.add_argument("--holdings-all", action="store_true", help="批次抓全部個股")
    ap.add_argument("--delay",        type=float, default=0.4)
    args = ap.parse_args()

    s = make_session()
    fetch_csrf_token(s)

    if args.check_cookie:
        r = s.get(f"{BASE}/stockRefEtf/index.do", timeout=10)
        if r.status_code == 200 and "ETF" in r.text:
            log.info(f"✅ 頁面正常（{len(r.text)} 字元）")
            if "X-XSRF-TOKEN" in s.headers or any("CSRF" in k.upper() for k in s.headers):
                log.info("✅ CSRF token 已設定")
        else:
            log.warning(f"⚠ 異常 status={r.status_code}，cookie 可能過期")
        return

    if args.dump_js:
        dump_js(s, context=args.context)
        return

    if args.probe:
        probe(s, args.probe)
        return

    conn = get_conn()

    if args.stock_list:
        log.info("抓取股票清單（POST api/stocks）...")
        rows = fetch_stock_list(s)
        log.info(f"  取得 {len(rows)} 筆")
        if rows:
            first = rows[0]
            log.info(f"  第一筆型別: {type(first).__name__}")
            log.info(f"  第一筆內容: {json.dumps(first, ensure_ascii=False)[:200]}")
            n = upsert_stock_master(conn, rows)
            log.info(f"✅ 寫入 stock_master: {n} 筆")

    if args.holdings:
        log.info(f"抓取 {args.holdings} 的 ETF 持倉...")
        rows = fetch_stock_holdings(s, [args.holdings])
        if rows:
            log.info(f"  範例 keys: {list(rows[0].keys())}")
        n = upsert_stock_holdings(conn, args.holdings, rows)
        log.info(f"✅ {args.holdings} → {len(rows)} 筆原始，寫入 {n} 筆")
        cur = conn.execute("""
            SELECT etf_code, etf_name, holding_weight_pct, holding_shares, ytd_return
            FROM stock_etf_holdings WHERE stock_code=?
            ORDER BY holding_weight_pct DESC NULLS LAST LIMIT 40
        """, (args.holdings,))
        print(f"\n{args.holdings} 被以下 ETF 持有（依權重排序）：")
        print(f"{'ETF代號':<9}{'ETF名稱':<20}{'權重%':>9}{'持有張數':>13}{'YTD%':>9}")
        print("-" * 64)
        for row in cur.fetchall():
            w  = f"{row['holding_weight_pct']:.3f}" if row['holding_weight_pct'] is not None else "—"
            sh = f"{row['holding_shares']:,}" if row['holding_shares'] is not None else "—"
            yd = f"{row['ytd_return']:.2f}" if row['ytd_return'] is not None else "—"
            print(f"{row['etf_code']:<9}{(row['etf_name'] or ''):<20}{w:>9}{sh:>13}{yd:>9}")

    if args.holdings_all:
        log.info("批次抓全部個股（Ctrl+C 可中斷，下次續傳）")
        fetch_holdings_all(s, conn, delay=args.delay)

    conn.close()


if __name__ == "__main__":
    main()
