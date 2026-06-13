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

# 5. 偵測 emega API endpoint
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
echo ""
echo "→ [1/3] 偵測 emega API endpoint..."
python3 "$SCRIPT_DIR/etl.py" --discover || true

# 6. 初始化資料庫 + 抓 ETF 清單
echo ""
echo "→ [2/3] 初始化資料庫 + 抓取 ETF 清單..."
python3 "$SCRIPT_DIR/etl.py" --init

# 7. 顯示統計
echo ""
echo "→ [3/3] 資料庫統計："
python3 "$SCRIPT_DIR/query.py" stats || true

echo ""
echo "======================================"
echo "  ✅ 安裝完成！後續指令："
echo "======================================"
cat <<'USAGE'

  # 啟用虛擬環境（每次開新 Terminal 都要先跑這行）
  source etf_db/.venv/bin/activate

  # 批次抓全部股票持倉（約 3 分鐘，含限速）
  python3 etf_db/etl.py --holdings-all

  # 查詢範例
  python3 etf_db/query.py stock 2330            # 台積電被哪些 ETF 持有
  python3 etf_db/query.py compare 0050 0056 00878
  python3 etf_db/query.py holdings 0050         # 0050 前 30 大持股
  python3 etf_db/query.py search 高股息
  python3 etf_db/query.py export --etf 0050 --fmt xlsx

  # 每日更新（可加進 crontab / launchd）
  python3 etf_db/etl.py --etf && python3 etf_db/etl.py --holdings-all

USAGE
