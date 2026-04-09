#!/usr/bin/env python3
"""
harp API クライアント（ハイブリッド方式）
Phase7A キャプチャ結果に基づく実装

方式: Playwright でログイン → Cookie取得 → requests で API直接呼び出し
理由: Incapsula WAF がブラウザ以外のリクエストをブロックするため、
      初回はブラウザでセッション確立し、以降はHTTP APIで高速処理

使い方:
  python3 harp_api.py --status          # 申込状況確認
  python3 harp_api.py --availability    # 空き状況確認（全対象施設）
  python3 harp_api.py --account 1       # アカウント1のみ
"""

import asyncio
import json
import os
import sys
import re
from datetime import datetime, timezone, timedelta
from pathlib import Path
from calendar import monthrange

import requests
from playwright.async_api import async_playwright

# === 定数 ===
BASE_DIR = Path(__file__).parent
PROJECT_DIR = BASE_DIR.parent.parent
RECON_DIR = PROJECT_DIR / "ツール" / "harp偵察"
CONFIG_FILE = BASE_DIR / "設定.json"
LOG_DIR = BASE_DIR / "logs"
ENV_FILE = RECON_DIR / ".env"

SITE_URL = "https://yoyaku.harp.lg.jp"
LGC = "011002"
JST = timezone(timedelta(hours=9))

# Phase7Aキャプチャで判明したパラメータ
利用目的 = {
    "バスケットボール": {
        "groupId": "utilizationPurpose",
        "key": "69",
        "utilizationPurposeId": 69,
        "utilizationPurposeCategoryName": "学校開放（屋内）",
        "utilizationPurposeName": "バスケットボール",
        "groupName": "利用目的",
        "itemId": "utilizationPurpose69",
    },
    "ミニバスケットボール": {
        "groupId": "utilizationPurpose",
        "key": "71",
        "utilizationPurposeId": 71,
        "utilizationPurposeCategoryName": "学校開放（屋内）",
        "utilizationPurposeName": "ミニバスケットボール",
        "groupName": "利用目的",
        "itemId": "utilizationPurpose71",
    },
}

# ステータスコード解読表
ステータス解読 = {
    "L01": "抽選受付中",
    "L03": "抽選待ち（申込済）",
    "L05": "当選",
    "L07": "落選",
    "L09": "抽選取消",
    "R01": "予約確定",
    "R03": "空きなし",
    "U01": "利用可能",
    "U03": "空きあり",
}


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


def resolve_account(env_data, account_num=1):
    acct_id = env_data.get(f"HARP_ACCOUNT_{account_num}_ID", "")
    acct_pw = env_data.get(f"HARP_ACCOUNT_{account_num}_PW", "")
    if not acct_id or not acct_pw:
        return None
    return {"id": acct_id, "pw": acct_pw, "label": f"#{account_num}"}


class HarpSession:
    """harp セッション管理（Playwright→requests ハイブリッド）"""

    def __init__(self):
        self.session = requests.Session()
        self.csrf_token = None
        self.logged_in = False
        self.account_label = ""

    async def login_via_browser(self, account):
        """Playwrightでログインし、Cookie を requests.Session にコピー"""
        self.account_label = account["label"]
        print(f"[LOGIN] アカウント {account['label']} でログイン中...")

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(
                viewport={"width": 375, "height": 812},
                user_agent="Mozilla/5.0 (Linux; Android 13; Pixel 7) "
                           "AppleWebKit/537.36 (KHTML, like Gecko) "
                           "Chrome/120.0.6099.230 Mobile Safari/537.36",
                locale="ja-JP",
                timezone_id="Asia/Tokyo",
            )
            context.set_default_timeout(30000)

            # WebDriver検知回避
            await context.add_init_script("""
                Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
                Object.defineProperty(navigator, 'plugins', {get: () => [1,2,3,4,5]});
                window.chrome = {runtime: {}};
            """)

            page = await context.new_page()

            # Incapsulaチャレンジ通過
            print("[LOGIN] Incapsulaチャレンジ通過中...")
            await page.goto(f"{SITE_URL}/sapporo/", wait_until="networkidle")
            await asyncio.sleep(3)

            for attempt in range(5):
                content = await page.content()
                if "Incapsula" in content or "Request Timeout" in content:
                    print(f"[LOGIN] チャレンジ中... ({attempt+1}/5)")
                    await asyncio.sleep(3)
                    await page.reload(wait_until="networkidle")
                else:
                    break

            # ログインページへ
            await page.goto(f"{SITE_URL}/sapporo/Login", wait_until="networkidle")
            await asyncio.sleep(2)

            # CSRFトークン取得
            csrf = await page.evaluate("""() => {
                const meta = document.querySelector('input[name="__RequestVerificationToken"]');
                return meta ? meta.value : null;
            }""")

            if csrf:
                self.csrf_token = csrf
                print(f"[LOGIN] CSRFトークン取得成功")

            # Vue.js待機
            try:
                await page.wait_for_function(
                    "() => document.querySelector('#app') && "
                    "!document.querySelector('#app').hasAttribute('v-cloak')",
                    timeout=10000,
                )
            except Exception:
                pass
            await asyncio.sleep(1)

            # ログイン: Vue.jsのAPIを直接呼び出す（DOM操作より確実）
            print("[LOGIN] API経由でログイン送信...")
            login_result = await page.evaluate("""async (params) => {
                try {
                    // CSRFトークンをhidden inputから取得
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
                    const data = await resp.text();
                    return {ok: resp.ok, status: resp.status, body: data.substring(0, 500)};
                } catch(e) {
                    return {ok: false, error: e.message};
                }
            }""", {"id": account["id"], "pw": account["pw"]})

            print(f"[LOGIN] レスポンス: status={login_result.get('status')}")

            # ログイン後のページ遷移を待つ
            await asyncio.sleep(2)
            await page.goto(f"{SITE_URL}/sapporo/RequestStatuses/Index",
                            wait_until="networkidle")
            await asyncio.sleep(2)

            # ログイン成功確認（申込状況ページに遷移できたか）
            if "/Login" in page.url:
                print("[LOGIN] ログイン失敗（リダイレクトされた）")
                await browser.close()
                return False

            print(f"[LOGIN] ログイン成功: {page.url}")

            # Cookie を requests.Session にコピー
            cookies = await context.cookies()
            for cookie in cookies:
                self.session.cookies.set(
                    cookie["name"], cookie["value"],
                    domain=cookie.get("domain", ""),
                    path=cookie.get("path", "/"),
                )

            # requestverificationtoken ヘッダー取得（APIリクエスト用）
            # ページ内のmetaタグまたはhidden inputから取得
            token = await page.evaluate("""() => {
                const input = document.querySelector('input[name="__RequestVerificationToken"]');
                if (input) return input.value;
                const meta = document.querySelector('meta[name="csrf-token"]');
                if (meta) return meta.getAttribute('content');
                return null;
            }""")
            if token:
                self.csrf_token = token

            await browser.close()

        # requests.Session のヘッダー設定
        self.session.headers.update({
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json;charset=UTF-8",
            "User-Agent": "Mozilla/5.0 (Linux; Android 13; Pixel 7) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) "
                          "Chrome/120.0.6099.230 Mobile Safari/537.36",
            "Referer": f"{SITE_URL}/sapporo/",
        })
        if self.csrf_token:
            self.session.headers["requestverificationtoken"] = self.csrf_token

        self.logged_in = True
        return True

    def api_post(self, path, data):
        """API POST リクエスト（JSON）"""
        url = f"{SITE_URL}/sapporo/{path}"
        try:
            resp = self.session.post(url, json=data, timeout=15)
            resp.raise_for_status()
            return resp.json()
        except requests.exceptions.HTTPError as e:
            print(f"[API ERROR] {path}: HTTP {resp.status_code}")
            return None
        except Exception as e:
            print(f"[API ERROR] {path}: {e}")
            return None

    def api_get(self, path):
        """API GET リクエスト"""
        url = f"{SITE_URL}/sapporo/{path}"
        try:
            resp = self.session.get(url, timeout=15)
            resp.raise_for_status()
            return resp
        except Exception as e:
            print(f"[API ERROR] {path}: {e}")
            return None

    # === 申込状況取得 ===
    def get_request_statuses(self, tab=0, page_num=1, page_size=100):
        """
        申込状況を取得
        tab: 0=すべて, 1=予約（確定済み）, 2=抽選
        """
        data = {
            "t": tab,
            "so": 1,
            "k": None,
            "st": "",
            "ym": None,
            "p": page_num,
            "s": page_size,
        }
        result = self.api_post("RequestStatuses/Search", data)
        if not result:
            return []
        return result.get("data", [])

    def get_all_statuses(self):
        """全申込状況を取得してカテゴリ分け"""
        all_data = self.get_request_statuses(tab=0, page_size=200)
        lottery_data = self.get_request_statuses(tab=2, page_size=200)
        reservation_data = self.get_request_statuses(tab=1, page_size=200)

        return {
            "all": all_data,
            "lottery": lottery_data,
            "reservation": reservation_data,
        }

    # === 空き状況取得 ===
    def get_day_availability(self, facility_code, date, purposes=None):
        """
        特定施設・特定日の空き状況を取得
        facility_code: 施設コード（例: "0281"）
        date: 日付文字列（例: "2026-04-30"）
        purposes: 利用目的リスト（デフォルト: バスケ+ミニバス）
        """
        if purposes is None:
            purposes = [利用目的["バスケットボール"], 利用目的["ミニバスケットボール"]]

        data = {
            "startDate": date,
            "endDate": date,
            "roomCode": None,
            "courtSize": None,
            "utilizationPurpose": purposes,
            "usePeople": None,
            "room": None,
            "toggleTimeType": False,
            "usageTimes": None,
            "usagePeriodOfTime": [0, 1, 2],
            "allowInternetRequest": None,
            "requestId": None,
        }
        path = f"FacilityAvailability/GetDay/{LGC}/{facility_code}"
        return self.api_post(path, data)

    def get_month_availability(self, facility_code, year, month, purposes=None):
        """月間の空き状況を一括取得"""
        _, days = monthrange(year, month)
        results = []
        for day in range(1, days + 1):
            date_str = f"{year}-{month:02d}-{day:02d}"
            result = self.get_day_availability(facility_code, date_str, purposes)
            if result:
                results.append({"date": date_str, "data": result})
        return results


def format_status_record(rec):
    """申込状況レコードを読みやすく整形"""
    st = rec.get("st", "")
    st_name = ステータス解読.get(st, st)
    t = rec.get("t", 0)
    t_name = {1: "予約", 2: "抽選"}.get(t, "不明")

    ud = rec.get("ud", "")[:10]
    us = rec.get("us", "")[:5]
    ue = rec.get("ue", "")[:5]

    return (
        f"  [{st_name}] {rec.get('f', '')} {rec.get('r', '')} {rec.get('c', '')}\n"
        f"    日時: {ud} {us}〜{ue} / 種別: {t_name} / 申込番号: {rec.get('a', '')}"
    )


def format_availability(result):
    """空き状況結果を読みやすく整形"""
    if not result:
        return "  データなし"

    lines = []
    rooms = result.get("rooms", [])
    for room in rooms:
        room_name = room.get("roomName", "")
        courts = room.get("courts", [])
        for court in courts:
            court_name = court.get("courtName", "")
            day_books = court.get("dayBooks", [])
            for db in day_books:
                date = db.get("usageDate", "")[:10]
                times = db.get("usageTimes", [])
                for t in times:
                    st = t.get("statusType", "")
                    st_name = ステータス解読.get(st, st)
                    lot_num = t.get("lotRequestNumber", 0)
                    frame_id = t.get("usageTimeFrameId", "")
                    lines.append(
                        f"  {room_name} {court_name} | {date} | "
                        f"枠ID:{frame_id} | {st_name} | 抽選申込数:{lot_num}"
                    )
    return "\n".join(lines) if lines else "  データなし"


async def cmd_status(account_num=1):
    """申込状況確認コマンド"""
    env_data = load_env()
    account = resolve_account(env_data, account_num)
    if not account:
        print(f"アカウント{account_num}の情報が見つかりません")
        return

    harp = HarpSession()
    if not await harp.login_via_browser(account):
        print("ログイン失敗")
        return

    print(f"\n{'='*60}")
    print(f"申込状況確認 — アカウント {account['label']} ({account['id']})")
    print(f"{'='*60}")

    statuses = harp.get_all_statuses()

    # 抽選
    lottery = statuses["lottery"]
    print(f"\n■ 抽選 ({len(lottery)}件)")
    for rec in lottery:
        print(format_status_record(rec))

    # 予約
    reservations = statuses["reservation"]
    print(f"\n■ 予約確定 ({len(reservations)}件)")
    for rec in reservations:
        print(format_status_record(rec))

    # サマリー
    l03_count = sum(1 for r in lottery if r.get("st") == "L03")
    l07_count = sum(1 for r in lottery if r.get("st") == "L07")
    l05_count = sum(1 for r in lottery if r.get("st") == "L05")
    r01_count = sum(1 for r in reservations if r.get("st") == "R01")

    print(f"\n■ サマリー")
    print(f"  抽選待ち(L03): {l03_count}/15")
    print(f"  当選(L05): {l05_count}")
    print(f"  落選(L07): {l07_count}")
    print(f"  予約確定(R01): {r01_count}")

    # 結果をJSON保存
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(JST).strftime("%Y%m%d_%H%M%S")
    save_path = LOG_DIR / f"status_{ts}.json"
    with open(save_path, "w", encoding="utf-8") as f:
        json.dump({
            "timestamp": datetime.now(JST).isoformat(),
            "account": account["id"],
            "statuses": statuses,
            "summary": {
                "l03": l03_count, "l05": l05_count,
                "l07": l07_count, "r01": r01_count,
            },
        }, f, ensure_ascii=False, indent=2)
    print(f"\n保存先: {save_path}")


async def cmd_availability(account_num=1):
    """空き状況確認コマンド"""
    config = load_config()
    env_data = load_env()
    account = resolve_account(env_data, account_num)
    if not account:
        print(f"アカウント{account_num}の情報が見つかりません")
        return

    harp = HarpSession()
    if not await harp.login_via_browser(account):
        print("ログイン失敗")
        return

    target_month = config["target_month"]
    year, month = map(int, target_month.split("-"))

    print(f"\n{'='*60}")
    print(f"空き状況確認 — {target_month} — アカウント {account['label']}")
    print(f"{'='*60}")

    # 対象日をフィルタ（土日のみ等）
    weekdays = config.get("target_weekdays", [0, 6])
    _, days = monthrange(year, month)

    target_dates = []
    for day in range(1, days + 1):
        dt = datetime(year, month, day)
        if dt.weekday() in weekdays:
            target_dates.append(f"{year}-{month:02d}-{day:02d}")

    print(f"対象日数: {len(target_dates)}日（曜日フィルタ適用）")

    all_results = []
    for facility in config["target_facilities"]:
        code = facility["code"]
        name = facility["name"]
        print(f"\n■ {name} ({code})")

        for date in target_dates:
            result = harp.get_day_availability(code, date)
            if result:
                formatted = format_availability(result)
                if "データなし" not in formatted:
                    print(f"  {date}:")
                    print(formatted)
                    all_results.append({
                        "facility": name,
                        "code": code,
                        "date": date,
                        "data": result,
                    })

    # 結果保存
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(JST).strftime("%Y%m%d_%H%M%S")
    save_path = LOG_DIR / f"availability_{ts}.json"
    with open(save_path, "w", encoding="utf-8") as f:
        json.dump({
            "timestamp": datetime.now(JST).isoformat(),
            "target_month": target_month,
            "results": all_results,
        }, f, ensure_ascii=False, indent=2)
    print(f"\n保存先: {save_path}")


async def main():
    import argparse
    parser = argparse.ArgumentParser(description="harp API クライアント")
    parser.add_argument("--status", action="store_true", help="申込状況確認")
    parser.add_argument("--availability", action="store_true", help="空き状況確認")
    parser.add_argument("--account", type=int, default=1, help="アカウント番号 (1 or 2)")
    args = parser.parse_args()

    if args.status:
        await cmd_status(args.account)
    elif args.availability:
        await cmd_availability(args.account)
    else:
        # デフォルト: 申込状況確認
        await cmd_status(args.account)


if __name__ == "__main__":
    asyncio.run(main())
