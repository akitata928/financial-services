"""
emega.com.tw ETFMaster Selenium 爬蟲
用途：抓取需要 JavaScript 渲染的 emega ETF 清單與持倉資料

安裝依賴：
  pip install selenium webdriver-manager openpyxl pandas
  # Chrome 需已安裝，或改用 Firefox
  pip install webdriver-manager

使用方式：
  python emega_selenium_scraper.py
"""

import time
import json
import re
import pandas as pd
from datetime import date
from pathlib import Path

try:
    from selenium import webdriver
    from selenium.webdriver.chrome.service import Service
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait, Select
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.common.exceptions import TimeoutException, NoSuchElementException
    from webdriver_manager.chrome import ChromeDriverManager
except ImportError:
    print("請先安裝：pip install selenium webdriver-manager")
    raise

BASE_URL = "https://www.emega.com.tw"
ETF_LIST_URL  = f"{BASE_URL}/etfmaster/index.do"
STOCK_ETF_URL = f"{BASE_URL}/etfmaster/stockRefEtf/index.do"

OUTPUT_PATH = str(Path.home() / "Desktop" / "emega_etf_holdings.xlsx")


# ─────────────────────────────────────────────
# Chrome Driver 設定
# ─────────────────────────────────────────────
def make_driver(headless: bool = True) -> webdriver.Chrome:
    opts = Options()
    if headless:
        opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_argument("--window-size=1920,1080")
    opts.add_argument("--lang=zh-TW")
    opts.add_experimental_option("excludeSwitches", ["enable-automation"])
    opts.add_experimental_option("useAutomationExtension", False)
    opts.add_argument(
        "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
    service = Service(ChromeDriverManager().install())
    driver = webdriver.Chrome(service=service, options=opts)
    driver.execute_cdp_cmd(
        "Page.addScriptToEvaluateOnNewDocument",
        {"source": "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})"},
    )
    return driver


# ─────────────────────────────────────────────
# 1. ETF 清單頁面（index.do）
#    欄位：ETF代號、ETF名稱、規模、費用率、配息、追蹤指數 等
# ─────────────────────────────────────────────
def scrape_etf_list(driver: webdriver.Chrome) -> list[dict]:
    print(f"[1/2] 開啟 ETF 清單頁面: {ETF_LIST_URL}")
    driver.get(ETF_LIST_URL)
    wait = WebDriverWait(driver, 20)

    # 等待主表格載入
    try:
        wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "table tbody tr")))
    except TimeoutException:
        print("  ✗ 表格未載入，嘗試直接解析頁面")

    time.sleep(2)

    # 嘗試點擊「全部顯示」或設定每頁筆數為最大值
    try:
        select_el = driver.find_element(By.CSS_SELECTOR, "select[name*='pageSize'], select[name*='length']")
        sel = Select(select_el)
        sel.select_by_visible_text("全部") if "全部" in [o.text for o in sel.options] \
            else sel.select_by_value(max([o.get_attribute("value") for o in sel.options], key=lambda x: int(x) if x.isdigit() else 0))
        time.sleep(2)
    except NoSuchElementException:
        pass

    # 擷取篩選器選項（資產類別、投資市場等下拉選單）
    filters_info = {}
    for sel_el in driver.find_elements(By.CSS_SELECTOR, "select"):
        try:
            name = sel_el.get_attribute("name") or sel_el.get_attribute("id") or ""
            options = [o.text for o in Select(sel_el).options if o.text.strip()]
            if options:
                filters_info[name] = options
        except Exception:
            pass
    if filters_info:
        print(f"  篩選器欄位: {list(filters_info.keys())}")

    # 解析所有頁面的表格資料
    all_rows = []
    page = 1
    while True:
        rows = _parse_table(driver)
        all_rows.extend(rows)
        print(f"  第 {page} 頁：{len(rows)} 筆")

        # 嘗試翻頁
        try:
            next_btn = driver.find_element(
                By.CSS_SELECTOR,
                "a[aria-label='Next'], li.next a, button.next, a.paginate_button.next"
            )
            if "disabled" in (next_btn.get_attribute("class") or ""):
                break
            next_btn.click()
            time.sleep(1.5)
            page += 1
        except NoSuchElementException:
            break

    print(f"  → 共取得 {len(all_rows)} 筆 ETF 資料")
    return all_rows


# ─────────────────────────────────────────────
# 2. 個股→ETF 持倉查詢（stockRefEtf/index.do）
#    可輸入：股票代號篩選，查詢哪些 ETF 持有此股
#    欄位：ETF代號、ETF名稱、持股比例、持股股數、資料日期
# ─────────────────────────────────────────────
def scrape_stock_ref_etf(
    driver: webdriver.Chrome,
    stock_codes: list[str] | None = None,
) -> list[dict]:
    """
    stock_codes: 若指定則逐一查詢，None 則抓取首頁預設結果
    """
    print(f"\n[2/2] 開啟持倉查詢頁面: {STOCK_ETF_URL}")
    driver.get(STOCK_ETF_URL)
    wait = WebDriverWait(driver, 20)
    time.sleep(2)

    if stock_codes is None:
        # 直接擷取預設結果
        try:
            wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "table tbody tr")))
        except TimeoutException:
            pass
        rows = _parse_table(driver)
        print(f"  → 取得 {len(rows)} 筆持倉資料（預設查詢）")
        return rows

    all_rows = []
    for code in stock_codes:
        print(f"  查詢個股: {code}")
        try:
            # 找輸入框
            input_el = wait.until(
                EC.presence_of_element_located(
                    (By.CSS_SELECTOR, "input[type='text'], input[name*='stock'], input[id*='stock']")
                )
            )
            input_el.clear()
            input_el.send_keys(code)
            time.sleep(0.5)

            # 找送出按鈕
            btn = driver.find_element(
                By.CSS_SELECTOR,
                "button[type='submit'], input[type='submit'], a.btn-search, button.search"
            )
            btn.click()
            time.sleep(2)

            rows = _parse_table(driver)
            for r in rows:
                r.setdefault("查詢代號", code)
            all_rows.extend(rows)
            print(f"    → {len(rows)} 筆")

        except (TimeoutException, NoSuchElementException) as e:
            print(f"    ✗ 查詢 {code} 失敗: {e}")

    print(f"  → 共取得 {len(all_rows)} 筆持倉資料")
    return all_rows


# ─────────────────────────────────────────────
# 通用表格解析
# ─────────────────────────────────────────────
def _parse_table(driver: webdriver.Chrome) -> list[dict]:
    rows = []
    try:
        tables = driver.find_elements(By.CSS_SELECTOR, "table")
        target = None
        for t in tables:
            trs = t.find_elements(By.CSS_SELECTOR, "tbody tr")
            if len(trs) > len(rows):
                target = t
                max_rows = len(trs)

        if not target:
            return rows

        headers = []
        for th in target.find_elements(By.CSS_SELECTOR, "thead th, th"):
            headers.append(th.text.strip())

        for tr in target.find_elements(By.CSS_SELECTOR, "tbody tr"):
            cells = [td.text.strip() for td in tr.find_elements(By.TAG_NAME, "td")]
            if cells:
                if headers and len(headers) == len(cells):
                    rows.append(dict(zip(headers, cells)))
                else:
                    rows.append({f"col_{i}": v for i, v in enumerate(cells)})
    except Exception as e:
        print(f"  ✗ 表格解析錯誤: {e}")
    return rows


# ─────────────────────────────────────────────
# 3. 攔截 AJAX 請求（進階：取得原始 JSON）
# ─────────────────────────────────────────────
def enable_network_logging(driver: webdriver.Chrome):
    """啟用 CDP 網路監聽，攔截 XHR/Fetch 響應"""
    driver.execute_cdp_cmd("Network.enable", {})
    driver.execute_cdp_cmd("Network.setRequestInterception", {
        "patterns": [{"urlPattern": "*emega*etf*", "interceptionStage": "HeadersReceived"}]
    })


def get_intercepted_json(driver: webdriver.Chrome) -> list[dict]:
    """從瀏覽器 performance log 中取得 JSON API 響應"""
    logs = driver.execute_script("""
        return window.performance.getEntriesByType('resource')
            .filter(e => e.initiatorType === 'xmlhttprequest' || e.initiatorType === 'fetch')
            .map(e => e.name);
    """)
    print(f"  攔截到 {len(logs)} 個 XHR/Fetch 請求:")
    for url in logs:
        print(f"    {url}")
    return logs


# ─────────────────────────────────────────────
# 4. 輸出 Excel
# ─────────────────────────────────────────────
def save_to_excel(etf_list: list, holdings: list, path: str):
    import openpyxl
    from openpyxl.styles import PatternFill, Font, Alignment, Border, Side

    wb = openpyxl.Workbook()

    def write_sheet(ws, data, title_color):
        if not data:
            ws.append(["無資料"])
            return
        headers = list(data[0].keys())
        ws.append(headers)
        fill_h = PatternFill("solid", fgColor=title_color)
        font_h = Font(bold=True, color="FFFFFF")
        for cell in ws[1]:
            cell.fill = fill_h
            cell.font = font_h
            cell.alignment = Alignment(horizontal="center")
        for row in data:
            ws.append([row.get(h, "") for h in headers])
        for col in ws.columns:
            max_len = max((len(str(c.value or "")) for c in col), default=10)
            ws.column_dimensions[col[0].column_letter].width = min(max_len + 2, 40)

    ws1 = wb.active
    ws1.title = "ETF清單(emega)"
    write_sheet(ws1, etf_list, "1F4E79")

    ws2 = wb.create_sheet("持倉成分股(emega)")
    write_sheet(ws2, holdings, "1E4620")

    wb.save(path)
    print(f"\n✅ emega 資料已儲存：{path}")


# ─────────────────────────────────────────────
# 主程式
# ─────────────────────────────────────────────
def main():
    print("=" * 55)
    print("  emega ETFMaster Selenium 爬蟲")
    print(f"  目標：{BASE_URL}")
    print("=" * 55)

    # 修改為 headless=False 可以看到瀏覽器畫面（除錯用）
    driver = make_driver(headless=True)

    try:
        etf_list = scrape_etf_list(driver)

        # 查詢特定個股的 ETF 持倉（可自行修改代號清單）
        target_stocks = ["2330", "2317", "2454", "2881", "3008"]
        holdings = scrape_stock_ref_etf(driver, stock_codes=target_stocks)

        # 顯示攔截到的 API endpoint
        print("\n── AJAX API 端點（供開發參考）──")
        get_intercepted_json(driver)

    finally:
        driver.quit()

    save_to_excel(etf_list, holdings, OUTPUT_PATH)

    print("\n── 資料欄位（自 emega 解析）──")
    if etf_list:
        print(f"ETF清單欄位: {list(etf_list[0].keys())}")
    if holdings:
        print(f"持倉欄位:    {list(holdings[0].keys())}")


if __name__ == "__main__":
    main()
