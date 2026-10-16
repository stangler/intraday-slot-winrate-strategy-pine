# merge_intraday_slot_csv.py
"""
csv/ 配下の全銘柄CSV(tv_backtest_scraper_intraday_slot.pyの出力)を
1つのCSVに横持ちで統合する。

入力: csv/*.csv (各銘柄1ファイル、Strategy Tester + Data Windowのセクション形式)
出力: csv/merged_all_symbols.csv (銘柄ごとに1行、全指標を列展開)

Pine変更後のData Windowキー:
  30d_XXXX-XXXX 勝率%     × 11区間
  30d_XXXX-XXXX 平均変化% × 11区間
  pl_XXXX-XXXX 損益       × 11区間
  pf_XXXX-XXXX PF         × 11区間
"""
import csv
import re
from pathlib import Path

CSV_DIR = Path("csv")
OUTPUT_FILE = CSV_DIR / "merged_all_symbols.csv"

FILENAME_PATTERN = re.compile(r"^(.+?)_(\d{8})\.csv$")

# 列の優先順序（存在するキーのみ出力に使う）
PREFERRED_ORDER = [
    "銘柄",
    "銘柄名",
    # Strategy Tester
    "総損益",
    "最大ドローダウン",
    "プロフィットファクター",
    # 30日 勝率%
    "30d_0900-0930 勝率%",
    "30d_0930-1000 勝率%",
    "30d_1000-1030 勝率%",
    "30d_1030-1100 勝率%",
    "30d_1100-1130 勝率%",
    "30d_1230-1300 勝率%",
    "30d_1300-1330 勝率%",
    "30d_1330-1400 勝率%",
    "30d_1400-1430 勝率%",
    "30d_1430-1500 勝率%",
    "30d_1500-1530 勝率%",
    # 30日 平均変化%
    "30d_0900-0930 平均変化%",
    "30d_0930-1000 平均変化%",
    "30d_1000-1030 平均変化%",
    "30d_1030-1100 平均変化%",
    "30d_1100-1130 平均変化%",
    "30d_1230-1300 平均変化%",
    "30d_1300-1330 平均変化%",
    "30d_1330-1400 平均変化%",
    "30d_1400-1430 平均変化%",
    "30d_1430-1500 平均変化%",
    "30d_1500-1530 平均変化%",
    # 時間帯別 損益
    "pl_0900-0930 損益",
    "pl_0930-1000 損益",
    "pl_1000-1030 損益",
    "pl_1030-1100 損益",
    "pl_1100-1130 損益",
    "pl_1230-1300 損益",
    "pl_1300-1330 損益",
    "pl_1330-1400 損益",
    "pl_1400-1430 損益",
    "pl_1430-1500 損益",
    "pl_1500-1530 損益",
    # 時間帯別 PF
    "pf_0900-0930 PF",
    "pf_0930-1000 PF",
    "pf_1000-1030 PF",
    "pf_1030-1100 PF",
    "pf_1100-1130 PF",
    "pf_1230-1300 PF",
    "pf_1300-1330 PF",
    "pf_1330-1400 PF",
    "pf_1400-1430 PF",
    "pf_1430-1500 PF",
    "pf_1500-1530 PF",
]


def parse_symbol_csv(path: Path) -> dict:
    with open(path, encoding="utf-8-sig", newline="") as f:
        rows = list(csv.reader(f))

    result = {}
    symbol = rows[0][1] if len(rows) > 0 and len(rows[0]) > 1 else path.stem
    symbol_name = (
        rows[1][1]
        if len(rows) > 1 and len(rows[1]) > 1 and rows[1][0] == "銘柄名"
        else ""
    )

    section = None
    for row in rows[2:]:
        if not row:
            continue
        if row[0].startswith("=== Strategy Tester"):
            section = "strategy"
            continue
        if row[0].startswith("=== Data Window"):
            section = "dw"
            continue
        if row[0] == "指標":
            continue
        if len(row) >= 2 and section in ("strategy", "dw"):
            key, val = row[0], row[1]
            result[key] = val

    return {"銘柄": symbol, "銘柄名": symbol_name, **result}


def build_fieldnames(all_rows: list) -> list:
    """PREFERRED_ORDER を優先しつつ、出現した全キーを漏れなく含める。"""
    seen = set()
    fieldnames = []

    for k in PREFERRED_ORDER:
        if any(k in row for row in all_rows):
            fieldnames.append(k)
            seen.add(k)

    for row in all_rows:
        for k in row.keys():
            if k not in seen:
                fieldnames.append(k)
                seen.add(k)

    return fieldnames


def main():
    files = sorted(p for p in CSV_DIR.glob("*.csv") if FILENAME_PATTERN.match(p.name))
    print(f"対象ファイル {len(files)}件")

    if not files:
        print("✗ csv/ に銘柄CSVが見つからない")
        return

    all_rows = []
    for f in files:
        data = parse_symbol_csv(f)
        all_rows.append(data)
        print(f"  ✓ {f.name} → 銘柄 {data.get('銘柄')} {data.get('銘柄名')}")

    fieldnames = build_fieldnames(all_rows)

    with open(OUTPUT_FILE, "w", newline="", encoding="utf-8-sig") as out_f:
        writer = csv.DictWriter(out_f, fieldnames=fieldnames, restval="")
        writer.writeheader()
        writer.writerows(all_rows)

    print(f"\n合計 {len(all_rows)}銘柄 → {OUTPUT_FILE} に保存")
    print(f"列数: {len(fieldnames)}")


if __name__ == "__main__":
    main()
