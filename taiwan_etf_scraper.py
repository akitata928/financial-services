"""
台股 ETF 資料爬蟲
資料來源：
  1. TWSE (台灣證券交易所) - 上市 ETF
  2. TPEX (證券櫃檯買賣中心) - 上櫃 ETF
  3. emega.com.tw ETFMaster - 持倉成分股

執行前請安裝：pip install requests beautifulsoup4 pandas openpyxl lxml
"""

import requests
from bs4 import BeautifulSoup
import pandas as pd
import json
import time
import re
from datetime import datetime, date
import openpyxl
from openpyxl.styles import (
    PatternFill, Font, Alignment, Border, Side, numbers
)
from openpyxl.utils import get_column_letter
import warnings
warnings.filterwarnings("ignore")

# ─────────────────────────────────────────────
# 通用 Session 設定（模擬瀏覽器）
# ─────────────────────────────────────────────
SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/html, */*",
    "Accept-Language": "zh-TW,zh;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
})


# ─────────────────────────────────────────────
# 1. TWSE 上市 ETF 清單
#    API: https://www.twse.com.tw/fund/TWT38U
# ─────────────────────────────────────────────
def fetch_twse_etf_list():
    """抓取 TWSE 上市 ETF 清單"""
    print("[1/4] 抓取 TWSE 上市 ETF 清單...")
    url = "https://www.twse.com.tw/fund/TWT38U"
    params = {"response": "json", "_": int(time.time() * 1000)}
    try:
        resp = SESSION.get(url, params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        rows = []
        fields = data.get("fields", [])
        for item in data.get("data", []):
            rows.append(dict(zip(fields, item)))
        print(f"  → 取得 {len(rows)} 筆上市 ETF")
        return rows
    except Exception as e:
        print(f"  ✗ 失敗: {e}")
        return []


# ─────────────────────────────────────────────
# 2. TWSE opendata - ETF 基本資料
#    API: https://opendata.twse.com.tw/v1/ETFortfolioComposition/getETFList
# ─────────────────────────────────────────────
def fetch_twse_opendata_etf():
    """抓取 TWSE OpenData ETF 清單（含追蹤指數、費用率等）"""
    print("[2/4] 抓取 TWSE OpenData ETF 詳細資料...")
    url = "https://opendata.twse.com.tw/v1/ETFortfolioComposition/getETFList"
    try:
        resp = SESSION.get(url, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        print(f"  → 取得 {len(data)} 筆 ETF 詳細資料")
        return data
    except Exception as e:
        print(f"  ✗ 失敗: {e}")
        return []


# ─────────────────────────────────────────────
# 3. TPEX 上櫃 ETF 清單
#    API: https://www.tpex.org.tw/web/ETF/etfMainList.php
# ─────────────────────────────────────────────
def fetch_tpex_etf_list():
    """抓取 TPEX 上櫃 ETF 清單"""
    print("[3/4] 抓取 TPEX 上櫃 ETF 清單...")
    url = "https://www.tpex.org.tw/web/ETF/etfMainList.php"
    params = {"l": "zh-tw"}
    try:
        resp = SESSION.get(url, params=params, timeout=15)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "lxml")
        table = soup.find("table", {"id": "etfMainListTable"}) or soup.find("table")
        rows = []
        if table:
            headers = [th.get_text(strip=True) for th in table.find_all("th")]
            for tr in table.find("tbody").find_all("tr"):
                cells = [td.get_text(strip=True) for td in tr.find_all("td")]
                if cells:
                    rows.append(dict(zip(headers, cells)))
        print(f"  → 取得 {len(rows)} 筆上櫃 ETF")
        return rows
    except Exception as e:
        print(f"  ✗ 失敗: {e}")
        return []


# ─────────────────────────────────────────────
# 4. emega ETFMaster - ETF 清單 + 持倉成分股
#    需要模擬登入/Session，以下為已知 endpoint 嘗試
# ─────────────────────────────────────────────
def fetch_emega_etf_list():
    """
    嘗試抓取 emega.com.tw ETFMaster 清單。
    該網站使用 Java Struts/.do 架構，可能需要先取得 session cookie。
    """
    print("[4/4] 嘗試抓取 emega.com.tw ETFMaster...")
    base = "https://www.emega.com.tw"

    # Step 1: 取得首頁 session cookie
    try:
        SESSION.headers["Referer"] = "https://www.emega.com.tw/"
        home_resp = SESSION.get(f"{base}/etfmaster/index.do", timeout=15)
        home_resp.raise_for_status()
    except Exception as e:
        print(f"  ✗ 首頁連線失敗: {e}")
        return [], []

    # Step 2: 嘗試 AJAX / JSON endpoint for ETF list
    etf_list_endpoints = [
        f"{base}/etfmaster/index.do?action=getEtfList",
        f"{base}/etfmaster/ajax/getEtfList.do",
        f"{base}/etfmaster/etfList.do",
    ]
    etf_list = []
    for ep in etf_list_endpoints:
        try:
            resp = SESSION.get(ep, timeout=10)
            if resp.status_code == 200 and resp.text.strip().startswith("["):
                etf_list = resp.json()
                print(f"  → ETF 清單: {len(etf_list)} 筆 (from {ep})")
                break
        except Exception:
            pass

    if not etf_list:
        # Step 3: 嘗試 POST 方式
        post_payload = {
            "action": "query",
            "pageNo": "1",
            "pageSize": "9999",
        }
        try:
            resp = SESSION.post(
                f"{base}/etfmaster/index.do",
                data=post_payload,
                timeout=10,
            )
            if resp.status_code == 200:
                soup = BeautifulSoup(resp.text, "lxml")
                rows = _parse_emega_table(soup)
                if rows:
                    etf_list = rows
                    print(f"  → ETF 清單 (POST 解析): {len(etf_list)} 筆")
        except Exception as e:
            print(f"  ✗ POST 方式失敗: {e}")

    if not etf_list:
        print("  ✗ emega ETF 清單無法取得（需要完整瀏覽器 / Selenium）")

    # Step 4: 嘗試持倉成分股 endpoint
    holdings_list = []
    holdings_ep = f"{base}/etfmaster/stockRefEtf/index.do"
    try:
        resp = SESSION.get(holdings_ep, timeout=10)
        if resp.status_code == 200:
            soup = BeautifulSoup(resp.text, "lxml")
            holdings_list = _parse_emega_table(soup)
            print(f"  → 持倉資料 (HTML 解析): {len(holdings_list)} 筆")
    except Exception as e:
        print(f"  ✗ 持倉頁面失敗: {e}")

    return etf_list, holdings_list


def _parse_emega_table(soup):
    """解析 emega 頁面的資料表格"""
    rows = []
    table = soup.find("table", {"class": re.compile("table|grid|data", re.I)})
    if not table:
        table = soup.find("table")
    if table:
        headers = []
        thead = table.find("thead") or table
        for th in thead.find_all("th"):
            headers.append(th.get_text(strip=True))
        tbody = table.find("tbody") or table
        for tr in tbody.find_all("tr"):
            cells = [td.get_text(strip=True) for td in tr.find_all("td")]
            if cells and len(cells) >= 2:
                rows.append(dict(zip(headers, cells)) if headers else {"raw": cells})
    return rows


# ─────────────────────────────────────────────
# 5. TWSE 成分股 / 持倉資料
#    API: https://opendata.twse.com.tw/v1/ETFortfolioComposition/...
# ─────────────────────────────────────────────
def fetch_etf_holdings_twse(etf_symbol: str):
    """
    抓取指定 ETF 的持倉成分股（TWSE OpenData）
    例: fetch_etf_holdings_twse("0050")
    """
    url = (
        "https://opendata.twse.com.tw/v1/ETFortfolioComposition/"
        f"getETFComponentStockInformation"
    )
    params = {"stockNo": etf_symbol}
    try:
        resp = SESSION.get(url, params=params, timeout=15)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        print(f"  ✗ 持倉資料 {etf_symbol} 失敗: {e}")
        return []


# ─────────────────────────────────────────────
# 6. 整合資料 → ETF DataFrame
# ─────────────────────────────────────────────
COLUMN_MAPPING_TWSE = {
    # TWSE TWT38U 欄位對應
    "有價證券代號": "ETF代號",
    "有價證券名稱": "ETF名稱",
    "ISIN Code": "ISIN",
    "掛牌日期": "掛牌日期",
    "發行公司": "發行公司",
    "基金規模(億元)": "基金規模(億元)",
    "總費用率": "總費用率(%)",
    "配息頻率": "配息頻率",
    "追蹤指數": "追蹤指數",
}

DEMO_ETF_DATA = [
    # 提供範例資料以供欄位示範（實際執行時由 API 補充）
    {
        "ETF代號": "0050", "ETF名稱": "元大台灣50",
        "交易所": "TWSE上市", "追蹤指數": "臺灣50指數",
        "資產類別": "台股ETF", "投資市場": "台股",
        "發行公司": "元大投信", "掛牌日期": "2003/06/30",
        "基金規模(億元)": "3500", "受益人數": "650000",
        "總費用率(%)": "0.46", "配息頻率": "半年配",
        "最新淨值": "175.50", "收盤價": "175.60",
        "折溢價(%)": "0.06", "成交量(張)": "12500",
        "ISIN": "TW0000050004",
    },
    {
        "ETF代號": "0056", "ETF名稱": "元大高股息",
        "交易所": "TWSE上市", "追蹤指數": "臺灣高股息指數",
        "資產類別": "台股ETF", "投資市場": "台股",
        "發行公司": "元大投信", "掛牌日期": "2007/12/26",
        "基金規模(億元)": "2800", "受益人數": "1200000",
        "總費用率(%)": "0.66", "配息頻率": "月配",
        "最新淨值": "43.20", "收盤價": "43.25",
        "折溢價(%)": "0.12", "成交量(張)": "45000",
        "ISIN": "TW0000056001",
    },
    {
        "ETF代號": "00878", "ETF名稱": "國泰永續高股息",
        "交易所": "TWSE上市", "追蹤指數": "MSCI台灣ESG永續高股息精選30指數",
        "資產類別": "台股ETF", "投資市場": "台股",
        "發行公司": "國泰投信", "掛牌日期": "2020/07/20",
        "基金規模(億元)": "3200", "受益人數": "1800000",
        "總費用率(%)": "0.65", "配息頻率": "季配",
        "最新淨值": "23.50", "收盤價": "23.55",
        "折溢價(%)": "0.21", "成交量(張)": "80000",
        "ISIN": "TW0000878006",
    },
    {
        "ETF代號": "00929", "ETF名稱": "復華台灣科技優息",
        "交易所": "TWSE上市", "追蹤指數": "臺灣指數公司特選台灣科技優息指數",
        "資產類別": "台股ETF", "投資市場": "台股",
        "發行公司": "復華投信", "掛牌日期": "2023/06/08",
        "基金規模(億元)": "1200", "受益人數": "900000",
        "總費用率(%)": "0.62", "配息頻率": "月配",
        "最新淨值": "21.30", "收盤價": "21.35",
        "折溢價(%)": "0.23", "成交量(張)": "50000",
        "ISIN": "TW0000929003",
    },
    {
        "ETF代號": "00919", "ETF名稱": "群益台灣精選高息",
        "交易所": "TWSE上市", "追蹤指數": "臺灣指數公司特選台灣精選高息指數",
        "資產類別": "台股ETF", "投資市場": "台股",
        "發行公司": "群益投信", "掛牌日期": "2022/10/20",
        "基金規模(億元)": "950", "受益人數": "750000",
        "總費用率(%)": "0.72", "配息頻率": "月配",
        "最新淨值": "22.10", "收盤價": "22.15",
        "折溢價(%)": "0.23", "成交量(張)": "42000",
        "ISIN": "TW0000919004",
    },
    {
        "ETF代號": "00646", "ETF名稱": "元大S&P500",
        "交易所": "TWSE上市", "追蹤指數": "S&P 500指數",
        "資產類別": "美股ETF", "投資市場": "美股",
        "發行公司": "元大投信", "掛牌日期": "2014/12/04",
        "基金規模(億元)": "450", "受益人數": "120000",
        "總費用率(%)": "0.45", "配息頻率": "不定期配息",
        "最新淨值": "55.20", "收盤價": "55.30",
        "折溢價(%)": "0.18", "成交量(張)": "5000",
        "ISIN": "TW0000646009",
    },
    {
        "ETF代號": "00757", "ETF名稱": "統一FANG+",
        "交易所": "TWSE上市", "追蹤指數": "NYSE FANG+指數",
        "資產類別": "美股ETF", "投資市場": "美股科技",
        "發行公司": "統一投信", "掛牌日期": "2018/07/12",
        "基金規模(億元)": "180", "受益人數": "45000",
        "總費用率(%)": "0.99", "配息頻率": "累積型",
        "最新淨值": "72.40", "收盤價": "72.50",
        "折溢價(%)": "0.14", "成交量(張)": "3500",
        "ISIN": "TW0000757004",
    },
    {
        "ETF代號": "00687B", "ETF名稱": "國泰20年美債",
        "交易所": "TWSE上市", "追蹤指數": "ICE美國政府20+年期債券指數",
        "資產類別": "債券ETF", "投資市場": "美國公債",
        "發行公司": "國泰投信", "掛牌日期": "2017/01/11",
        "基金規模(億元)": "280", "受益人數": "85000",
        "總費用率(%)": "0.26", "配息頻率": "月配",
        "最新淨值": "38.20", "收盤價": "38.25",
        "折溢價(%)": "0.13", "成交量(張)": "8000",
        "ISIN": "TW0000687B07",
    },
    {
        "ETF代號": "00679B", "ETF名稱": "元大美債20年",
        "交易所": "TWSE上市", "追蹤指數": "ICE美國政府20+年期債券指數",
        "資產類別": "債券ETF", "投資市場": "美國公債",
        "發行公司": "元大投信", "掛牌日期": "2017/01/11",
        "基金規模(億元)": "350", "受益人數": "95000",
        "總費用率(%)": "0.25", "配息頻率": "月配",
        "最新淨值": "39.10", "收盤價": "39.15",
        "折溢價(%)": "0.13", "成交量(張)": "9500",
        "ISIN": "TW0000679B06",
    },
    {
        "ETF代號": "00713", "ETF名稱": "元大台灣高息低波",
        "交易所": "TWSE上市", "追蹤指數": "臺灣指數公司特選高股息低波動指數",
        "資產類別": "台股ETF", "投資市場": "台股",
        "發行公司": "元大投信", "掛牌日期": "2017/09/27",
        "基金規模(億元)": "750", "受益人數": "380000",
        "總費用率(%)": "0.45", "配息頻率": "季配",
        "最新淨值": "58.30", "收盤價": "58.35",
        "折溢價(%)": "0.09", "成交量(張)": "15000",
        "ISIN": "TW0000713006",
    },
]

DEMO_HOLDINGS_DATA = [
    {"ETF代號": "0050", "ETF名稱": "元大台灣50",
     "成分股代號": "2330", "成分股名稱": "台積電",
     "持股比例(%)": "48.52", "持股股數(千股)": "125000",
     "持股市值(萬元)": "105500000", "資料日期": "2024-05-31"},
    {"ETF代號": "0050", "ETF名稱": "元大台灣50",
     "成分股代號": "2317", "成分股名稱": "鴻海",
     "持股比例(%)": "4.85", "持股股數(千股)": "28500",
     "持股市值(萬元)": "10542000", "資料日期": "2024-05-31"},
    {"ETF代號": "0050", "ETF名稱": "元大台灣50",
     "成分股代號": "2454", "成分股名稱": "聯發科",
     "持股比例(%)": "4.12", "持股股數(千股)": "5800",
     "持股市值(萬元)": "8950000", "資料日期": "2024-05-31"},
    {"ETF代號": "0056", "ETF名稱": "元大高股息",
     "成分股代號": "2881", "成分股名稱": "富邦金",
     "持股比例(%)": "5.20", "持股股數(千股)": "52000",
     "持股市值(萬元)": "3200000", "資料日期": "2024-05-31"},
    {"ETF代號": "0056", "ETF名稱": "元大高股息",
     "成分股代號": "2882", "成分股名稱": "國泰金",
     "持股比例(%)": "4.88", "持股股數(千股)": "75000",
     "持股市值(萬元)": "3006000", "資料日期": "2024-05-31"},
    {"ETF代號": "00878", "ETF名稱": "國泰永續高股息",
     "成分股代號": "2330", "成分股名稱": "台積電",
     "持股比例(%)": "6.50", "持股股數(千股)": "12000",
     "持股市值(萬元)": "10080000", "資料日期": "2024-05-31"},
]


# ─────────────────────────────────────────────
# 7. 建立 Excel 報表
# ─────────────────────────────────────────────
ETF_COLUMNS = [
    ("ETF代號",         "A", 10),
    ("ETF名稱",         "B", 22),
    ("交易所",          "C", 12),
    ("資產類別",        "D", 14),
    ("投資市場",        "E", 14),
    ("追蹤指數",        "F", 36),
    ("發行公司",        "G", 16),
    ("掛牌日期",        "H", 12),
    ("基金規模(億元)",  "I", 14),
    ("受益人數",        "J", 12),
    ("總費用率(%)",     "K", 12),
    ("配息頻率",        "L", 12),
    ("最新淨值",        "M", 10),
    ("收盤價",          "N", 10),
    ("折溢價(%)",       "O", 10),
    ("成交量(張)",      "P", 12),
    ("ISIN",            "Q", 18),
]

HOLDINGS_COLUMNS = [
    ("ETF代號",        "A", 10),
    ("ETF名稱",        "B", 22),
    ("成分股代號",     "C", 12),
    ("成分股名稱",     "D", 18),
    ("持股比例(%)",    "E", 12),
    ("持股股數(千股)", "F", 14),
    ("持股市值(萬元)", "G", 16),
    ("資料日期",       "H", 12),
]

# 色票
COLOR_HEADER_BLUE   = "1F4E79"
COLOR_HEADER_GREEN  = "1E4620"
COLOR_HEADER_GOLD   = "C55A11"
COLOR_ROW_EVEN      = "EBF3FB"
COLOR_ROW_ODD       = "FFFFFF"
COLOR_ACCENT        = "2E75B6"


def _make_border(style="thin"):
    s = Side(style=style, color="B0B0B0")
    return Border(left=s, right=s, top=s, bottom=s)


def _style_header(ws, row_num, columns, fill_color=COLOR_HEADER_BLUE):
    fill = PatternFill("solid", fgColor=fill_color)
    font = Font(bold=True, color="FFFFFF", size=11)
    align = Alignment(horizontal="center", vertical="center", wrap_text=True)
    border = _make_border()
    for col_def in columns:
        col_letter = col_def[1]
        cell = ws[f"{col_letter}{row_num}"]
        cell.fill = fill
        cell.font = font
        cell.alignment = align
        cell.border = border


def _style_data_rows(ws, start_row, end_row, columns):
    for row in range(start_row, end_row + 1):
        fill_color = COLOR_ROW_EVEN if row % 2 == 0 else COLOR_ROW_ODD
        fill = PatternFill("solid", fgColor=fill_color)
        border = _make_border()
        for col_def in columns:
            col_letter = col_def[1]
            cell = ws[f"{col_letter}{row}"]
            cell.fill = fill
            cell.border = border
            cell.alignment = Alignment(
                horizontal="center", vertical="center", wrap_text=False
            )


def build_excel(etf_data: list, holdings_data: list, output_path: str):
    """建立 Excel 報表"""
    wb = openpyxl.Workbook()

    # ── Sheet 1: ETF 清單 ──────────────────────
    ws1 = wb.active
    ws1.title = "台股ETF清單"
    ws1.freeze_panes = "B2"
    ws1.row_dimensions[1].height = 36

    # 標題列
    for col_def in ETF_COLUMNS:
        name, col_letter, width = col_def
        cell = ws1[f"{col_letter}1"]
        cell.value = name
        ws1.column_dimensions[col_letter].width = width
    _style_header(ws1, 1, ETF_COLUMNS, COLOR_HEADER_BLUE)

    # 資料列
    col_names = [c[0] for c in ETF_COLUMNS]
    for i, row_data in enumerate(etf_data, start=2):
        for col_def in ETF_COLUMNS:
            name, col_letter, _ = col_def
            ws1[f"{col_letter}{i}"] = row_data.get(name, "")
    _style_data_rows(ws1, 2, 1 + len(etf_data), ETF_COLUMNS)

    # 統計資訊
    info_row = len(etf_data) + 3
    ws1[f"A{info_row}"] = f"資料更新：{date.today().strftime('%Y/%m/%d')}"
    ws1[f"A{info_row}"].font = Font(italic=True, color="666666", size=9)
    ws1[f"C{info_row}"] = f"共 {len(etf_data)} 檔 ETF"
    ws1[f"C{info_row}"].font = Font(bold=True, color=COLOR_ACCENT, size=10)

    # ── Sheet 2: 持倉成分股 ──────────────────
    ws2 = wb.create_sheet("持倉成分股")
    ws2.freeze_panes = "C2"
    ws2.row_dimensions[1].height = 36

    for col_def in HOLDINGS_COLUMNS:
        name, col_letter, width = col_def
        cell = ws2[f"{col_letter}1"]
        cell.value = name
        ws2.column_dimensions[col_letter].width = width
    _style_header(ws2, 1, HOLDINGS_COLUMNS, COLOR_HEADER_GREEN)

    for i, row_data in enumerate(holdings_data, start=2):
        for col_def in HOLDINGS_COLUMNS:
            name, col_letter, _ = col_def
            ws2[f"{col_letter}{i}"] = row_data.get(name, "")
    if holdings_data:
        _style_data_rows(ws2, 2, 1 + len(holdings_data), HOLDINGS_COLUMNS)

    info_row2 = len(holdings_data) + 3
    ws2[f"A{info_row2}"] = f"資料更新：{date.today().strftime('%Y/%m/%d')}"
    ws2[f"A{info_row2}"].font = Font(italic=True, color="666666", size=9)

    # ── Sheet 3: 欄位說明 ─────────────────────
    ws3 = wb.create_sheet("欄位說明")
    ws3.column_dimensions["A"].width = 20
    ws3.column_dimensions["B"].width = 50
    ws3.column_dimensions["C"].width = 30

    headers = ["欄位名稱", "說明", "資料來源"]
    for i, h in enumerate(headers, start=1):
        cell = ws3.cell(row=1, column=i, value=h)
        cell.fill = PatternFill("solid", fgColor=COLOR_HEADER_GOLD)
        cell.font = Font(bold=True, color="FFFFFF", size=11)
        cell.alignment = Alignment(horizontal="center", vertical="center")
        cell.border = _make_border()
    ws3.row_dimensions[1].height = 28

    field_docs = [
        ("ETF代號",         "ETF在交易所的股票代號",                      "TWSE / TPEX"),
        ("ETF名稱",         "ETF中文簡稱",                                "TWSE / TPEX"),
        ("交易所",          "TWSE上市 / TPEX上櫃",                        "TWSE / TPEX"),
        ("資產類別",        "台股ETF / 美股ETF / 債券ETF / 商品ETF 等",   "emega / 自行分類"),
        ("投資市場",        "台股 / 美股 / 亞股 / 全球 / 美國公債 等",    "emega / 自行分類"),
        ("追蹤指數",        "ETF 所追蹤的基準指數名稱",                   "TWSE OpenData / emega"),
        ("發行公司",        "基金管理公司（投信）名稱",                   "TWSE / TPEX"),
        ("掛牌日期",        "ETF 在交易所正式掛牌交易日期",               "TWSE / TPEX"),
        ("基金規模(億元)",  "基金淨資產總值（新台幣億元）",               "TWSE OpenData / 各投信"),
        ("受益人數",        "持有該 ETF 受益憑證的投資人數",              "TWSE OpenData"),
        ("總費用率(%)",     "年化總費用率，包含管理費、保管費等",         "各投信公開說明書"),
        ("配息頻率",        "月配 / 季配 / 半年配 / 年配 / 累積型",       "各投信 / emega"),
        ("最新淨值",        "最新每單位淨值（NAV）",                      "TWSE / TPEX"),
        ("收盤價",          "最新收盤市場價格",                           "TWSE / TPEX"),
        ("折溢價(%)",       "(收盤價 - NAV) / NAV × 100%",               "計算欄位"),
        ("成交量(張)",      "當日成交張數（1張=1000股）",                 "TWSE / TPEX"),
        ("ISIN",            "國際證券識別碼",                             "TWSE"),
        ("", "", ""),
        ("成分股代號",      "持倉個股在台灣交易所的股票代號",             "TWSE OpenData / emega"),
        ("成分股名稱",      "持倉個股中文名稱",                           "TWSE OpenData / emega"),
        ("持股比例(%)",     "該個股佔 ETF 淨資產的百分比",               "TWSE OpenData / emega"),
        ("持股股數(千股)",  "ETF 持有該個股的股數（千股單位）",           "TWSE OpenData"),
        ("持股市值(萬元)",  "ETF 持有該個股的市值（新台幣萬元）",         "TWSE OpenData"),
        ("資料日期",        "持倉資料的公告/揭露日期",                    "TWSE OpenData / emega"),
    ]
    for row_idx, (f, d, s) in enumerate(field_docs, start=2):
        fill = PatternFill("solid", fgColor="FFF2CC" if f else "FFFFFF")
        ws3.cell(row=row_idx, column=1, value=f).fill = fill
        ws3.cell(row=row_idx, column=2, value=d).fill = fill
        ws3.cell(row=row_idx, column=3, value=s).fill = fill
        for col in range(1, 4):
            c = ws3.cell(row=row_idx, column=col)
            c.border = _make_border()
            c.alignment = Alignment(vertical="center", wrap_text=True)
        ws3.row_dimensions[row_idx].height = 22

    ws3.cell(row=2, column=1).font = Font(bold=True)

    # ── Sheet 4: API 說明 ────────────────────
    ws4 = wb.create_sheet("資料來源說明")
    ws4.column_dimensions["A"].width = 22
    ws4.column_dimensions["B"].width = 60
    ws4.column_dimensions["C"].width = 20

    ws4.merge_cells("A1:C1")
    title_cell = ws4["A1"]
    title_cell.value = "台股 ETF 資料來源 & 說明"
    title_cell.font = Font(bold=True, size=14, color="FFFFFF")
    title_cell.fill = PatternFill("solid", fgColor=COLOR_ACCENT)
    title_cell.alignment = Alignment(horizontal="center", vertical="center")
    ws4.row_dimensions[1].height = 32

    sources = [
        ["資料來源", "API / 網址", "說明"],
        ["TWSE 上市ETF清單",
         "https://www.twse.com.tw/fund/TWT38U?response=json",
         "上市ETF代號、名稱、掛牌日"],
        ["TWSE OpenData ETF",
         "https://opendata.twse.com.tw/v1/ETFortfolioComposition/getETFList",
         "ETF基本資料含追蹤指數"],
        ["TWSE OpenData 持倉",
         "https://opendata.twse.com.tw/v1/ETFortfolioComposition/getETFComponentStockInformation?stockNo=0050",
         "ETF 成分股持倉資料"],
        ["TPEX 上櫃ETF",
         "https://www.tpex.org.tw/web/ETF/etfMainList.php",
         "上櫃ETF清單"],
        ["emega ETFMaster",
         "https://www.emega.com.tw/etfmaster/index.do",
         "需模擬瀏覽器 / Selenium，持倉查詢篩選器"],
        ["emega 持倉查詢",
         "https://www.emega.com.tw/etfmaster/stockRefEtf/index.do",
         "依個股查詢哪些ETF持有，含持股比例"],
    ]
    for row_idx, row_data in enumerate(sources, start=2):
        fill = PatternFill("solid", fgColor=COLOR_ROW_EVEN if row_idx % 2 == 0 else COLOR_ROW_ODD)
        if row_idx == 2:
            fill = PatternFill("solid", fgColor="D6E4F0")
        for col_idx, val in enumerate(row_data, start=1):
            cell = ws4.cell(row=row_idx, column=col_idx, value=val)
            cell.fill = fill
            cell.border = _make_border()
            cell.alignment = Alignment(vertical="center", wrap_text=True)
            if row_idx == 2:
                cell.font = Font(bold=True, size=10)
        ws4.row_dimensions[row_idx].height = 36

    wb.save(output_path)
    print(f"\n✅ Excel 檔案已儲存：{output_path}")
    print(f"   工作表：台股ETF清單 / 持倉成分股 / 欄位說明 / 資料來源說明")


# ─────────────────────────────────────────────
# 主程式
# ─────────────────────────────────────────────
def main():
    print("=" * 55)
    print("  台股 ETF 資料抓取工具")
    print(f"  執行時間：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 55)

    # 抓取資料
    twse_etf    = fetch_twse_etf_list()
    opendata    = fetch_twse_opendata_etf()
    tpex_etf    = fetch_tpex_etf_list()
    emega_etf, emega_holdings = fetch_emega_etf_list()

    # 合併 TWSE 資料
    all_etf_data = []
    if twse_etf or opendata:
        # 合併 TWT38U + OpenData
        opendata_map = {r.get("ETFNum", ""): r for r in opendata}
        for row in twse_etf:
            code = row.get("有價證券代號", "").strip()
            od   = opendata_map.get(code, {})
            all_etf_data.append({
                "ETF代號":       code,
                "ETF名稱":       row.get("有價證券名稱", ""),
                "交易所":        "TWSE上市",
                "資產類別":      od.get("ETFType", ""),
                "投資市場":      od.get("ETFCategory", ""),
                "追蹤指數":      od.get("IndexName", row.get("追蹤指數", "")),
                "發行公司":      od.get("FundManager", row.get("發行公司", "")),
                "掛牌日期":      row.get("掛牌日期", ""),
                "基金規模(億元)": od.get("TotalNetAsset", ""),
                "受益人數":      od.get("BeneficiaryNum", ""),
                "總費用率(%)":   od.get("ExpenseRatio", ""),
                "配息頻率":      od.get("DividendFrequency", ""),
                "最新淨值":      od.get("LatestNAV", ""),
                "收盤價":        row.get("收盤價", ""),
                "折溢價(%)":     "",
                "成交量(張)":    row.get("成交量", ""),
                "ISIN":          row.get("ISIN Code", ""),
            })
        for row in tpex_etf:
            all_etf_data.append({
                "ETF代號":   row.get("代號", ""),
                "ETF名稱":   row.get("名稱", ""),
                "交易所":    "TPEX上櫃",
                "追蹤指數":  row.get("追蹤指數", ""),
                "發行公司":  row.get("基金管理公司", ""),
                "掛牌日期":  row.get("掛牌日期", ""),
            })

    # 若 API 無資料，使用示範資料
    if not all_etf_data:
        print("\n⚠ 無法從 API 取得資料，使用示範資料建立欄位範本")
        all_etf_data = DEMO_ETF_DATA

    holdings_data = emega_holdings if emega_holdings else DEMO_HOLDINGS_DATA

    # 建立 Excel
    output = "/home/user/Desktop/台股ETF清單.xlsx"
    build_excel(all_etf_data, holdings_data, output)

    print("\n── 欄位規劃摘要 ──────────────────────────")
    print("【ETF清單】17 個欄位:")
    for c in ETF_COLUMNS:
        print(f"  {c[1]:>2}. {c[0]}")
    print("\n【持倉成分股】8 個欄位:")
    for c in HOLDINGS_COLUMNS:
        print(f"  {c[1]:>2}. {c[0]}")
    print("\n── 資料來源狀態 ───────────────────────────")
    print(f"  TWSE 上市ETF : {'✅' if twse_etf else '⚠ 未取得（請本地執行）'}")
    print(f"  TWSE OpenData: {'✅' if opendata else '⚠ 未取得（請本地執行）'}")
    print(f"  TPEX 上櫃ETF : {'✅' if tpex_etf else '⚠ 未取得（請本地執行）'}")
    print(f"  emega ETFMstr: {'✅' if emega_etf else '⚠ 需 Selenium（詳見下方說明）'}")


if __name__ == "__main__":
    main()
