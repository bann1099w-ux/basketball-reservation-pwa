#!/usr/bin/env python3
"""
空き枠監視 + キャンセル取り自動申込スクリプト

動作フロー:
  1. Playwright でログイン（Incapsula通過 + セッション確立）
  2. 各施設の空き状況ページに遷移し、画面DOMから空き枠を検出
  3. ○（利用可能）を検出 → ブラウザ操作で先着申込
  4. 結果をログ + 通知

方式: API(U03)では空き判定不可のため、ブラウザ画面の表示状態を直接スクレイピング

凡例（harpシステム）:
  ○ = 利用可能（先着申込可能）
  ◎ = 抽選待ち
  ◇ = 抽選申込可
  「前」= 受付前（先着受付開始前）
  × = 空きなし / 利用不可

使い方:
  python3 空き監視.py                    # 両アカウント、ループ監視
  python3 空き監視.py --once             # 1回だけチェック（テスト用）
  python3 空き監視.py --account 1        # アカウント1のみ
  python3 空き監視.py --dry-run          # 検出のみ（申込しない）

注意:
  - 3/26〜利用日まで先着申込可能（4月分）
  - サーバー負荷を考慮し、最低5分間隔を推奨
"""

import asyncio
import json
import os
import sys
import time
from datetime import datetime, timezone, timedelta, date
from pathlib import Path
from calendar import monthrange

from playwright.async_api import async_playwright

# === 定数 ===
BASE_DIR = Path(__file__).parent
PROJECT_DIR = BASE_DIR.parent.parent
RECON_DIR = PROJECT_DIR / "ツール" / "harp偵察"
CONFIG_FILE = BASE_DIR / "設定.json"
LOG_DIR = BASE_DIR / "logs"
ENV_FILE = RECON_DIR / ".env"
FOUND_SLOTS_FILE = LOG_DIR / "found_slots.json"

SITE_URL = "https://yoyaku.harp.lg.jp"
LGC = "011002"
JST = timezone(timedelta(hours=9))

WEEKDAY_NAMES = ["月", "火", "水", "木", "金", "土", "日"]


def now_jst():
    return datetime.now(JST)


def load_config():
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def load_env():
    env_data = {}
    with open(ENV_FILE, "r") as f:
        for line in f:
            line = line.strip()
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                env_data[k.strip()] = v.strip()
    return env_data


def resolve_accounts(config, env_data, account_filter=None):
    accounts = []
    for i, acct_cfg in enumerate(config["accounts"], 1):
        if account_filter and i != account_filter:
            continue
        acct_id = env_data.get(acct_cfg["id_env"], "")
        acct_pw = env_data.get(acct_cfg["pw_env"], "")
        if acct_id and acct_pw:
            accounts.append({
                "id": acct_id, "pw": acct_pw,
                "label": acct_cfg.get("label", f"#{i}"),
                "num": i,
            })
    return accounts


def log(msg, level="INFO"):
    ts = now_jst().strftime("%H:%M:%S")
    print(f"[{ts}] [{level}] {msg}")


def load_found_slots():
    if FOUND_SLOTS_FILE.exists():
        with open(FOUND_SLOTS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"applied": [], "seen": []}


def save_found_slots(data):
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    with open(FOUND_SLOTS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


class MonitorSession:
    """監視用セッション（Playwright常駐）"""

    def __init__(self):
        self.playwright = None
        self.browser = None
        self.context = None
        self.page = None
        self.logged_in = False

    async def start(self, account):
        """ブラウザ起動 + ログイン"""
        self.playwright = await async_playwright().start()
        self.browser = await self.playwright.chromium.launch(headless=True)
        self.context = await self.browser.new_context(
            viewport={"width": 375, "height": 812},
            user_agent="Mozilla/5.0 (Linux; Android 13; Pixel 7) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/120.0.6099.230 Mobile Safari/537.36",
            locale="ja-JP", timezone_id="Asia/Tokyo",
        )
        self.context.set_default_timeout(30000)
        await self.context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
            Object.defineProperty(navigator, 'plugins', {get: () => [1,2,3,4,5]});
            window.chrome = {runtime: {}};
        """)
        self.page = await self.context.new_page()

        # Incapsula通過
        log(f"Incapsulaチャレンジ通過中... ({account['label']})")
        await self.page.goto(f"{SITE_URL}/sapporo/", wait_until="networkidle")
        await asyncio.sleep(3)
        for attempt in range(5):
            content = await self.page.content()
            if "Incapsula" in content or "Request Timeout" in content:
                log(f"チャレンジ中... ({attempt+1}/5)")
                await asyncio.sleep(3)
                await self.page.reload(wait_until="networkidle")
            else:
                break

        # ログイン（API経由）
        log(f"ログイン中... ({account['label']})")
        await self.page.goto(f"{SITE_URL}/sapporo/Login", wait_until="networkidle")
        await asyncio.sleep(2)

        login_result = await self.page.evaluate("""async (params) => {
            try {
                const tokenInput = document.querySelector(
                    'input[name="__RequestVerificationToken"]'
                );
                const token = tokenInput ? tokenInput.value : '';
                const resp = await fetch('/sapporo/Login', {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json;charset=UTF-8',
                        'Accept': 'application/json, text/plain, */*',
                        'requestverificationtoken': token,
                    },
                    body: JSON.stringify({
                        sessionToken: '',
                        userId: params.id,
                        password: params.pw,
                    }),
                });
                return {ok: resp.ok, status: resp.status};
            } catch(e) {
                return {ok: false, error: e.message};
            }
        }""", {"id": account["id"], "pw": account["pw"]})

        if not login_result.get("ok"):
            log(f"ログイン失敗: {login_result}", "ERROR")
            return False

        await asyncio.sleep(2)
        await self.page.goto(f"{SITE_URL}/sapporo/RequestStatuses/Index",
                             wait_until="networkidle")
        await asyncio.sleep(1)

        if "/Login" in self.page.url:
            log("ログイン失敗（リダイレクト）", "ERROR")
            return False

        log(f"ログイン成功: {account['label']}")
        self.logged_in = True
        return True

    async def _dismiss_tutorial(self):
        """チュートリアルオーバーレイを閉じる"""
        for _ in range(10):
            clicked = await self.page.evaluate("""() => {
                const btns = document.querySelectorAll('button, a, .v-btn, span');
                for (const b of btns) {
                    const t = b.textContent.trim();
                    if (t === 'スキップ' || t === '閉じる') {
                        b.click();
                        return t;
                    }
                }
                return null;
            }""")
            if not clicked:
                break
            await asyncio.sleep(0.3)

    async def _wait_vue(self):
        """Vue.js レンダリング待機"""
        try:
            await self.page.wait_for_function(
                "() => document.querySelector('#app') && "
                "!document.querySelector('#app').hasAttribute('v-cloak')",
                timeout=10000,
            )
        except Exception:
            pass
        await asyncio.sleep(2)

    async def check_facility_day(self, facility_code, facility_name, date_str):
        """
        施設の日別空き状況をブラウザ画面から読み取る。
        ○（利用可能）のセルがあれば空き枠として返す。
        """
        purpose_key = "71"  # ミニバスケットボール
        url = (
            f"{SITE_URL}/sapporo/FacilityAvailability/Index/{LGC}/{facility_code}"
            f"?u%5B0%5D={purpose_key}&u%5B1%5D=69"
            f"&pt%5B0%5D=0&pt%5B1%5D=1&pt%5B2%5D=2"
            f"&ptn=2&d={date_str}&sd={date_str}&ed={date_str}"
        )

        await self.page.goto(url, wait_until="networkidle")
        await asyncio.sleep(3)
        await self._wait_vue()
        await self._dismiss_tutorial()
        await asyncio.sleep(1)

        # 画面DOMからステータスを読み取る
        slots = await self.page.evaluate("""() => {
            const results = [];

            // 方法1: テキストベースで○を探す
            const allElements = document.querySelectorAll('td, div, span, .v-btn');
            for (const el of allElements) {
                const text = el.textContent.trim();
                // ○ = 利用可能、◎ = 抽選待ち、◇ = 抽選申込可
                if (text === '○' || text === '◎') {
                    // 親要素からコート名・時間帯を推定
                    let parent = el.closest('tr') || el.parentElement;
                    let context = parent ? parent.textContent.trim().substring(0, 100) : '';
                    results.push({
                        symbol: text,
                        context: context,
                        available: text === '○',
                    });
                }
            }

            // 方法2: aria属性やclass名でステータスセルを探す
            const statusCells = document.querySelectorAll(
                '[class*="available"], [class*="vacant"], [class*="reservable"],' +
                '[aria-label*="利用可能"], [aria-label*="空き"]'
            );
            for (const cell of statusCells) {
                results.push({
                    symbol: '○',
                    context: cell.getAttribute('aria-label') || cell.className,
                    available: true,
                    source: 'class/aria',
                });
            }

            // 方法3: Vuetifyのチップやバッジでステータスを探す
            const chips = document.querySelectorAll('.v-chip, .status-badge');
            for (const chip of chips) {
                const text = chip.textContent.trim();
                if (text.includes('利用可') || text.includes('予約可') || text === '○') {
                    results.push({
                        symbol: '○',
                        context: text,
                        available: true,
                        source: 'chip',
                    });
                }
            }

            // 方法4: 受付前/空きなし等のステータスも記録（デバッグ用）
            const statusTexts = [];
            const allText = document.querySelectorAll('*');
            for (const el of allText) {
                if (el.children.length === 0) {
                    const t = el.textContent.trim();
                    if (t === '前' || t === '受付前' || t === '×' || t === '○' ||
                        t === '◎' || t === '◇' || t.includes('利用可') ||
                        t.includes('空きなし') || t.includes('予約済')) {
                        statusTexts.push(t);
                    }
                }
            }

            return {
                available_slots: results.filter(r => r.available),
                all_slots: results,
                status_texts: [...new Set(statusTexts)],
            };
        }""")

        return slots

    async def apply_spot_via_ui(self, facility_code, facility_name, date_str):
        """
        先着申込: 空き枠セル（○）をクリックして予約手続きに進む
        前提: check_facility_day で○が検出された直後にこのページ上で実行
        """
        log(f"  先着申込開始: {facility_name} {date_str}")

        # ○セルをクリック
        click_result = await self.page.evaluate("""() => {
            const allElements = document.querySelectorAll('td, div, span, .v-btn, a');
            for (const el of allElements) {
                const text = el.textContent.trim();
                if (text === '○') {
                    el.click();
                    return {clicked: true, text: text};
                }
            }
            return {clicked: false};
        }""")

        if not click_result.get("clicked"):
            log("  ○セル見つからず", "WARN")
            return "cell_not_found"

        log(f"  ○セルクリック")
        await asyncio.sleep(3)
        await self._wait_vue()

        # 予約申込ボタンを探してクリック
        apply_result = await self.page.evaluate("""() => {
            const keywords = ['予約申込', '申込', '予約する', '次へ', '確認'];
            const btns = document.querySelectorAll(
                'button, .v-btn, input[type="submit"], a.v-btn'
            );
            for (const kw of keywords) {
                for (const btn of btns) {
                    const text = btn.textContent.trim();
                    if (text.includes(kw) && btn.offsetParent !== null &&
                        !btn.disabled) {
                        btn.click();
                        return {clicked: true, text: text};
                    }
                }
            }
            return {clicked: false};
        }""")

        if apply_result.get("clicked"):
            log(f"  申込ボタン: {apply_result['text']}")
        else:
            log("  申込ボタン見つからず", "WARN")
            ts = now_jst().strftime("%Y%m%d_%H%M%S")
            await self.page.screenshot(
                path=str(LOG_DIR / f"spot_nobtn_{facility_code}_{date_str}_{ts}.png")
            )
            return "btn_not_found"

        await asyncio.sleep(3)

        # 確認ダイアログ対応
        await self.page.evaluate("""() => {
            const dlg = document.querySelector(
                '.v-dialog--active, .v-overlay--active, .modal.show'
            );
            if (dlg) {
                const btns = dlg.querySelectorAll('button, .v-btn');
                for (const btn of btns) {
                    const text = btn.textContent.trim();
                    if (text.includes('はい') || text.includes('OK') ||
                        text.includes('確定') || text.includes('申込')) {
                        btn.click();
                        return true;
                    }
                }
            }
            return false;
        }""")

        await asyncio.sleep(3)

        # 結果スクリーンショット
        ts = now_jst().strftime("%Y%m%d_%H%M%S")
        await self.page.screenshot(
            path=str(LOG_DIR / f"spot_result_{facility_code}_{date_str}_{ts}.png")
        )

        # エラーチェック
        error = await self.page.evaluate("""() => {
            const alerts = document.querySelectorAll(
                '.v-alert, .error, .v-snack, [role="alert"]'
            );
            for (const el of alerts) {
                if (el.offsetParent !== null) return el.textContent.trim();
            }
            return null;
        }""")

        if error:
            log(f"  エラー: {error}", "ERROR")
            return "error"

        current_url = self.page.url
        if "RequestStatuses" in current_url or "Complete" in current_url:
            log("  申込成功（リダイレクト確認）")
            return "applied"

        log(f"  結果不明 (URL: {current_url})", "WARN")
        return "unknown"

    async def close(self):
        if self.browser:
            await self.browser.close()
        if self.playwright:
            await self.playwright.stop()


async def monitor_loop(config, accounts, dry_run=False, once=False):
    """メイン監視ループ"""
    mon_cfg = config.get("monitoring", {})
    interval = mon_cfg.get("interval_seconds", 300)
    target_month = mon_cfg.get("target_month_spot", "2026-04")
    max_applies = mon_cfg.get("max_spot_applications", 5)

    year, month = map(int, target_month.split("-"))
    weekdays = config.get("target_weekdays", [5, 6])
    _, days = monthrange(year, month)

    # 対象日リスト（今日以降 + 対象曜日）
    today = date.today()
    target_dates = []
    for day in range(1, days + 1):
        dt = date(year, month, day)
        if dt >= today and dt.weekday() in weekdays:
            target_dates.append(dt.strftime("%Y-%m-%d"))

    if not target_dates:
        log("対象日がありません（全て過去日か対象曜日なし）")
        return

    log(f"監視開始: {target_month}")
    log(f"対象日: {len(target_dates)}日 ({', '.join(target_dates[:5])}...)")
    log(f"対象施設: {len(config['target_facilities'])}校")
    log(f"ポーリング間隔: {interval}秒")
    log(f"モード: {'DRY RUN' if dry_run else '自動申込'}")

    found_history = load_found_slots()
    applied_count = 0
    cycle = 0

    for account in accounts:
        session = MonitorSession()
        try:
            if not await session.start(account):
                log(f"セッション開始失敗: {account['label']}", "ERROR")
                continue

            while True:
                cycle += 1
                log(f"\n--- スキャン #{cycle} ({account['label']}) ---")
                total_available = 0

                for facility in config["target_facilities"]:
                    fc = facility["code"]
                    fn = facility["name"]

                    for date_str in target_dates:
                        dt = datetime.strptime(date_str, "%Y-%m-%d")
                        wd = WEEKDAY_NAMES[dt.weekday()]

                        result = await session.check_facility_day(
                            fc, fn, date_str
                        )

                        available = result.get("available_slots", [])
                        status_texts = result.get("status_texts", [])

                        if available:
                            total_available += len(available)
                            log(f"  ★ {fn} {date_str}({wd}): "
                                f"空き{len(available)}枠 ★", "ALERT")

                            slot_key = f"{fc}_{date_str}"

                            # 重複チェック
                            if slot_key in [h.get("key")
                                            for h in found_history["applied"]]:
                                log(f"    → 申込済みスキップ")
                                continue

                            found_history["seen"].append({
                                "key": slot_key,
                                "timestamp": now_jst().isoformat(),
                                "facility": fn,
                                "date": date_str,
                            })

                            if dry_run:
                                log(f"    → [DRY RUN] 申込スキップ")
                            elif applied_count >= max_applies:
                                log(f"    → 申込上限到達 ({applied_count}/{max_applies})")
                            else:
                                result = await session.apply_spot_via_ui(
                                    fc, fn, date_str
                                )
                                if result == "applied":
                                    applied_count += 1
                                    found_history["applied"].append({
                                        "key": slot_key,
                                        "timestamp": now_jst().isoformat(),
                                        "account": account["label"],
                                        "facility": fn,
                                        "date": date_str,
                                    })
                                    log(f"    → 申込成功！ "
                                        f"({applied_count}/{max_applies})")
                                else:
                                    log(f"    → 申込結果: {result}")
                        else:
                            # ステータステキストを表示（デバッグ）
                            if status_texts:
                                st_str = ", ".join(status_texts[:3])
                                log(f"  {fn} {date_str}({wd}): {st_str}")

                        # サーバー負荷軽減（ページ遷移ごとに待機）
                        await asyncio.sleep(1)

                if total_available == 0:
                    log(f"空き枠なし（{len(config['target_facilities'])}校 × "
                        f"{len(target_dates)}日 完了）")

                save_found_slots(found_history)

                if once:
                    break
                if applied_count >= max_applies:
                    log(f"申込上限到達。監視終了。")
                    break

                log(f"次のスキャンまで {interval}秒待機...")
                await asyncio.sleep(interval)

        except KeyboardInterrupt:
            log("監視中断（Ctrl+C）")
        except Exception as e:
            log(f"エラー: {e}", "ERROR")
            import traceback
            traceback.print_exc()
        finally:
            await session.close()

    # 最終レポート
    log(f"\n{'='*50}")
    log(f"監視終了 — スキャン{cycle}回 / 申込{applied_count}件")

    ts = now_jst().strftime("%Y%m%d_%H%M%S")
    report_path = LOG_DIR / f"monitor_report_{ts}.json"
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump({
            "timestamp": now_jst().isoformat(),
            "cycles": cycle,
            "applied": applied_count,
            "found_slots": found_history,
        }, f, ensure_ascii=False, indent=2)
    log(f"レポート: {report_path}")


async def main():
    import argparse
    parser = argparse.ArgumentParser(description="空き枠監視 + キャンセル取り自動申込")
    parser.add_argument("--once", action="store_true", help="1回だけチェック")
    parser.add_argument("--dry-run", action="store_true", help="検出のみ（申込しない）")
    parser.add_argument("--account", type=int, help="アカウント番号 (1 or 2)")
    parser.add_argument("--interval", type=int, help="ポーリング間隔（秒）")
    args = parser.parse_args()

    config = load_config()
    env_data = load_env()

    if args.interval:
        config.setdefault("monitoring", {})["interval_seconds"] = args.interval

    accounts = resolve_accounts(config, env_data, args.account)
    if not accounts:
        print("エラー: アカウント情報が見つかりません")
        sys.exit(1)

    dry_run = args.dry_run or config.get("dry_run", True)

    await monitor_loop(config, accounts, dry_run=dry_run, once=args.once)


if __name__ == "__main__":
    asyncio.run(main())
