# tv_backtest_scraper_intraday_slot.py — 全自動版
import asyncio
import csv
import re
from datetime import datetime
from pathlib import Path
from playwright.async_api import async_playwright

# ========== 設定 ==========
FORMAT_FILE = Path("format.csv")
OUTPUT_DIR  = Path("csv")
DEBUG_DIR   = Path("debug")
CHART_URL   = "https://jp.tradingview.com/chart/"
USER_DATA   = r"C:\Temp\tv-profile-pw"   # セッション保存先
WAIT_RECALC = 4      # 銘柄切替後の最低待機（秒）
WAIT_SEARCH = 1.5    # 検索ダイアログ安定待機（秒）
EXCHANGE    = "TSE"

# --- Strategy Tester 安定化取得の設定 ---
ST_POLL_INTERVAL_MS = 1000  # ポーリング間隔
ST_MAX_ATTEMPTS = 12        # 最大ポーリング回数
ST_STABLE_CHECKS = 2        # 連続同一値の回数で「安定」

# --- Data Window 安定化取得の設定 ---
DW_INITIAL_WAIT_MS = 1500   # Endキー押下後の初回待機
DW_POLL_INTERVAL_MS = 700   # ポーリング間隔
DW_MAX_ATTEMPTS = 10        # 最大ポーリング回数
DW_STABLE_CHECKS = 2        # 連続同一値の回数で「安定」
DW_EMPTY_MIN_ATTEMPTS = 4   # 空結果を「安定」とみなす最低試行回数

READ_RETRIES = 2            # 整合性NG時の再読み取り回数
# ==========================

def load_symbols() -> list[tuple[str, str]]:
    with open(FORMAT_FILE, encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        return [
            (row["銘柄コード"].strip(), row.get("銘柄名", "").strip())
            for row in reader if row.get("銘柄コード", "").strip()
        ]

def save_csv(strategy_data: list, dw_data: list, symbol: str, symbol_name: str = ""):
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r'[\\/:*?"<>|]', "_", symbol)
    fn = OUTPUT_DIR / f"{safe}_{datetime.now():%Y%m%d}.csv"
    with open(fn, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["銘柄", symbol])
        w.writerow(["銘柄名", symbol_name])
        w.writerow(["=== Strategy Tester ===", ""])
        w.writerow(["指標", "値"])
        w.writerows(strategy_data)
        if not strategy_data:
            w.writerow(["備考", "トレード0件(データ不足: IPO直後等)"])
        w.writerow(["=== Data Window ===", ""])
        w.writerow(["指標", "値"])
        w.writerows(dw_data)
    print(f"  ✓ 保存 → {fn}")

async def switch_symbol(page, symbol: str):
    await page.keyboard.press("Space")
    await page.wait_for_timeout(int(WAIT_SEARCH * 1000))
    query = f"{EXCHANGE}:{symbol}"
    await page.keyboard.type(query, delay=80)
    await page.wait_for_timeout(1200)
    await page.keyboard.press("ArrowDown")
    await page.wait_for_timeout(300)
    await page.keyboard.press("Enter")

async def scrape_strategy(page) -> list:
    keywords = [
        "総損益", "最大ドローダウン",
        "プロフィットファクター"
    ]
    return await page.evaluate("""
    (keywords) => {
        const texts = [];
        const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
        let node;
        while (node = walker.nextNode()) {
            const t = node.textContent.trim();
            if (t) texts.push(t);
        }
        const result = [];
        const seen = new Set();
        const isNumericVal = v => /^[+\\-−]?[\\d,\\.]+(%|円)?$/.test(v) || v === '∅' || v === '−' || v === '-' || /^[+\\-−]?[\\d,\\.]+$/.test(v.replace(/[,]/g,''));
        for (let i = 0; i < texts.length - 1; i++) {
            const key = texts[i];
            if (!keywords.some(k => key.includes(k))) continue;
            const val = texts[i + 1];
            if (!isNumericVal(val)) continue;
            if (!seen.has(key)) {
                seen.add(key);
                result.push([key, val]);
            }
        }
        return result;
    }
    """, keywords)

async def wait_for_chart_update(page, symbol: str, prev_strategy: list) -> list:
    """
    銘柄切替の反映待ち + Strategy Tester 値の安定取得。

    タイトル変化だけでは Strategy Tester の再計算完了を保証できず、
    前銘柄の値をそのまま読む事故があった(5802/5803/6594 が同値)。
    → Data Window 銘柄ラベル出現を待ち、Strategy Tester が
      「前銘柄と異なる値で2回連続一致」するまでポーリングする。
    """
    try:
        await page.wait_for_function(
            f"document.title.includes('{symbol}')",
            timeout=12000
        )
    except Exception:
        print("  ⚠ タイトル変化タイムアウト → 継続")

    try:
        await page.wait_for_function(
            f"document.body.innerText.includes('{symbol} · 30 · {EXCHANGE}')",
            timeout=10000
        )
    except Exception:
        print("  ⚠ Data Window 銘柄ラベル未検出 → 継続")

    await page.wait_for_timeout(int(WAIT_RECALC * 1000))

    prev = None
    stable = 0
    cur = []
    for _ in range(ST_MAX_ATTEMPTS):
        cur = await scrape_strategy(page)
        stable = stable + 1 if (prev is not None and cur == prev) else 0
        prev = cur
        # 前銘柄と同値のうちは未更新の疑い → 安定扱いにしない
        is_stale = bool(prev_strategy) and cur == prev_strategy
        if stable >= ST_STABLE_CHECKS - 1 and not is_stale:
            return cur
        await page.wait_for_timeout(ST_POLL_INTERVAL_MS)

    if prev_strategy and cur == prev_strategy:
        print("  ⚠ Strategy Tester が前銘柄と同値のまま → 未更新の可能性あり")
    return cur

# Pine側のデータウィンドウplotタイトル(30日勝率%×11 + 30日平均変化%×11 + 損益×11 + PF×11 = 44本)と一致させる必要あり
DW_ANCHOR_KEYS = [
    # 30日ローリング 勝率%
    "30d_0900-0930 勝率%", "30d_0930-1000 勝率%", "30d_1000-1030 勝率%", "30d_1030-1100 勝率%",
    "30d_1100-1130 勝率%", "30d_1230-1300 勝率%", "30d_1300-1330 勝率%", "30d_1330-1400 勝率%",
    "30d_1400-1430 勝率%", "30d_1430-1500 勝率%", "30d_1500-1530 勝率%",
    # 30日ローリング 平均変化%
    "30d_0900-0930 平均変化%", "30d_0930-1000 平均変化%", "30d_1000-1030 平均変化%", "30d_1030-1100 平均変化%",
    "30d_1100-1130 平均変化%", "30d_1230-1300 平均変化%", "30d_1300-1330 平均変化%", "30d_1330-1400 平均変化%",
    "30d_1400-1430 平均変化%", "30d_1430-1500 平均変化%", "30d_1500-1530 平均変化%",
    # 時間帯別 損益
    "pl_0900-0930 損益", "pl_0930-1000 損益", "pl_1000-1030 損益", "pl_1030-1100 損益",
    "pl_1100-1130 損益", "pl_1230-1300 損益", "pl_1300-1330 損益", "pl_1330-1400 損益",
    "pl_1400-1430 損益", "pl_1430-1500 損益", "pl_1500-1530 損益",
    # 時間帯別 プロフィットファクター
    "pf_0900-0930 PF", "pf_0930-1000 PF", "pf_1000-1030 PF", "pf_1030-1100 PF",
    "pf_1100-1130 PF", "pf_1230-1300 PF", "pf_1300-1330 PF", "pf_1330-1400 PF",
    "pf_1400-1430 PF", "pf_1430-1500 PF", "pf_1500-1530 PF",
]

async def _read_data_window_raw(page, anchor_keys=DW_ANCHOR_KEYS) -> dict:
    """
    ページ全体のテキストノードから、既知ラベル(anchor_keys)に完全一致するノードを探し、
    直後のノードを値として取る。コンテナ要素のクラス名には依存しない。
    位置ベースのペアリング(texts[i], texts[i+1] 機械的ペア)は廃止。
    アンカー0件の場合は空リストを返す(ゴミ行を出力しない)。
    """
    return await page.evaluate("""
    (anchorKeys) => {
        const texts = [];
        const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
        let node;
        while (node = walker.nextNode()) {
            const t = node.textContent.trim();
            if (t) texts.push(t);
        }
        const keySet = new Set(anchorKeys);
        const items = [];
        const seen = new Set();
        for (let i = 0; i < texts.length - 1; i++) {
            const label = texts[i];
            if (keySet.has(label) && !seen.has(label)) {
                seen.add(label);
                items.push([label, texts[i + 1]]);
            }
        }
        return { items, total: texts.length, has30d: document.body.innerHTML.includes('30d_') };
    }
    """, anchor_keys)

async def dump_debug(page, symbol: str):
    """アンカー未検出時の診断出力。debug/ 配下にテキストを保存する。"""
    DEBUG_DIR.mkdir(parents=True, exist_ok=True)
    info = await page.evaluate("""
    () => {
        const texts = [];
        const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
        let node;
        while (node = walker.nextNode()) {
            const t = node.textContent.trim();
            if (t) texts.push(t);
        }
        const idx = texts.findIndex(t => t.includes('時間帯別戦略'));
        return {
            has30d: document.body.innerHTML.includes('30d_'),
            total: texts.length,
            around: idx >= 0 ? texts.slice(Math.max(0, idx - 5), idx + 40) : [],
        };
    }
    """)
    fn = DEBUG_DIR / f"dw_{symbol}.txt"
    with open(fn, "w", encoding="utf-8") as f:
        f.write(f"has30d_in_DOM: {info['has30d']}\n")
        f.write(f"text_nodes: {info['total']}\n")
        f.write("--- 時間帯別戦略 周辺 ---\n")
        for t in info["around"]:
            f.write(t + "\n")
    print(f"  ⚠ 診断出力 → {fn} (30d_ がDOMに存在: {info['has30d']})")

async def scrape_data_window(page, symbol: str, press_end: bool = True) -> list:
    """
    Endキーで最終バーへ移動 → Data Windowを取得。
    「2回連続で同じ値」になるまでポーリングして確定する。
    アンカー未検出のまま終了した場合は診断ファイルを出力する。
    """
    if press_end:
        await page.keyboard.press("End")
    await page.wait_for_timeout(DW_INITIAL_WAIT_MS)

    prev_items = None
    stable_count = 0

    for attempt in range(1, DW_MAX_ATTEMPTS + 1):
        result = await _read_data_window_raw(page)
        items = result["items"]

        stable_count = stable_count + 1 if (prev_items is not None and items == prev_items) else 0
        prev_items = items

        empty_ok = bool(items) or attempt >= DW_EMPTY_MIN_ATTEMPTS
        if stable_count >= DW_STABLE_CHECKS - 1 and empty_ok:
            print(f"  データウィンドウ: anchored / {len(items)}行 (安定確認 attempt={attempt})")
            if not items:
                await dump_debug(page, symbol)
            return items

        await page.wait_for_timeout(DW_POLL_INTERVAL_MS)

    print(f"  ⚠ データウィンドウ 値が安定しないまま最大試行回数に到達 ({len(prev_items or [])}行) → 最終値を使用")
    if not prev_items:
        await dump_debug(page, symbol)
    return prev_items or []

def _to_number(v: str) -> float:
    if v is None:
        return 0.0
    v = str(v).strip().replace(",", "").replace("∅", "0").replace("円", "").replace("%", "")
    if v in ("−", "-", ""):
        return 0.0  # 決済トレード0件 = PF/勝率 算出不可
    v = v.replace("−", "-")
    try:
        return float(v)
    except ValueError:
        return 0.0

def is_dw_consistent(strategy_data: list, dw_data: list) -> bool:
    """総損益が非ゼロなのにdw_dataが空なら未反映の疑いとみなす簡易版。"""
    strategy = dict(strategy_data)
    net_profit = _to_number(strategy.get("総損益", "0"))
    if net_profit != 0 and len(dw_data) == 0:
        return False
    return True

async def main():
    symbols = load_symbols()
    print(f"対象 {len(symbols)} 銘柄: {symbols}\n")

    async with async_playwright() as p:
        browser = await p.chromium.launch_persistent_context(
            user_data_dir=USER_DATA,
            headless=False,
            args=["--start-maximized"],
            no_viewport=True,
        )
        page = browser.pages[0] if browser.pages else await browser.new_page()

        await page.goto(CHART_URL)

        print("=" * 50)
        print("【初回のみ】ログイン + チャート設定が必要")
        print("2回目以降はそのままEnterでOK")
        print("  1. TradingViewにログイン")
        print("  2. ストラテジー適用済みチャートを開く")
        print("  3. Strategy Testerパネルを表示")
        print("  4. データウィンドウを開く")
        print("=" * 50)
        input("準備完了 → Enter: ")

        success, failed = [], []
        prev_strategy: list = []

        for i, (symbol, symbol_name) in enumerate(symbols, 1):
            print(f"\n[{i}/{len(symbols)}] {symbol} {symbol_name}")
            try:
                await switch_symbol(page, symbol)
                strategy_data = await wait_for_chart_update(page, symbol, prev_strategy)
                dw_data       = await scrape_data_window(page, symbol)

                retry_count = 0
                while not is_dw_consistent(strategy_data, dw_data) and retry_count < READ_RETRIES:
                    retry_count += 1
                    print(f"  ⚠ 整合性チェック失敗（総損益あり/データ空）→ 再読み取り {retry_count}/{READ_RETRIES}")
                    await page.wait_for_timeout(2000)
                    strategy_data = await scrape_strategy(page)
                    dw_data       = await scrape_data_window(page, symbol, press_end=False)

                if not is_dw_consistent(strategy_data, dw_data):
                    print("  ✗ 再取得後も整合性NG → 手動確認が必要")

                if not strategy_data:
                    print("  ℹ Strategy Tester 値なし → トレード0件(IPO直後等)として記録")

                prev_strategy = strategy_data

                if strategy_data or dw_data:
                    save_csv(strategy_data, dw_data, symbol, symbol_name)
                    success.append(symbol)
                else:
                    # 両方空でも備考付きで保存(IPO等の欠損を明示)
                    save_csv(strategy_data, dw_data, symbol, symbol_name)
                    success.append(symbol)

            except Exception as e:
                print(f"  ✗ エラー: {e}")
                failed.append(symbol)

        print(f"\n{'='*50}")
        print(f"完了: 成功 {len(success)} / 失敗 {len(failed)}")
        if failed:
            print(f"失敗銘柄: {failed}")

        input("ブラウザを閉じる → Enter: ")
        await browser.close()

if __name__ == "__main__":
    asyncio.run(main())
