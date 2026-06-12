"""
ETF Local DB Query Interface

Usage:
  python query.py etf 0050                    # ETF profile
  python query.py compare 0050 0056 00878     # compare multiple ETFs
  python query.py stock 2330                  # which ETFs hold this stock
  python query.py top-holders 2330 --limit 10 # top 10 ETFs by weight
  python query.py holdings 0050               # ETF's top holdings
  python query.py search 高股息               # search ETF name/index
  python query.py stats                       # DB summary stats
  python query.py export --etf 0050 --fmt xlsx  # export to xlsx
"""

import argparse
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any, Optional

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DB_PATH = Path(os.environ.get("ETF_DB_PATH", Path.home() / "Desktop" / "etf_master.db"))


# ---------------------------------------------------------------------------
# Connection helper
# ---------------------------------------------------------------------------

def get_connection(db_path: Optional[Path] = None) -> sqlite3.Connection:
    path = db_path or DB_PATH
    if not path.exists():
        print(f"ERROR: Database not found at {path}", file=sys.stderr)
        print("Run `python etl.py --init` to create and populate the database.", file=sys.stderr)
        sys.exit(1)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def rows_to_dicts(rows) -> list:
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Tabulate helper — uses `tabulate` if installed, else pandas, else plain
# ---------------------------------------------------------------------------

def _tabulate(data: list, headers: Optional[list] = None, tablefmt: str = "simple") -> str:
    if not data:
        return "(no data)"

    # Try tabulate first
    try:
        from tabulate import tabulate
        if headers:
            rows = [[row.get(h, "") for h in headers] for row in data]
            return tabulate(rows, headers=headers, tablefmt=tablefmt)
        else:
            if data:
                headers = list(data[0].keys())
                rows = [[row.get(h, "") for h in headers] for row in data]
                return tabulate(rows, headers=headers, tablefmt=tablefmt)
    except ImportError:
        pass

    # Try pandas fallback
    try:
        import pandas as pd
        df = pd.DataFrame(data)
        if headers:
            present = [h for h in headers if h in df.columns]
            df = df[present]
        return df.to_string(index=False)
    except ImportError:
        pass

    # Plain text fallback
    if not data:
        return "(no data)"
    cols = headers or list(data[0].keys())
    widths = {c: max(len(str(c)), max((len(str(row.get(c, ""))) for row in data), default=0)) for c in cols}
    sep = "  "
    header_line = sep.join(str(c).ljust(widths[c]) for c in cols)
    divider = sep.join("-" * widths[c] for c in cols)
    lines = [header_line, divider]
    for row in data:
        lines.append(sep.join(str(row.get(c, "")).ljust(widths[c]) for c in cols))
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Query functions
# ---------------------------------------------------------------------------

def get_etf_profile(conn: sqlite3.Connection, code: str) -> Optional[dict]:
    """Return ETF profile as a dict, or None if not found."""
    row = conn.execute(
        """
        SELECT p.*,
               s.close_price, s.nav, s.premium_discount_pct,
               s.volume_k, s.ytd_return, s.one_year_return, s.snapshot_date
        FROM etf_profile p
        LEFT JOIN (
            SELECT * FROM etf_market_snapshot
            WHERE etf_code = ?
            ORDER BY snapshot_date DESC
            LIMIT 1
        ) s ON p.etf_code = s.etf_code
        WHERE p.etf_code = ?
        """,
        (code, code),
    ).fetchone()
    return dict(row) if row else None


def compare_etfs(conn: sqlite3.Connection, codes: list) -> list:
    """Compare multiple ETFs side by side. Returns list of dicts."""
    placeholders = ",".join("?" * len(codes))
    rows = conn.execute(
        f"""
        SELECT p.etf_code, p.etf_name, p.issuer, p.category, p.region,
               p.expense_ratio, p.aum_billion, p.beneficiary_count,
               p.tracking_index, p.dividend_type, p.inception_date,
               s.close_price, s.nav, s.ytd_return, s.one_year_return
        FROM etf_profile p
        LEFT JOIN (
            SELECT etf_code, close_price, nav, ytd_return, one_year_return
            FROM etf_market_snapshot
            WHERE (etf_code, snapshot_date) IN (
                SELECT etf_code, MAX(snapshot_date)
                FROM etf_market_snapshot
                GROUP BY etf_code
            )
        ) s ON p.etf_code = s.etf_code
        WHERE p.etf_code IN ({placeholders})
        ORDER BY p.etf_code
        """,
        codes,
    ).fetchall()
    return rows_to_dicts(rows)


def stock_to_etfs(
    conn: sqlite3.Connection,
    stock_code: str,
    min_weight: Optional[float] = None,
) -> list:
    """Which ETFs hold a given stock, sorted by weight desc."""
    query = """
        SELECT h.etf_code, h.etf_name, h.holding_weight_pct, h.holding_shares,
               h.snapshot_date, p.category, p.aum_billion
        FROM stock_etf_holdings h
        LEFT JOIN etf_profile p ON h.etf_code = p.etf_code
        WHERE h.stock_code = ?
    """
    params: list = [stock_code]

    if min_weight is not None:
        query += " AND h.holding_weight_pct >= ?"
        params.append(min_weight)

    # Use the latest snapshot for each ETF
    query = f"""
        SELECT h.etf_code, h.etf_name, h.holding_weight_pct, h.holding_shares,
               h.snapshot_date, p.category, p.aum_billion
        FROM stock_etf_holdings h
        LEFT JOIN etf_profile p ON h.etf_code = p.etf_code
        WHERE h.stock_code = ?
          AND h.snapshot_date = (
              SELECT MAX(snapshot_date) FROM stock_etf_holdings
              WHERE stock_code = ? AND etf_code = h.etf_code
          )
        {'AND h.holding_weight_pct >= ' + str(min_weight) if min_weight is not None else ''}
        ORDER BY h.holding_weight_pct DESC NULLS LAST
    """
    rows = conn.execute(query, [stock_code, stock_code]).fetchall()
    return rows_to_dicts(rows)


def etf_top_holdings(conn: sqlite3.Connection, etf_code: str, limit: int = 30) -> list:
    """Top holdings of an ETF sorted by weight desc."""
    rows = conn.execute(
        """
        SELECT h.stock_code, h.stock_name, h.weight_pct, h.shares, h.market_value,
               h.snapshot_date, m.industry
        FROM etf_holdings h
        LEFT JOIN stock_master m ON h.stock_code = m.stock_code
        WHERE h.etf_code = ?
          AND h.snapshot_date = (
              SELECT MAX(snapshot_date) FROM etf_holdings WHERE etf_code = ?
          )
        ORDER BY h.weight_pct DESC NULLS LAST
        LIMIT ?
        """,
        (etf_code, etf_code, limit),
    ).fetchall()
    return rows_to_dicts(rows)


def search_etf(conn: sqlite3.Connection, keyword: str) -> list:
    """Search ETF by name, tracking index, issuer, or category."""
    pattern = f"%{keyword}%"
    rows = conn.execute(
        """
        SELECT etf_code, etf_name, issuer, category, tracking_index,
               expense_ratio, aum_billion, region
        FROM etf_profile
        WHERE etf_name LIKE ?
           OR tracking_index LIKE ?
           OR issuer LIKE ?
           OR category LIKE ?
           OR etf_code LIKE ?
        ORDER BY aum_billion DESC NULLS LAST
        """,
        (pattern, pattern, pattern, pattern, pattern),
    ).fetchall()
    return rows_to_dicts(rows)


def db_stats(conn: sqlite3.Connection) -> None:
    """Print counts and summary info for all tables."""
    tables = [
        "etf_profile",
        "etf_market_snapshot",
        "stock_master",
        "stock_etf_holdings",
        "etf_holdings",
        "etf_stock_weight_history",
        "etf_nav_history",
        "knowledge_chunks",
    ]
    print("\n=== Database Statistics ===")
    print(f"{'Table':<30} {'Rows':>10}")
    print("-" * 42)
    total = 0
    for table in tables:
        try:
            n = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            print(f"{table:<30} {n:>10,}")
            total += n
        except Exception as exc:
            print(f"{table:<30} {'ERROR':>10}  ({exc})")

    print("-" * 42)
    print(f"{'TOTAL':<30} {total:>10,}")

    # Latest snapshot dates
    for tbl, col, code_col in [
        ("etf_market_snapshot", "snapshot_date", "etf_code"),
        ("stock_etf_holdings", "snapshot_date", "stock_code"),
        ("etf_holdings", "snapshot_date", "etf_code"),
    ]:
        try:
            row = conn.execute(
                f"SELECT MAX({col}) as latest, COUNT(DISTINCT {code_col}) as codes FROM {tbl}"
            ).fetchone()
            if row and row["latest"]:
                print(f"\n  {tbl}: latest={row['latest']}, distinct codes={row['codes']}")
        except Exception:
            pass

    print()


def top_holders(conn: sqlite3.Connection, stock_code: str, limit: int = 10) -> list:
    """Top ETF holders of a stock by weight."""
    rows = conn.execute(
        """
        SELECT h.etf_code, h.etf_name, h.holding_weight_pct, h.holding_shares,
               h.snapshot_date, p.aum_billion, p.category
        FROM stock_etf_holdings h
        LEFT JOIN etf_profile p ON h.etf_code = p.etf_code
        WHERE h.stock_code = ?
          AND h.snapshot_date = (
              SELECT MAX(snapshot_date) FROM stock_etf_holdings
              WHERE stock_code = ? AND etf_code = h.etf_code
          )
        ORDER BY h.holding_weight_pct DESC NULLS LAST
        LIMIT ?
        """,
        (stock_code, stock_code, limit),
    ).fetchall()
    return rows_to_dicts(rows)


def export_to_xlsx(
    conn: sqlite3.Connection,
    etf_code: str,
    output_path: Optional[str] = None,
) -> str:
    """Export ETF data to an xlsx file with multiple sheets. Returns the output path."""
    try:
        import openpyxl
    except ImportError:
        print("ERROR: openpyxl not installed. Run: pip install openpyxl", file=sys.stderr)
        sys.exit(1)

    try:
        import pandas as pd
    except ImportError:
        print("ERROR: pandas not installed. Run: pip install pandas", file=sys.stderr)
        sys.exit(1)

    if output_path is None:
        output_path = f"etf_{etf_code}.xlsx"

    profile = get_etf_profile(conn, etf_code)
    holdings = etf_top_holdings(conn, etf_code, limit=100)

    # Market snapshot history
    snap_rows = conn.execute(
        """
        SELECT snapshot_date, close_price, nav, premium_discount_pct,
               volume_k, ytd_return, one_year_return
        FROM etf_market_snapshot
        WHERE etf_code = ?
        ORDER BY snapshot_date DESC
        """,
        (etf_code,),
    ).fetchall()
    snapshots = rows_to_dicts(snap_rows)

    # Weight history
    wh_rows = conn.execute(
        """
        SELECT record_date, stock_code, weight_pct
        FROM etf_stock_weight_history
        WHERE etf_code = ?
        ORDER BY record_date DESC, weight_pct DESC
        LIMIT 500
        """,
        (etf_code,),
    ).fetchall()
    weight_history = rows_to_dicts(wh_rows)

    with pd.ExcelWriter(output_path, engine="openpyxl") as writer:
        # Sheet 1: Profile
        if profile:
            # Remove raw_json from display
            display_profile = {k: v for k, v in profile.items() if k != "raw_json"}
            pd.DataFrame([display_profile]).T.to_excel(
                writer, sheet_name="Profile", header=False
            )

        # Sheet 2: Holdings
        if holdings:
            pd.DataFrame(holdings).to_excel(writer, sheet_name="Holdings", index=False)

        # Sheet 3: Market snapshots
        if snapshots:
            pd.DataFrame(snapshots).to_excel(writer, sheet_name="Market Snapshots", index=False)

        # Sheet 4: Weight history
        if weight_history:
            pd.DataFrame(weight_history).to_excel(
                writer, sheet_name="Weight History", index=False
            )

    print(f"Exported {etf_code} to {output_path}")
    return output_path


# ---------------------------------------------------------------------------
# CLI helpers
# ---------------------------------------------------------------------------

def _print_profile(profile: dict) -> None:
    """Pretty-print an ETF profile dict."""
    skip = {"raw_json"}
    label_map = {
        "etf_code": "ETF Code",
        "etf_name": "Name",
        "issuer": "Issuer",
        "category": "Category",
        "etf_type": "Type",
        "region": "Region",
        "leverage_type": "Leverage",
        "dividend_type": "Dividend",
        "tracking_index": "Tracking Index",
        "currency": "Currency",
        "inception_date": "Inception Date",
        "expense_ratio": "Expense Ratio (%)",
        "aum_billion": "AUM (Billion TWD)",
        "beneficiary_count": "Beneficiaries",
        "fetched_at": "Data Fetched At",
        "close_price": "Close Price",
        "nav": "NAV",
        "premium_discount_pct": "Premium/Discount (%)",
        "volume_k": "Volume (K)",
        "ytd_return": "YTD Return (%)",
        "one_year_return": "1-Year Return (%)",
        "snapshot_date": "Snapshot Date",
    }
    print()
    print("=" * 55)
    for key, val in profile.items():
        if key in skip or val is None:
            continue
        label = label_map.get(key, key)
        print(f"  {label:<28} {val}")
    print("=" * 55)


# ---------------------------------------------------------------------------
# Main CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="ETF Local DB Query Interface",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--db", metavar="PATH", help="Override DB path")

    subparsers = parser.add_subparsers(dest="command", required=True)

    # etf <code>
    p_etf = subparsers.add_parser("etf", help="Show ETF profile")
    p_etf.add_argument("code", help="ETF code, e.g. 0050")

    # compare <code> [code ...]
    p_compare = subparsers.add_parser("compare", help="Compare multiple ETFs")
    p_compare.add_argument("codes", nargs="+", help="ETF codes to compare")

    # stock <code>
    p_stock = subparsers.add_parser("stock", help="Which ETFs hold a stock")
    p_stock.add_argument("code", help="Stock code, e.g. 2330")
    p_stock.add_argument("--min-weight", type=float, default=None, help="Minimum weight pct")

    # top-holders <code>
    p_top = subparsers.add_parser("top-holders", help="Top ETF holders of a stock")
    p_top.add_argument("code", help="Stock code")
    p_top.add_argument("--limit", type=int, default=10, help="Number of results (default: 10)")

    # holdings <etf_code>
    p_hold = subparsers.add_parser("holdings", help="ETF top holdings")
    p_hold.add_argument("code", help="ETF code")
    p_hold.add_argument("--limit", type=int, default=30, help="Number of holdings (default: 30)")

    # search <keyword>
    p_search = subparsers.add_parser("search", help="Search ETF by keyword")
    p_search.add_argument("keyword", help="Keyword to search")

    # stats
    subparsers.add_parser("stats", help="DB summary statistics")

    # export
    p_export = subparsers.add_parser("export", help="Export ETF data to xlsx")
    p_export.add_argument("--etf", required=True, metavar="CODE", help="ETF code to export")
    p_export.add_argument("--fmt", choices=["xlsx"], default="xlsx", help="Output format")
    p_export.add_argument("--out", metavar="FILE", default=None, help="Output file path")

    args = parser.parse_args()

    db_path = Path(args.db) if args.db else None
    conn = get_connection(db_path)

    # ---- etf ----
    if args.command == "etf":
        profile = get_etf_profile(conn, args.code)
        if not profile:
            print(f"ETF '{args.code}' not found in database.")
            sys.exit(1)
        _print_profile(profile)

    # ---- compare ----
    elif args.command == "compare":
        rows = compare_etfs(conn, args.codes)
        if not rows:
            print("No ETFs found for the given codes.")
            sys.exit(1)
        cols = [
            "etf_code", "etf_name", "category", "expense_ratio",
            "aum_billion", "ytd_return", "one_year_return", "tracking_index",
        ]
        print()
        print(_tabulate(rows, headers=cols))

    # ---- stock ----
    elif args.command == "stock":
        rows = stock_to_etfs(conn, args.code, min_weight=args.min_weight)
        if not rows:
            print(f"No ETF holdings found for stock '{args.code}'.")
            print("Run `python etl.py --holdings <code>` to fetch data for this stock.")
            sys.exit(0)
        cols = ["etf_code", "etf_name", "holding_weight_pct", "holding_shares",
                "category", "aum_billion", "snapshot_date"]
        print(f"\nETFs holding {args.code}:")
        print(_tabulate(rows, headers=cols))
        print(f"\nTotal: {len(rows)} ETF(s)")

    # ---- top-holders ----
    elif args.command == "top-holders":
        rows = top_holders(conn, args.code, limit=args.limit)
        if not rows:
            print(f"No holder data found for stock '{args.code}'.")
            sys.exit(0)
        cols = ["etf_code", "etf_name", "holding_weight_pct", "holding_shares",
                "aum_billion", "category", "snapshot_date"]
        print(f"\nTop {args.limit} ETF holders of stock {args.code}:")
        print(_tabulate(rows, headers=cols))

    # ---- holdings ----
    elif args.command == "holdings":
        rows = etf_top_holdings(conn, args.code, limit=args.limit)
        profile = get_etf_profile(conn, args.code)
        if profile:
            print(f"\nETF: {args.code}  {profile.get('etf_name', '')}")
        if not rows:
            print(f"No holdings data found for ETF '{args.code}'.")
            print("Run `python etl.py --etf-weight <code>` to fetch holdings for this ETF.")
            sys.exit(0)
        cols = ["stock_code", "stock_name", "weight_pct", "shares", "market_value",
                "industry", "snapshot_date"]
        print(f"\nTop {args.limit} holdings:")
        print(_tabulate(rows, headers=cols))

        # Weight sum
        total_w = sum(r.get("weight_pct") or 0 for r in rows)
        print(f"\n  Weight sum (top {len(rows)}): {total_w:.2f}%")

    # ---- search ----
    elif args.command == "search":
        rows = search_etf(conn, args.keyword)
        if not rows:
            print(f"No ETFs found matching '{args.keyword}'.")
            sys.exit(0)
        cols = ["etf_code", "etf_name", "issuer", "category", "expense_ratio",
                "aum_billion", "region"]
        print(f"\nSearch results for '{args.keyword}':")
        print(_tabulate(rows, headers=cols))
        print(f"\nTotal: {len(rows)} ETF(s)")

    # ---- stats ----
    elif args.command == "stats":
        db_stats(conn)

    # ---- export ----
    elif args.command == "export":
        export_to_xlsx(conn, args.etf, output_path=args.out)

    conn.close()


if __name__ == "__main__":
    main()
