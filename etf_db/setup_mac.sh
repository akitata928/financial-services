#!/usr/bin/env bash
# ─────────────────────────────────────────────
# 台股 ETF 本機資料庫 — Mac Mini 一鍵安裝腳本
# 用法：bash etf_db/setup_mac.sh
# ─────────────────────────────────────────────
set -euo pipefail

echo "======================================"
echo "  台股 ETF 本機資料庫 安裝程式 (macOS)"
echo "======================================"

# 1. 檢查 Python 3
if ! command -v python3 &>/dev/null; then
  echo "✗ 找不到 python3。請先安裝："
  echo "    brew install python3"
  echo "  或從 https://www.python.org/downloads/ 下載"
  exit 1
fi
echo "✓ Python: $(python3 --version)"

# 2. 建立虛擬環境（避免污染系統 Python）
VENV_DIR="$(dirname "$0")/.venv"
if [ ! -d "$VENV_DIR" ]; then
  echo "→ 建立虛擬環境 $VENV_DIR ..."
  python3 -m venv "$VENV_DIR"
fi
# shellcheck disable=SC1091
source "$VENV_DIR/bin/activate"
echo "✓ 虛擬環境已啟用"

# 3. 安裝依賴
echo "→ 安裝 Python 套件..."
pip install --quiet --upgrade pip
pip install --quiet requests beautifulsoup4 pandas openpyxl tabulate lxml
echo "✓ 套件安裝完成"

# 4. 建立資料庫目錄
DB_PATH="${ETF_DB_PATH:-$HOME/Desktop/etf_master.db}"
echo "✓ 資料庫位置: $DB_PATH"

# 5. 初始化資料庫 + 抓 ETF 清單（TWSE/TPEX 公開 API，不需 cookie）
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
echo ""
echo "→ [1/3] 初始化資料庫 + 抓取 ETF 清單（TWSE/TPEX）..."
python3 "$SCRIPT_DIR/twse_loader.py"

# 6. 顯示統計前提示 cookie 設定
echo ""
echo "→ [2/3] ETF 清單已入庫"
echo "   若要抓取個股 → ETF 反查持倉，請先設定 emega cookie："
echo "   cat > ~/.etf_session_cookie << 'ENDCOOKIE'"
echo "   （貼上 Chrome F12 複製的整串 cookie 字串）"
echo "   ENDCOOKIE"
echo "   python3 etf_db/emega_holdings.py --check-cookie   # 驗證"
echo "   python3 etf_db/emega_holdings.py --stock-list     # 更新個股主檔"
echo "   python3 etf_db/emega_holdings.py --holdings-all   # 批次抓持倉（約 4 分鐘）"

# 7. 顯示統計
echo ""
echo "→ [3/3] 資料庫統計："
python3 "$SCRIPT_DIR/query.py" stats || true
echo ""

echo ""
echo "======================================"
echo "  ✅ 安裝完成！後續指令："
echo "======================================"
cat <<'USAGE'

  # 啟用虛擬環境（每次開新 Terminal 都要先跑這行）
  source etf_db/.venv/bin/activate

  # ETF 清單更新（TWSE/TPEX 公開 API）
  python3 etf_db/twse_loader.py --price         # ETF 清單 + 最新行情

  # 個股 → ETF 反查持倉（需要 emega cookie）
  python3 etf_db/emega_holdings.py --check-cookie
  python3 etf_db/emega_holdings.py --holdings-all   # 批次抓全部個股（約 4 分鐘）

  # 查詢範例
  python3 etf_db/query.py stock 2330            # 台積電被哪些 ETF 持有
  python3 etf_db/query.py compare 0050 0056 00878
  python3 etf_db/query.py holdings 0050         # 0050 前 30 大持股
  python3 etf_db/query.py search 高股息
  python3 etf_db/query.py export --etf 0050 --fmt xlsx

  # 每日更新（可加進 launchd）
  python3 etf_db/twse_loader.py --price && python3 etf_db/emega_holdings.py --holdings-all

USAGE
