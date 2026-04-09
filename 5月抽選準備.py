#!/usr/bin/env python3
"""
5月分抽選申込 準備 + 実行スクリプト

スケジュール:
  4/20 09:00  抽選申込開始 → このスクリプトで自動申込
  4/22 深夜   自動抽選（操作不要）
  4/23 09:00  結果確認 + 利用申請（★最重要）→ --confirm で実行
  4/25       利用申請期限

使い方:
  python3 5月抽選準備.py --plan            # 申込計画表示（テスト）
  python3 5月抽選準備.py --check-status    # 現在の申込状況確認
  python3 5月抽選準備.py --apply           # 抽選申込実行（4/20〜22）
  python3 5月抽選準備.py --confirm         # 当選確認+利用申請（4/23〜25）
  python3 5月抽選準備.py --account 1       # アカウント指定

注意:
  - 同じ日時・複数校への抽選申込は禁止（警告→資格停止）
  - 申込回数上限: 屋内15回/月（全施設合計）
  - 当選後の利用申請忘れ = 当選無効（最大リスク）
"""

import asyncio
import json
import sys
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
PLAN_FILE = LOG_DIR / "5月抽選計画.json"

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


def generate_lottery_plan(config):
    """
    5月分の抽選申込計画を生成する。
    ルール: 同一日時に複数校申込禁止 → 施設を日付ごとに分散配置
    """
    target_month = config.get("lottery", {}).get("target_month", config["target_month"])
    year, month = map(int, target_month.split("-"))
    weekdays = config["target_weekdays"]
    facilities = config["target_facilities"]
    max_per_account = config["max_applications_per_account"]

    _, days = monthrange(year, month)

    # 対象日リスト
    target_dates = []
    for day in range(1, days + 1):
        dt = date(year, month, day)
        if dt.weekday() in weekdays:
            target_dates.append(dt)

    # 施設を日付に分散配置（同一日時に1校のみ）
    # 戦略: 各日付に最も優先度の高い施設から順にアサイン
    plan = []
    facility_cycle = list(range(len(facilities)))

    for i, dt in enumerate(target_dates):
        if len(plan) >= max_per_account:
            break

        # ラウンドロビンで施設をローテーション
        fi = facility_cycle[i % len(facility_cycle)]
        facility = facilities[fi]

        plan.append({
            "date": dt.strftime("%Y-%m-%d"),
            "weekday": WEEKDAY_NAMES[dt.weekday()],
            "facility_code": facility["code"],
            "facility_name": facility["name"],
            "court_preference": config.get("court_preference", "half_a"),
            "time_slot": config.get("preferred_time_slot", "夜間"),
        })

    return {
        "target_month": target_month,
        "total_dates": len(target_dates),
        "planned_applications": len(plan),
        "max_per_account": max_per_account,
        "plan": plan,
    }


async def browser_login(account):
    """Playwright でログインしてページオブジェクトを返す"""
    pw = await async_playwright().start()
    browser = await pw.chromium.launch(headless=True)
    context = await browser.new_context(
        viewport={"width": 375, "height": 812},
        user_agent="Mozilla/5.0 (Linux; Android 13; Pixel 7) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/120.0.6099.230 Mobile Safari/537.36",
        locale="ja-JP", timezone_id="Asia/Tokyo",
    )
    context.set_default_timeout(30000)

    await context.add_init_script("""
        Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
        Object.defineProperty(navigator, 'plugins', {get: () => [1,2,3,4,5]});
        window.chrome = {runtime: {}};
    """)

    page = await context.new_page()

    # Incapsula通過
    log(f"Incapsulaチャレンジ通過中...")
    await page.goto(f"{SITE_URL}/sapporo/", wait_until="networkidle")
    await asyncio.sleep(3)
    for attempt in range(5):
        content = await page.content()
        if "Incapsula" in content or "Request Timeout" in content:
            await asyncio.sleep(3)
            await page.reload(wait_until="networkidle")
        else:
            break

    # ログイン
    log(f"ログイン中... ({account['label']})")
    await page.goto(f"{SITE_URL}/sapporo/Login", wait_until="networkidle")
    await asyncio.sleep(2)

    result = await page.evaluate("""async (params) => {
        try {
            const token = document.querySelector(
                'input[name="__RequestVerificationToken"]'
            )?.value || '';
            const resp = await fetch('/sapporo/Login', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json;charset=UTF-8',
                    'requestverificationtoken': token,
                },
                body: JSON.stringify({
                    sessionToken: '', userId: params.id, password: params.pw,
                }),
            });
            return {ok: resp.ok, status: resp.status};
        } catch(e) {
            return {ok: false, error: e.message};
        }
    }""", {"id": account["id"], "pw": account["pw"]})

    await asyncio.sleep(2)
    await page.goto(f"{SITE_URL}/sapporo/RequestStatuses/Index",
                    wait_until="networkidle")
    await asyncio.sleep(1)

    if "/Login" in page.url:
        log("ログイン失敗", "ERROR")
        await browser.close()
        await pw.stop()
        return None, None, None

    log("ログイン成功")
    return pw, browser, page


async def get_current_l03_count(page):
    """現在の抽選待ち(L03)件数を取得"""
    result = await page.evaluate("""async () => {
        const resp = await fetch('/sapporo/RequestStatuses/Search', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({t:2, so:1, k:null, st:"", ym:null, p:1, s:200})
        });
        return await resp.json();
    }""")

    data = result.get("data", [])
    l03 = [r for r in data if r.get("st") == "L03"]
    return len(l03), data


async def cmd_plan(config):
    """申込計画を表示"""
    plan_data = generate_lottery_plan(config)

    print(f"\n{'='*60}")
    print(f"5月抽選申込計画 — {plan_data['target_month']}")
    print(f"{'='*60}")
    print(f"対象日数: {plan_data['total_dates']}日（土日）")
    print(f"申込予定: {plan_data['planned_applications']}件 / 上限{plan_data['max_per_account']}件")
    print()

    for i, p in enumerate(plan_data["plan"], 1):
        print(f"  {i:2d}. {p['date']}({p['weekday']}) "
              f"{p['facility_name']} / {p['time_slot']} / {p['court_preference']}")

    print(f"\n注意:")
    print(f"  - 同一日時に複数校への申込は禁止（ルール違反）")
    print(f"  - 各アカウント最大{plan_data['max_per_account']}件")
    print(f"  - 申込期間: 4/20 09:00 〜 4/22")
    print(f"  - 当選確認+利用申請: 4/23 09:00 〜 4/25")

    # 計画をファイルに保存
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    with open(PLAN_FILE, "w", encoding="utf-8") as f:
        json.dump(plan_data, f, ensure_ascii=False, indent=2)
    print(f"\n計画保存先: {PLAN_FILE}")

    return plan_data


async def cmd_check_status(config, accounts):
    """現在の申込状況を確認"""
    for account in accounts:
        pw_inst, browser, page = await browser_login(account)
        if not page:
            continue

        try:
            l03_count, all_lottery = await get_current_l03_count(page)

            print(f"\n{'='*60}")
            print(f"申込状況 — {account['label']} ({account['id']})")
            print(f"{'='*60}")
            print(f"抽選待ち(L03): {l03_count}/15")
            print(f"残り枠: {15 - l03_count}")

            # 5月分のみ表示
            target = config.get("lottery", {}).get("target_month",
                                                    config["target_month"])
            may_records = [r for r in all_lottery
                          if r.get("ud", "").startswith(target)]
            if may_records:
                print(f"\n{target}分の抽選:")
                for r in may_records:
                    st = r.get("st", "")
                    print(f"  [{st}] {r.get('f', '')} {r.get('c', '')} "
                          f"{r.get('ud', '')[:10]} {r.get('us', '')[:5]}〜{r.get('ue', '')[:5]}")
            else:
                print(f"\n{target}分の申込はまだありません")

        finally:
            await browser.close()
            await pw_inst.stop()


async def cmd_apply(config, accounts, dry_run=True):
    """
    抽選申込を実行する。
    Playwright でブラウザ操作して申込。
    """
    plan_data = generate_lottery_plan(config)

    print(f"\n{'='*60}")
    print(f"抽選申込{'（DRY RUN）' if dry_run else '★ 実行 ★'}")
    print(f"{'='*60}")
    print(f"計画: {plan_data['planned_applications']}件")

    for account in accounts:
        pw_inst, browser, page = await browser_login(account)
        if not page:
            continue

        try:
            # 現在の申込数チェック
            l03_count, _ = await get_current_l03_count(page)
            remaining = 15 - l03_count
            log(f"アカウント {account['label']}: L03={l03_count}, 残り枠={remaining}")

            if remaining <= 0:
                log(f"申込枠なし。スキップ。", "WARN")
                continue

            applied = 0
            for entry in plan_data["plan"]:
                if applied >= remaining:
                    log(f"枠上限到達")
                    break

                fc = entry["facility_code"]
                fn = entry["facility_name"]
                dt = entry["date"]
                wd = entry["weekday"]

                log(f"\n申込 #{applied+1}: {fn} {dt}({wd})")

                if dry_run:
                    log(f"  [DRY RUN] スキップ")
                    applied += 1
                    continue

                # 施設の空き状況ページに遷移
                purpose_key = config.get("utilization_purpose_key", "71")
                url = (
                    f"{SITE_URL}/sapporo/FacilityAvailability/Index/{LGC}/{fc}"
                    f"?u%5B0%5D={purpose_key}&pt%5B0%5D=0&pt%5B1%5D=1&pt%5B2%5D=2"
                    f"&ptn=2&d={dt}&sd={dt}&ed={dt}"
                )
                await page.goto(url, wait_until="networkidle")
                await asyncio.sleep(3)

                # Vue.js 待機
                try:
                    await page.wait_for_function(
                        "() => document.querySelector('#app') && "
                        "!document.querySelector('#app').hasAttribute('v-cloak')",
                        timeout=10000,
                    )
                except Exception:
                    pass
                await asyncio.sleep(2)

                # スクリーンショット
                LOG_DIR.mkdir(parents=True, exist_ok=True)
                ts = now_jst().strftime("%Y%m%d_%H%M%S")
                await page.screenshot(
                    path=str(LOG_DIR / f"lottery_{fc}_{dt}_{ts}.png")
                )

                # 抽選申込セル（L01）をクリック
                clicked = await page.evaluate("""() => {
                    const cells = document.querySelectorAll(
                        '.status-cell, .time-cell, td, .v-btn'
                    );
                    for (const cell of cells) {
                        const text = cell.textContent.trim();
                        if (text === '△' || text.includes('抽選') ||
                            text.includes('申込')) {
                            cell.click();
                            return {clicked: true, text: text};
                        }
                    }
                    return {clicked: false};
                }""")

                if clicked.get("clicked"):
                    log(f"  セルクリック: {clicked['text']}")
                    await asyncio.sleep(2)

                    # 申込ボタンクリック
                    btn = await page.evaluate("""() => {
                        const keywords = ['抽選申込', '申込', '次へ'];
                        const btns = document.querySelectorAll(
                            'button, .v-btn, a.v-btn'
                        );
                        for (const kw of keywords) {
                            for (const b of btns) {
                                if (b.textContent.trim().includes(kw) &&
                                    b.offsetParent !== null && !b.disabled) {
                                    b.click();
                                    return {clicked: true, text: b.textContent.trim()};
                                }
                            }
                        }
                        return {clicked: false};
                    }""")

                    if btn.get("clicked"):
                        log(f"  ボタン: {btn['text']}")
                        await asyncio.sleep(3)

                        # 確認ダイアログ
                        await page.evaluate("""() => {
                            const dlg = document.querySelector(
                                '.v-dialog--active, .v-overlay--active'
                            );
                            if (dlg) {
                                const btns = dlg.querySelectorAll('button, .v-btn');
                                for (const b of btns) {
                                    if (b.textContent.includes('はい') ||
                                        b.textContent.includes('OK')) {
                                        b.click(); return true;
                                    }
                                }
                            }
                            return false;
                        }""")
                        await asyncio.sleep(3)

                        await page.screenshot(
                            path=str(LOG_DIR / f"lottery_result_{fc}_{dt}_{ts}.png")
                        )
                        applied += 1
                        log(f"  申込完了")
                    else:
                        log(f"  申込ボタン見つからず", "WARN")
                else:
                    log(f"  抽選受付セル見つからず", "WARN")

                await asyncio.sleep(1)

            log(f"\nアカウント {account['label']}: {applied}件処理")

        finally:
            await browser.close()
            await pw_inst.stop()


async def cmd_confirm(config, accounts, dry_run=True):
    """
    当選確認 + 利用申請
    L05（当選）ステータスの申込に対して利用申請を行う。
    ★ これを忘れると当選無効 ★
    """
    print(f"\n{'='*60}")
    print(f"当選確認 + 利用申請{'（DRY RUN）' if dry_run else '★ 実行 ★'}")
    print(f"{'='*60}")

    for account in accounts:
        pw_inst, browser, page = await browser_login(account)
        if not page:
            continue

        try:
            # 抽選タブの結果を取得
            result = await page.evaluate("""async () => {
                const resp = await fetch('/sapporo/RequestStatuses/Search', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({t:2, so:1, k:null, st:"", ym:null, p:1, s:200})
                });
                return await resp.json();
            }""")

            data = result.get("data", [])
            won = [r for r in data if r.get("st") == "L05"]

            print(f"\nアカウント {account['label']}:")

            if not won:
                print(f"  当選(L05)なし")
                # L08（当選→利用申請済み）も確認
                l08 = [r for r in data if r.get("st") == "L08"]
                if l08:
                    print(f"  利用申請済み(L08): {len(l08)}件")
                continue

            print(f"  ★ 当選 {len(won)}件 — 利用申請が必要！ ★")
            for r in won:
                print(f"    {r.get('f', '')} {r.get('c', '')} "
                      f"{r.get('ud', '')[:10]} {r.get('us', '')[:5]}〜{r.get('ue', '')[:5]}")
                print(f"    申込番号: {r.get('a', '')}")

                if dry_run:
                    print(f"    [DRY RUN] 利用申請スキップ")
                    continue

                # 利用申請: 詳細画面に遷移
                app_no = r.get("a", "")
                branch = r.get("ab", 1)
                detail_url = (
                    f"{SITE_URL}/sapporo/LotRequests/Details"
                    f"/{LGC}/{app_no}/{branch}"
                )
                log(f"  利用申請: {detail_url}")
                await page.goto(detail_url, wait_until="networkidle")
                await asyncio.sleep(3)

                # 利用申請ボタンをクリック
                btn = await page.evaluate("""() => {
                    const keywords = [
                        '利用申請', '予約確定', '申請', '確定', '利用する'
                    ];
                    const btns = document.querySelectorAll(
                        'button, .v-btn, a.v-btn'
                    );
                    for (const kw of keywords) {
                        for (const b of btns) {
                            if (b.textContent.trim().includes(kw) &&
                                b.offsetParent !== null && !b.disabled) {
                                b.click();
                                return {clicked: true, text: b.textContent.trim()};
                            }
                        }
                    }
                    return {clicked: false};
                }""")

                if btn.get("clicked"):
                    log(f"  利用申請ボタン: {btn['text']}")
                    await asyncio.sleep(3)

                    # 確認ダイアログ
                    await page.evaluate("""() => {
                        const dlg = document.querySelector(
                            '.v-dialog--active, .v-overlay--active'
                        );
                        if (dlg) {
                            const btns = dlg.querySelectorAll('button, .v-btn');
                            for (const b of btns) {
                                if (b.textContent.includes('はい') ||
                                    b.textContent.includes('OK') ||
                                    b.textContent.includes('確定')) {
                                    b.click(); return true;
                                }
                            }
                        }
                        return false;
                    }""")
                    await asyncio.sleep(3)

                    LOG_DIR.mkdir(parents=True, exist_ok=True)
                    ts = now_jst().strftime("%Y%m%d_%H%M%S")
                    await page.screenshot(
                        path=str(LOG_DIR / f"confirm_{app_no}_{ts}.png")
                    )
                    log(f"  利用申請完了: {r.get('f', '')} {r.get('ud', '')[:10]}")
                else:
                    log(f"  利用申請ボタン見つからず", "WARN")
                    LOG_DIR.mkdir(parents=True, exist_ok=True)
                    ts = now_jst().strftime("%Y%m%d_%H%M%S")
                    await page.screenshot(
                        path=str(LOG_DIR / f"confirm_fail_{app_no}_{ts}.png")
                    )

        finally:
            await browser.close()
            await pw_inst.stop()


async def main():
    import argparse
    parser = argparse.ArgumentParser(description="5月抽選 準備・申込・確認")
    parser.add_argument("--plan", action="store_true", help="申込計画表示")
    parser.add_argument("--check-status", action="store_true", help="現在の申込状況")
    parser.add_argument("--apply", action="store_true", help="抽選申込実行")
    parser.add_argument("--confirm", action="store_true", help="当選確認+利用申請")
    parser.add_argument("--account", type=int, help="アカウント番号")
    parser.add_argument("--dry-run", action="store_true", help="予行演習")
    parser.add_argument("--execute", action="store_true", help="実際に実行")
    args = parser.parse_args()

    config = load_config()
    env_data = load_env()
    accounts = resolve_accounts(config, env_data, args.account)

    dry_run = not args.execute if args.execute else True
    if args.dry_run:
        dry_run = True

    if args.plan:
        await cmd_plan(config)
    elif args.check_status:
        await cmd_check_status(config, accounts)
    elif args.apply:
        await cmd_apply(config, accounts, dry_run=dry_run)
    elif args.confirm:
        await cmd_confirm(config, accounts, dry_run=dry_run)
    else:
        # デフォルト: 計画表示
        await cmd_plan(config)


if __name__ == "__main__":
    asyncio.run(main())
