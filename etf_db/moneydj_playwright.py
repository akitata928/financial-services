"""
MoneyDJ 台股 ETF 資料補完器 — 用 Playwright 真實 Chromium 繞過 403 反爬

用途：
  補完 etf_profile 缺的欄位（發行商、費用率、規模、受益人數、追蹤指數…）
  以及 etf_market_snapshot 的今年報酬率（ytd_return）。
  資料來源：MoneyDJ Rank0016 受益人數排行（一頁含全部台股 ETF 與多欄位）。

為什麼用 Playwright 而非 requests：
  MoneyDJ 對純 HTTP 請求回 403，必須用真實瀏覽器渲染。這支腳本不需 LLM、
  不需 API key，確定性抓整張表，幾秒完成 —— 適合大量表格資料萃取。
  （browser-use 是 LLM 驅動瀏覽器，適合複雜互動，抓大量表格會燒 token 且慢。）

安裝（Mac Mini，只需一次）：
  source etf_db/.venv/bin/activate
  pip install playwright
  playwright install chromium

用法：
  python3 etf_db/moneydj_playwright.py              # headless 抓取並寫入 DB
  python3 etf_db/moneydj_playwright.py --headed     # 顯示瀏覽器（除錯用）
  python3 etf_db/moneydj_playwright.py --dry-run    # 只抓不寫，印出前 10 筆與表頭
  python3 etf_db/moneydj_playwright.py --dump-html /tmp/mj.html  # 存原始 HTML 供分析
"""

import argparse
import json
import os
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    print("請先安裝：pip install playwright && playwright install chromium")
    raise

DB_PATH = Path(os.environ.get("ETF_DB_PATH", Path.home() / "Desktop" / "etf_master.db"))

RANK_URL = "https://www.moneydj.com/etf/x/Rank/Rank0016.xdjhtm"

# 表頭關鍵字 → etf_profile 欄位。用「包含」比對，容忍 MoneyDJ 改字。
# 只要表頭含左邊任一關鍵字，就對應到右邊欄位。
HEADER_MAP = [
    (("代碼", "代號", "股票代號"),               "etf_code"),
    (("ETF名稱", "基金名稱", "名稱"),            "etf_name"),
    (("發行", "投信", "基金公司", "經理公司"),    "issuer"),
    (("受益人數",),                              "beneficiary_count"),
    (("規模", "淨資產", "資產規模", "基金規模"),   "aum_billion"),
    (("總費用", "費用率", "經理費", "總管理費"),   "expense_ratio"),
    (("追蹤指數", "指數"),                       "tracking_index"),
    (("配息", "分配"),                           "dividend_type"),
    (("今年", "YTD", "年初至今"),                "ytd_return"),
    (("近一年", "一年"),                         "one_year_return"),
    (("類別", "分類", "類型"),                    "category"),
    (("收盤", "淨值", "市價"),                    "close_price"),
]

NUMERIC_COLS = {"beneficiary_count", "aum_billion", "expense_ratio",
                "ytd_return", "one_year_return", "close_price"}


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def today_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _match_column(header: str) -> str | None:
    h = header.strip()
    for keywords, col in HEADER_MAP:
        if any(k in h for k in keywords):
            return col
    return None


def _parse_number(raw: str) -> float | None:
    """把 '1,234'、'12.3%'、'5,678人'、'12.3億' 轉成 float。無效回 None。"""
    if raw is None:
        return None
    s = str(raw).strip().replace(",", "").replace("%", "")
    s = re.sub(r"[人億元檔張\s]", "", s)
    if s in ("", "-", "--", "N/A", "n/a"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Playwright 抓取
# ---------------------------------------------------------------------------

def scrape_moneydj(headed: bool = False, dump_html: str | None = None) -> tuple[list[str], list[dict]]:
    """回傳 (原始表頭清單, 每列 dict：key=對應欄位或 raw_N)。"""
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=not headed,
            args=["--disable-blink-features=AutomationControlled"],
        )
        ctx = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ),
            locale="zh-TW",
            viewport={"width": 1440, "height": 900},
        )
        page = ctx.new_page()
        print(f"→ 開啟 {RANK_URL} ...")
        page.goto(RANK_URL, wait_until="networkidle", timeout=60000)
        page.wait_for_timeout(1500)  # 讓 JS 排行表渲染完

        html = page.content()
        if dump_html:
            Path(dump_html).write_text(html, encoding="utf-8")
            print(f"→ 已存原始 HTML：{dump_html}")

        # 找出資料列最多的那張表
        tables = page.query_selector_all("table")
        best, best_rows = None, 0
        for t in tables:
            n = len(t.query_selector_all("tr"))
            if n > best_rows:
                best, best_rows = t, n
        if best is None:
            browser.close()
            return [], []

        trs = best.query_selector_all("tr")
        # 表頭：第一個含 th 的列，否則第一列的 td
        headers: list[str] = []
        for tr in trs:
            ths = tr.query_selector_all("th")
            if ths:
                headers = [th.inner_text().strip() for th in ths]
                break
        if not headers and trs:
            headers = [td.inner_text().strip() for td in trs[0].query_selector_all("td")]

        # 每個表頭欄位對應到哪個 DB 欄位
        col_for_idx = {i: _match_column(h) for i, h in enumerate(headers)}

        rows: list[dict] = []
        for tr in trs:
            tds = tr.query_selector_all("td")
            if not tds or len(tds) < 2:
                continue
            values = [td.inner_text().strip() for td in tds]
            if len(values) != len(headers):
                # 欄數不符（可能是跨欄標題列），仍嘗試靠位置對應
                pass
            rec: dict = {}
            for i, v in enumerate(values):
                col = col_for_idx.get(i)
                key = col if col else f"raw_{i}"
                rec[key] = v
            # 必須有像 ETF 代碼的欄位
            code = rec.get("etf_code", "")
            if not re.match(r"^0\d{3,5}[A-Z]{0,2}$", code):
                # 從所有欄位裡撈一個像代碼的
                for v in values:
                    if re.match(r"^00\d{2,4}[A-Z]{0,2}$", v.strip()):
                        rec["etf_code"] = v.strip()
                        break
            rows.append(rec)

        browser.close()

    # 只留有 00 開頭代碼的（官方 ETF 編碼原則）
    clean = [r for r in rows if str(r.get("etf_code", "")).startswith("00")]
    return headers, clean


# ---------------------------------------------------------------------------
# 寫入 DB
# ---------------------------------------------------------------------------

def upsert(rows: list[dict]) -> tuple[int, int]:
    if not DB_PATH.exists():
        print(f"✗ 找不到 DB：{DB_PATH}，請先跑 twse_loader.py")
        sys.exit(1)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    now = now_iso()
    snap_date = today_iso()
    n_profile = 0
    n_snap = 0

    for r in rows:
        code = r.get("etf_code")
        if not code:
            continue

        profile_cols = {
            "issuer":            r.get("issuer"),
            "category":          r.get("category"),
            "tracking_index":    r.get("tracking_index"),
            "dividend_type":     r.get("dividend_type"),
            "expense_ratio":     _parse_number(r.get("expense_ratio")),
            "aum_billion":       _parse_number(r.get("aum_billion")),
            "beneficiary_count": _parse_number(r.get("beneficiary_count")),
            "etf_name":          r.get("etf_name"),
        }
        # 只更新有值的欄位（COALESCE：新值優先，None 保留舊值）
        conn.execute(
            """
            INSERT INTO etf_profile (etf_code, etf_name, issuer, category,
                tracking_index, dividend_type, expense_ratio, aum_billion,
                beneficiary_count, raw_json, fetched_at)
            VALUES (:etf_code, :etf_name, :issuer, :category, :tracking_index,
                :dividend_type, :expense_ratio, :aum_billion, :beneficiary_count,
                :raw_json, :fetched_at)
            ON CONFLICT(etf_code) DO UPDATE SET
                etf_name          = COALESCE(excluded.etf_name, etf_profile.etf_name),
                issuer            = COALESCE(excluded.issuer, etf_profile.issuer),
                category          = COALESCE(excluded.category, etf_profile.category),
                tracking_index    = COALESCE(excluded.tracking_index, etf_profile.tracking_index),
                dividend_type     = COALESCE(excluded.dividend_type, etf_profile.dividend_type),
                expense_ratio     = COALESCE(excluded.expense_ratio, etf_profile.expense_ratio),
                aum_billion       = COALESCE(excluded.aum_billion, etf_profile.aum_billion),
                beneficiary_count = COALESCE(excluded.beneficiary_count, etf_profile.beneficiary_count),
                fetched_at        = excluded.fetched_at
            """,
            {
                "etf_code": code,
                "raw_json": json.dumps(r, ensure_ascii=False),
                "fetched_at": now,
                **profile_cols,
            },
        )
        n_profile += 1

        ytd = _parse_number(r.get("ytd_return"))
        one_yr = _parse_number(r.get("one_year_return"))
        close = _parse_number(r.get("close_price"))
        if ytd is not None or one_yr is not None or close is not None:
            conn.execute(
                """
                INSERT INTO etf_market_snapshot
                    (etf_code, snapshot_date, close_price, ytd_return, one_year_return)
                VALUES (:etf_code, :snap, :close, :ytd, :one_yr)
                ON CONFLICT(etf_code, snapshot_date) DO UPDATE SET
                    close_price     = COALESCE(excluded.close_price, etf_market_snapshot.close_price),
                    ytd_return      = COALESCE(excluded.ytd_return, etf_market_snapshot.ytd_return),
                    one_year_return = COALESCE(excluded.one_year_return, etf_market_snapshot.one_year_return)
                """,
                {"etf_code": code, "snap": snap_date, "close": close, "ytd": ytd, "one_yr": one_yr},
            )
            n_snap += 1

    conn.commit()
    conn.close()
    return n_profile, n_snap


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="MoneyDJ ETF 資料補完（Playwright 真實瀏覽器）")
    ap.add_argument("--headed", action="store_true", help="顯示瀏覽器視窗（除錯）")
    ap.add_argument("--dry-run", action="store_true", help="只抓不寫，印出表頭與前 10 筆")
    ap.add_argument("--dump-html", metavar="PATH", help="存原始 HTML 供分析")
    ap.add_argument("--db", help="DB 路徑（覆蓋預設）")
    args = ap.parse_args()

    global DB_PATH
    if args.db:
        DB_PATH = Path(args.db)

    print("=" * 60)
    print("  MoneyDJ ETF 資料補完器（Playwright）")
    print("=" * 60)

    headers, rows = scrape_moneydj(headed=args.headed, dump_html=args.dump_html)

    print(f"\n原始表頭（{len(headers)} 欄）：")
    for i, h in enumerate(headers):
        mapped = _match_column(h)
        print(f"  [{i}] {h!r:20s} → {mapped or '（未對應）'}")

    print(f"\n抓到 {len(rows)} 檔 ETF（00 開頭）")

    if not rows:
        print("✗ 沒抓到資料。用 --dump-html /tmp/mj.html 存 HTML 給我分析表格結構。")
        return

    print("\n前 10 筆（對應後欄位）：")
    keys = ["etf_code", "etf_name", "issuer", "beneficiary_count",
            "aum_billion", "expense_ratio", "ytd_return"]
    print("  " + " | ".join(k for k in keys))
    for r in rows[:10]:
        print("  " + " | ".join(str(r.get(k, ""))[:12] for k in keys))

    if args.dry_run:
        print("\n[dry-run] 未寫入 DB。確認欄位對應無誤後，移除 --dry-run 重跑。")
        return

    n_p, n_s = upsert(rows)
    print(f"\n✅ 已更新 etf_profile：{n_p} 筆")
    print(f"✅ 已更新 etf_market_snapshot（報酬率）：{n_s} 筆")
    print("\n驗證：python3 etf_db/query.py etf 0050")


if __name__ == "__main__":
    main()
