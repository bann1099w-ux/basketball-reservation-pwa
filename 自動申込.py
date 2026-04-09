#!/usr/bin/env python3
"""
バスケットボール施設 自動抽選申込スクリプト
harp.lg.jp (札幌市施設予約システム) に対して Playwright で自動申込を行う。

フロー:
  1. Incapsula WAFチャレンジ通過
  2. ログイン
  3. 現在のL03件数を確認（上限チェック）
  4. 施設空き状況を確認
  5. 抽選申込可能な枠を選択・申込
  6. 結果をログ・通知

使い方:
  python3 自動申込.py                  # dry_run=設定値
  python3 自動申込.py --dry-run        # 実行せず確認のみ
  python3 自動申込.py --execute        # 実際に申込実行
  python3 自動申込.py --account 1      # アカウント1のみ
"""

import asyncio
import json
import os
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from calendar import monthrange
from urllib.parse import urlparse

from playwright.async_api import async_playwright

# === パス定数 ===
BASE_DIR = Path(__file__).parent
PROJECT_DIR = BASE_DIR.parent.parent
HARP_RECON_DIR = PROJECT_DIR / "ツール" / "harp偵察"
CONFIG_FILE = BASE_DIR / "設定.json"
LOG_DIR = BASE_DIR / "logs"
ENV_FILE = HARP_RECON_DIR / ".env"

SITE_URL = "https://yoyaku.harp.lg.jp"
TOP_URL = f"{SITE_URL}/sapporo/"
LGC = "011002"
JST = timezone(timedelta(hours=9))

VIEWPORT = {"width": 375, "height": 812}
UA = "Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.6099.230 Mobile Safari/537.36"
TIMEOUT_MS = 30000


# === ロガー ===
class Logger:
    def __init__(self, log_dir: Path):
        log_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(JST).strftime("%Y%m%d_%H%M%S")
        self.log_file = log_dir / f"apply_{ts}.log"
        self.results = []

    def log(self, msg: str, level: str = "INFO"):
        ts = datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S")
        entry = f"[{ts}] [{level}] {msg}"
        print(entry)
        with open(self.log_file, "a", encoding="utf-8") as f:
            f.write(entry + "\n")

    def add_result(self, account: str, facility: str, date: str,
                   court: str, status: str, detail: str = ""):
        self.results.append({
            "timestamp": datetime.now(JST).isoformat(),
            "account": account,
            "facility": facility,
            "date": date,
            "court": court,
            "status": status,
            "detail": detail,
        })

    def save_results(self):
        result_file = self.log_file.with_suffix(".json")
        with open(result_file, "w", encoding="utf-8") as f:
            json.dump({
                "run_at": datetime.now(JST).isoformat(),
                "results": self.results,
                "summary": self.get_summary(),
            }, f, ensure_ascii=False, indent=2)
        return result_file

    def get_summary(self):
        total = len(self.results)
        success = sum(1 for r in self.results if r["status"] == "applied")
        skipped = sum(1 for r in self.results if r["status"] == "skipped")
        failed = sum(1 for r in self.results if r["status"] == "failed")
        return {
            "total": total, "applied": success,
            "skipped": skipped, "failed": failed,
        }


# === 設定読み込み ===
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
            })
    return accounts


# === Playwright ヘルパー ===
async def wait(sec=2.0):
    await asyncio.sleep(sec)


async def wait_for_vue(page, timeout=10000):
    try:
        await page.wait_for_function(
            "() => document.querySelector('#app') && "
            "!document.querySelector('#app').hasAttribute('v-cloak')",
            timeout=timeout,
        )
    except Exception:
        pass
    await wait(1)


async def pass_incapsula(page, logger):
    """Incapsula WAFチャレンジを通過"""
    logger.log("Incapsulaチャレンジ通過待機中...")
    await page.goto(TOP_URL, wait_until="networkidle")
    await wait(3)

    for attempt in range(5):
        content = await page.content()
        if "Incapsula" in content or "Request Timeout" in content:
            logger.log(f"チャレンジ中... (試行{attempt+1}/5)")
            await wait(3)
            await page.reload(wait_until="networkidle")
            await wait(2)
        else:
            logger.log("Incapsulaチャレンジ通過")
            return True

    title = await page.title()
    if "札幌市" in title or "ホーム" in title:
        logger.log("チャレンジ通過（タイトル確認）")
        return True

    logger.log("チャレンジ通過失敗", "ERROR")
    return False


async def do_login(page, account, logger):
    """harp.lg.jpにログイン"""
    logger.log(f"ログイン中... (アカウント: {account['label']})")
    await page.goto(f"{SITE_URL}/sapporo/Login", wait_until="networkidle")
    await wait(2)
    await wait_for_vue(page)

    # フィールド検出
    id_field = (
        await page.query_selector("input[type='text']")
        or await page.query_selector("input[type='tel']")
        or await page.query_selector(
            "input:not([type='password']):not([type='hidden']):not([type='submit'])"
        )
    )
    pw_field = await page.query_selector("input[type='password']")

    if not id_field or not pw_field:
        logger.log("ログインフィールド検出失敗", "ERROR")
        return False

    await id_field.click()
    await wait(0.3)
    await id_field.fill("")
    await id_field.type(account["id"], delay=50)
    await wait(0.5)

    await pw_field.click()
    await wait(0.3)
    await pw_field.fill("")
    await pw_field.type(account["pw"], delay=50)
    await wait(0.5)

    # ログインボタン
    login_btn = (
        await page.query_selector("button[type='submit']")
        or await page.query_selector("input[type='submit']")
        or await page.query_selector("button:has-text('ログイン')")
        or await page.query_selector(".v-btn:has-text('ログイン')")
    )

    if login_btn:
        await login_btn.click()
    else:
        await pw_field.press("Enter")

    await wait(3)
    await page.wait_for_load_state("networkidle")
    await wait_for_vue(page)

    # ログイン成功判定
    url = page.url
    if "/Login" not in url:
        logger.log(f"ログイン成功: {url}")
        return True

    logger.log("ログイン失敗（ログインページのまま）", "ERROR")
    return False


async def get_current_l03_count(page, logger):
    """現在のL03(抽選待ち)件数を取得"""
    logger.log("現在の申込状況を確認中...")
    await page.goto(f"{SITE_URL}/sapporo/RequestStatuses/Index", wait_until="networkidle")
    await wait(2)
    await wait_for_vue(page)

    # API呼び出しをインターセプト（横取り取得）
    l03_count = 0
    all_records = []

    try:
        response = await page.evaluate("""async () => {
            const resp = await fetch('/sapporo/RequestStatuses/Search', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({t:0, so:1, k:null, st:"", ym:null, p:1, s:200})
            });
            return await resp.json();
        }""")

        data = response.get("data", [])
        for rec in data:
            if rec.get("st") == "L03":
                l03_count += 1
                all_records.append({
                    "facility": rec.get("f", ""),
                    "court": rec.get("c", "全面"),
                    "date": rec.get("ud", "")[:10],
                    "time": f"{rec.get('us', '')}~{rec.get('ue', '')}",
                })
    except Exception as e:
        logger.log(f"申込状況取得エラー: {e}", "WARN")

    logger.log(f"現在のL03件数: {l03_count}/15")
    return l03_count, all_records


async def check_facility_availability(page, facility, config, logger):
    """施設の空き状況を確認し、申込可能な枠を返す"""
    code = facility["code"]
    name = facility["name"]
    year, month = map(int, config["target_month"].split("-"))

    start_date = f"{year}-{month:02d}-01"
    _, days = monthrange(year, month)
    end_date = f"{year}-{month:02d}-{days}"

    logger.log(f"空き状況確認: {name} ({code}) [{start_date}~{end_date}]")

    # 施設空き状況APIを直接呼ぶ
    try:
        result = await page.evaluate("""async (params) => {
            const resp = await fetch(
                `/sapporo/FacilityAvailability/GetCalendar/${params.lgc}/${params.fc}`,
                {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({
                        startDate: params.start,
                        endDate: params.end,
                        roomCode: null,
                        courtSize: null,
                        utilizationPurpose: [{
                            groupId: "utilizationPurpose",
                            key: params.upKey,
                            utilizationPurposeId: parseInt(params.upKey),
                            utilizationPurposeCategoryName: "学校開放（屋内）",
                            utilizationPurposeName: "ミニバスケットボール",
                            groupName: "利用目的",
                            itemId: "utilizationPurpose" + params.upKey
                        }],
                        usePeople: null,
                        room: null,
                        toggleTimeType: false,
                        usageTimes: null,
                        usagePeriodOfTime: null,
                        allowInternetRequest: null,
                        requestId: null,
                        period: 35
                    })
                }
            );
            return await resp.json();
        }""", {
            "lgc": LGC,
            "fc": code,
            "start": start_date,
            "end": end_date,
            "upKey": config["utilization_purpose_key"],
        })
    except Exception as e:
        logger.log(f"  空き状況API呼び出し失敗: {e}", "ERROR")
        return []

    # 対象曜日の抽選受付中枠をフィルタ
    weekdays = config["target_weekdays"]
    available_slots = []

    day_books = result.get("dayBooks", [])
    for db in day_books:
        usage_date = db.get("usageDate", "")
        if not usage_date:
            continue
        dt = datetime.fromisoformat(usage_date.replace("+09:00", "").replace("Z", ""))
        if dt.weekday() not in weekdays:
            continue

        status = db.get("statusType", "")
        # L01（抽選受付中）が申込可能
        if status == "L01":
            available_slots.append({
                "facility_code": code,
                "facility_name": name,
                "date": dt.strftime("%Y-%m-%d"),
                "weekday": dt.strftime("%a"),
                "status": status,
                "raw": db,
            })

    logger.log(f"  {name}: 抽選受付中 {len(available_slots)}枠")
    return available_slots


async def navigate_to_booking_form(page, facility_code, slot_date, logger):
    """施設の空き状況ページに遷移し、抽選申込フォームに到達する"""
    url = f"{SITE_URL}/sapporo/FacilityAvailability/Index/{LGC}/{facility_code}"
    logger.log(f"  施設空き状況ページに遷移: {url}")

    await page.goto(url, wait_until="networkidle")
    await wait(3)
    await wait_for_vue(page)
    return True


async def apply_for_slot(page, slot, config, logger, dry_run=True):
    """
    抽選申込を実行する。
    Vue.js SPAのUI操作でフォームを辿る。
    """
    name = slot["facility_name"]
    date = slot["date"]
    code = slot["facility_code"]

    logger.log(f"  申込処理: {name} {date}")

    if dry_run:
        logger.log(f"  [DRY RUN] 申込スキップ: {name} {date}")
        return "dry_run"

    try:
        # ステップ1: 施設空き状況ページに遷移
        nav_ok = await navigate_to_booking_form(page, code, date, logger)
        if not nav_ok:
            return "nav_failed"

        # ステップ2: カレンダーから対象日付のセルをクリック
        # harpのカレンダーは日付セルがクリック可能
        target_day = int(date.split("-")[2])
        clicked = await page.evaluate("""(targetDay) => {
            // カレンダーのセルを探す（Vue.js + Vuetifyのカレンダー）
            const cells = document.querySelectorAll(
                '.v-calendar .v-btn, .calendar-cell, td[data-date], .day-cell'
            );
            for (const cell of cells) {
                const text = cell.textContent.trim();
                if (text === String(targetDay)) {
                    cell.click();
                    return true;
                }
            }
            // フォールバック: data-date属性
            const dateCell = document.querySelector(`[data-date="${targetDay}"]`);
            if (dateCell) { dateCell.click(); return true; }
            return false;
        }""", target_day)

        if not clicked:
            # 別のアプローチ: URLパラメータで日付指定
            logger.log(f"  カレンダーセルクリック失敗、直接URLアプローチ試行")

        await wait(2)
        await wait_for_vue(page)

        # ステップ3: 時間帯選択（夜間を選択）
        time_clicked = await page.evaluate("""() => {
            const btns = document.querySelectorAll(
                '.v-btn, button, .time-slot, .period-btn, a'
            );
            for (const btn of btns) {
                const text = btn.textContent.trim();
                if (text.includes('夜間') || text.includes('18:') || text.includes('19:')) {
                    btn.click();
                    return text;
                }
            }
            return null;
        }""")

        if time_clicked:
            logger.log(f"  時間帯選択: {time_clicked}")
        else:
            logger.log(f"  時間帯選択失敗", "WARN")

        await wait(2)
        await wait_for_vue(page)

        # ステップ4: コート選択（半面A優先）
        court_pref = config.get("court_preference", "half_a")
        court_labels = {
            "half_a": ["半面Ａ", "半面A"],
            "half_b": ["半面Ｂ", "半面B"],
            "full": ["全面"],
        }
        target_courts = court_labels.get(court_pref, ["半面Ａ", "半面A"])

        court_clicked = await page.evaluate("""(targetCourts) => {
            const els = document.querySelectorAll(
                '.v-btn, button, .court-btn, label, .v-radio, .v-list-item'
            );
            for (const el of els) {
                const text = el.textContent.trim();
                for (const court of targetCourts) {
                    if (text.includes(court)) {
                        el.click();
                        return text;
                    }
                }
            }
            return null;
        }""", target_courts)

        if court_clicked:
            logger.log(f"  コート選択: {court_clicked}")

        await wait(2)
        await wait_for_vue(page)

        # ステップ5: 申込ボタン / 抽選申込ボタンをクリック
        apply_clicked = await page.evaluate("""() => {
            const keywords = ['抽選申込', '申込', '申し込み', '申込む', '確定', '送信', '次へ'];
            const btns = document.querySelectorAll('button, .v-btn, input[type="submit"], a');
            for (const kw of keywords) {
                for (const btn of btns) {
                    const text = btn.textContent.trim();
                    if (text.includes(kw) && btn.offsetParent !== null) {
                        btn.click();
                        return text;
                    }
                }
            }
            return null;
        }""")

        if apply_clicked:
            logger.log(f"  申込ボタンクリック: {apply_clicked}")
        else:
            logger.log(f"  申込ボタン検出失敗", "WARN")
            await page.screenshot(
                path=str(LOG_DIR / f"apply_btn_fail_{code}_{date}.png")
            )
            return "btn_not_found"

        await wait(3)
        await wait_for_vue(page)

        # ステップ6: 確認ダイアログ対応
        confirm_clicked = await page.evaluate("""() => {
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
                        return text;
                    }
                }
            }
            return null;
        }""")

        if confirm_clicked:
            logger.log(f"  確認ダイアログ: {confirm_clicked}")

        await wait(3)

        # ステップ7: 結果確認 - スクリーンショット保存
        await page.screenshot(
            path=str(LOG_DIR / f"apply_result_{code}_{date}.png")
        )

        # エラーメッセージチェック
        error_text = await page.evaluate("""() => {
            const alerts = document.querySelectorAll(
                '.v-alert, .error, .v-snack, .alert-danger, [role="alert"]'
            );
            for (const el of alerts) {
                if (el.offsetParent !== null) return el.textContent.trim();
            }
            return null;
        }""")

        if error_text:
            logger.log(f"  エラー検出: {error_text}", "ERROR")
            return "error"

        # 成功メッセージチェック
        success_text = await page.evaluate("""() => {
            const els = document.querySelectorAll(
                '.v-alert.success, .success, .v-snack.success, [role="status"]'
            );
            for (const el of els) {
                if (el.offsetParent !== null) return el.textContent.trim();
            }
            // URLチェック
            if (location.href.includes('RequestStatuses')
                || location.href.includes('Complete')) return 'redirect_to_status';
            return null;
        }""")

        if success_text:
            logger.log(f"  申込成功: {success_text}")
            return "applied"

        logger.log(f"  申込結果不明 (URL: {page.url})", "WARN")
        return "unknown"

    except Exception as e:
        logger.log(f"  申込処理エラー: {e}", "ERROR")
        try:
            await page.screenshot(
                path=str(LOG_DIR / f"apply_error_{code}_{date}.png")
            )
        except Exception:
            pass
        return "error"


async def run_account(browser, account, config, logger, dry_run):
    """1アカウント分の申込処理"""
    label = account["label"]
    logger.log(f"{'='*50}")
    logger.log(f"アカウント {label} ({account['id']}) 処理開始")

    context = await browser.new_context(
        viewport=VIEWPORT, user_agent=UA,
        locale="ja-JP", timezone_id="Asia/Tokyo",
    )
    context.set_default_timeout(TIMEOUT_MS)

    # WebDriver（ウェブドライバ）検知回避
    await context.add_init_script("""
        Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
        Object.defineProperty(navigator, 'plugins', {get: () => [1,2,3,4,5]});
        Object.defineProperty(navigator, 'languages', {get: () => ['ja-JP','ja','en-US','en']});
        window.chrome = {runtime: {}};
    """)

    page = await context.new_page()

    try:
        # ステップ1: インカプスラ（Incapsula）チャレンジ
        if not await pass_incapsula(page, logger):
            await page.screenshot(path=str(LOG_DIR / f"incapsula_fail_{label}.png"))
            logger.log(f"アカウント {label}: WAFチャレンジ失敗", "ERROR")
            logger.add_result(label, "-", "-", "-", "failed", "WAFチャレンジ失敗")
            await context.close()
            return

        # ステップ2: ログイン
        if not await do_login(page, account, logger):
            await page.screenshot(path=str(LOG_DIR / f"login_fail_{label}.png"))
            logger.add_result(label, "-", "-", "-", "failed", "ログイン失敗")
            await context.close()
            return

        # ステップ3: 現在のL03（抽選待ち）件数チェック
        l03_count, existing = await get_current_l03_count(page, logger)
        max_apps = config.get("max_applications_per_account", 15)
        remaining = max_apps - l03_count

        if remaining <= 0:
            logger.log(f"アカウント {label}: 申込枠なし ({l03_count}/{max_apps})")
            logger.add_result(label, "-", "-", "-", "skipped", f"枠上限到達 {l03_count}/{max_apps}")
            await context.close()
            return

        logger.log(f"残り申込枠: {remaining}")

        # 既存申込のセット（重複防止）
        existing_set = set()
        for rec in existing:
            key = f"{rec['facility']}_{rec['date']}_{rec.get('court', '')}"
            existing_set.add(key)

        # ステップ4: 各施設の空き確認 + 申込
        applied_count = 0
        for facility in config["target_facilities"]:
            if applied_count >= remaining:
                logger.log(f"申込枠上限到達、残り施設スキップ")
                break

            slots = await check_facility_availability(page, facility, config, logger)

            for slot in slots:
                if applied_count >= remaining:
                    break

                # 重複チェック
                dup_key = f"{slot['facility_name']}_{slot['date']}_"
                if any(dup_key in k for k in existing_set):
                    logger.log(f"  スキップ(重複): {slot['facility_name']} {slot['date']}")
                    logger.add_result(
                        label, slot["facility_name"], slot["date"],
                        "-", "skipped", "重複申込"
                    )
                    continue

                # 申込実行
                result = await apply_for_slot(page, slot, config, logger, dry_run)
                court = config.get("court_preference", "half_a")

                if result == "applied":
                    applied_count += 1
                    logger.add_result(
                        label, slot["facility_name"], slot["date"],
                        court, "applied", "申込成功"
                    )
                elif result == "dry_run":
                    logger.add_result(
                        label, slot["facility_name"], slot["date"],
                        court, "dry_run", "DRY RUN"
                    )
                else:
                    logger.add_result(
                        label, slot["facility_name"], slot["date"],
                        court, "failed", f"結果: {result}"
                    )

                await wait(1)  # サーバー負荷配慮

        logger.log(f"アカウント {label}: 完了 (申込{applied_count}件)")

    except Exception as e:
        logger.log(f"アカウント {label}: 致命的エラー: {e}", "ERROR")
        try:
            await page.screenshot(path=str(LOG_DIR / f"fatal_{label}.png"))
        except Exception:
            pass
    finally:
        await context.close()


# === 通知 ===
def send_notification(logger, config):
    """心拍.py経由でダッシュボードに通知"""
    summary = logger.get_summary()
    msg = (
        f"バスケ自動申込完了: "
        f"申込{summary['applied']} / スキップ{summary['skipped']} / "
        f"失敗{summary['failed']} (計{summary['total']}件)"
    )

    if config.get("notify", {}).get("heartbeat", False):
        try:
            sys.path.insert(0, str(PROJECT_DIR / "内閣府"))
            from 心拍 import send_heartbeat
            event_type = "complete" if summary["failed"] == 0 else "error"
            send_heartbeat(event_type, msg, str(logger.log_file))
            logger.log(f"Heartbeat通知送信: {event_type}")
        except Exception as e:
            logger.log(f"Heartbeat通知失敗: {e}", "WARN")

    logger.log(f"結果サマリー: {msg}")


# === メイン ===
async def main():
    import argparse
    parser = argparse.ArgumentParser(description="バスケ施設 自動抽選申込")
    parser.add_argument("--dry-run", action="store_true", help="実行せず確認のみ")
    parser.add_argument("--execute", action="store_true", help="実際に申込実行")
    parser.add_argument("--account", type=int, help="特定アカウントのみ (1 or 2)")
    parser.add_argument("--config", default=str(CONFIG_FILE), help="設定ファイル")
    args = parser.parse_args()

    config = load_config()
    env_data = load_env()

    # ドライラン（dry_run: 予行演習）判定: --execute > --dry-run > 設定値
    if args.execute:
        dry_run = False
    elif args.dry_run:
        dry_run = True
    else:
        dry_run = config.get("dry_run", True)

    accounts = resolve_accounts(config, env_data, args.account)
    if not accounts:
        print("エラー: アカウント情報が見つかりません")
        sys.exit(1)

    logger = Logger(LOG_DIR)
    logger.log(f"バスケ自動申込スクリプト起動")
    logger.log(f"モード: {'DRY RUN' if dry_run else '★ 実行モード ★'}")
    logger.log(f"対象月: {config['target_month']}")
    logger.log(f"施設数: {len(config['target_facilities'])}")
    logger.log(f"アカウント数: {len(accounts)}")

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)

        for account in accounts:
            await run_account(browser, account, config, logger, dry_run)

        await browser.close()

    # 結果保存・通知
    result_file = logger.save_results()
    logger.log(f"結果ファイル: {result_file}")
    send_notification(logger, config)

    summary = logger.get_summary()
    logger.log(f"{'='*50}")
    logger.log(f"全処理完了")

    return 0 if summary["failed"] == 0 else 1


if __name__ == "__main__":
    exit_code = asyncio.run(main())
    sys.exit(exit_code)
