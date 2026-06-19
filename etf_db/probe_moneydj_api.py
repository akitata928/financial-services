"""
MoneyDJ ETF API 端點探測腳本
用法：python3 etf_db/probe_moneydj_api.py

依 XDJ 框架慣例，逐一嘗試常見的 API pattern，
並列出哪些端點能回傳 JSON 資料（直接可用的 API）。
"""

import json
import time
import requests

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Referer": "https://www.moneydj.com/ETF/",
    "Accept-Language": "zh-TW,zh;q=0.9",
    "Accept": "application/json, text/html, */*",
    "X-Requested-With": "XMLHttpRequest",
}

BASE_PARAMS = {"eRank": "allex", "eOrd": "T100100", "eSort": "2"}

# 依 XDJ 框架慣例嘗試的端點變體
CANDIDATES = [
    # ── XDJ 框架：把 .xdjhtm 換成各種 JSON 後綴 ──
    ("GET",  "https://www.moneydj.com/etf/x/Rank/Rank0016.djjson",  BASE_PARAMS),
    ("GET",  "https://www.moneydj.com/etf/x/Rank/Rank0016.djjsonp", BASE_PARAMS),
    ("GET",  "https://www.moneydj.com/etf/x/Rank/Rank0016.ashx",    BASE_PARAMS),

    # ── /djapi/ 路徑 ──
    ("GET",  "https://www.moneydj.com/djapi/etf/rank",              {"eRank": "allex"}),
    ("GET",  "https://www.moneydj.com/djapi/ETF/Rank0016",          BASE_PARAMS),

    # ── /fund/x/ 路徑（MoneyDJ 另一系統）──
    ("GET",  "https://www.moneydj.com/fund/x/Rank/Rank0016.djjson", BASE_PARAMS),

    # ── /ETF/api/ 路徑 ──
    ("GET",  "https://www.moneydj.com/ETF/api/etfrank",             BASE_PARAMS),
    ("GET",  "https://www.moneydj.com/ETF/api/rank",                BASE_PARAMS),

    # ── 以 .aspx 結尾（部分 XDJ 站台）──
    ("GET",  "https://www.moneydj.com/etf/x/Rank/Rank0016.aspx",   BASE_PARAMS),

    # ── 帶 response=json 參數（類 TWSE 風格）──
    ("GET",  "https://www.moneydj.com/etf/x/Rank/Rank0016.xdjhtm",
             {**BASE_PARAMS, "response": "json"}),
    ("GET",  "https://www.moneydj.com/etf/x/Rank/Rank0016.xdjhtm",
             {**BASE_PARAMS, "format": "json"}),
]

def probe():
    s = requests.Session()
    s.headers.update(HEADERS)

    print("=" * 65)
    print("  MoneyDJ ETF API 探測")
    print("=" * 65)
    print(f"{'#':<3} {'狀態':<6} {'JSON?':<6} {'URL'}")
    print("-" * 65)

    found = []
    for i, (method, url, params) in enumerate(CANDIDATES, 1):
        try:
            r = s.request(method, url, params=params, timeout=10)
            status = r.status_code
            is_json = False
            ct = r.headers.get("Content-Type", "")
            body = r.text[:500] if r.text else ""

            # 判斷是否為 JSON
            if "json" in ct.lower():
                is_json = True
            else:
                try:
                    data = json.loads(body)
                    is_json = True
                except Exception:
                    # 有時 XDJ 回傳 djjson( {...} ) 格式（JSONP）
                    if body.startswith("djjson(") or body.startswith("callback("):
                        is_json = True

            mark = "✅" if (status == 200 and is_json) else ("🟡" if status == 200 else "❌")
            short_url = url.replace("https://www.moneydj.com", "")
            print(f"{i:<3} {status:<6} {'是' if is_json else '否':<6} {mark} {short_url}")

            if status == 200 and is_json:
                found.append((url, params, body))

            time.sleep(0.4)
        except Exception as e:
            print(f"{i:<3} ERR    否     ❌ {url}  ({e})")
            time.sleep(0.4)

    print("\n" + "=" * 65)
    if found:
        print(f"✅ 找到 {len(found)} 個可用 JSON API：\n")
        for url, params, body in found:
            print(f"  URL: {url}")
            print(f"  參數: {params}")
            print(f"  回應前 300 字: {body[:300]}")
            print()
    else:
        print("❌ 無直接 JSON API，建議繼續用 HTML 解析（現有方案）")
    print("=" * 65)


if __name__ == "__main__":
    probe()
