#!/usr/bin/env python3
"""
キャンセル監視.py
目的: 26日09:30以降〜利用日まで、キャンセルで空いた枠を5分おきに検出→即申込
戦略: 09:00の争奪戦には参加しない。混雑が落ち着く09:30から安定監視を開始する

方式: 空き監視.py の MonitorSession を流用し、ブラウザDOMから○を検出→申込
"""

import asyncio
import json
import datetime
import os
import sys
from pathlib import Path

sys.path.append(os.path.expanduser("~/BIGBAN/ツール/バスケ自動申込"))
from 空き監視 import MonitorSession, load_config, load_env, resolve_accounts, now_jst

LOG_PATH = Path(os.path.expanduser("~/O-TN-SUN/記憶/キャンセル監視ログ.jsonl"))
LOG_DIR = Path(__file__).parent / "logs"

検出済み = set()  # 重複申込防止


def ログ記録(entry):
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    print(f"[{entry['timestamp']}] {entry['イベント']}: {entry.get('詳細', '')}")


async def run(dry_run=False, account_num=1):
    config = load_config()
    env_data = load_env()
    accounts = resolve_accounts(config, env_data, account_num)

    if not accounts:
        print("エラー: アカウント情報が見つかりません")
        return

    account = accounts[0]
    now = datetime.datetime.now()

    # 09:00〜09:30は待機（サーバー過負荷期間）
    if now.hour == 9 and now.minute < 30:
        残り秒 = (30 - now.minute) * 60 - now.second
        print(f"[待機] 09:00-09:30はサーバー混雑期間。{残り秒}秒後に開始します")
        await asyncio.sleep(残り秒)

    ログ記録({
        "timestamp": datetime.datetime.now().isoformat(),
        "イベント": "キャンセル監視開始",
        "詳細": f"dry_run={dry_run} account={account['label']}"
    })

    # 対象日: 4月の土日
    mon_cfg = config.get("monitoring", {})
    target_month = mon_cfg.get("target_month_spot", "2026-04")
    year, month = map(int, target_month.split("-"))
    weekdays = config.get("target_weekdays", [5, 6])

    from calendar import monthrange
    from datetime import date
    _, days = monthrange(year, month)
    today = date.today()
    対象日 = []
    for day in range(1, days + 1):
        dt = date(year, month, day)
        if dt >= today and dt.weekday() in weekdays:
            対象日.append(dt.strftime("%Y-%m-%d"))

    if not 対象日:
        print("対象日がありません")
        return

    print(f"対象日: {len(対象日)}日 ({', '.join(対象日[:5])}...)")
    print(f"対象施設: {len(config['target_facilities'])}校")

    session = MonitorSession()
    申込成功数 = 0
    max_applies = mon_cfg.get("max_spot_applications", 5)
    cycle = 0

    try:
        if not await session.start(account):
            print("セッション開始失敗")
            return

        while True:
            cycle += 1
            now = datetime.datetime.now()
            print(f"\n--- キャンセルスキャン #{cycle} [{now.strftime('%H:%M')}] ---")

            for 施設 in config["target_facilities"]:
                fc = 施設["code"]
                fn = 施設["name"]

                for 日付 in 対象日:
                    key = f"{fc}_{日付}"
                    if key in 検出済み:
                        continue

                    result = await session.check_facility_day(fc, fn, 日付)
                    available = result.get("available_slots", [])
                    status_texts = result.get("status_texts", [])

                    if available:
                        ログ記録({
                            "timestamp": now.isoformat(),
                            "イベント": "キャンセル枠検出",
                            "詳細": {
                                "施設": fn,
                                "日付": 日付,
                                "空き数": len(available),
                            }
                        })

                        if dry_run:
                            print(f"  [DRY-RUN] 申込スキップ: {fn} {日付}")
                            検出済み.add(key)
                        elif 申込成功数 >= max_applies:
                            print(f"  申込上限到達 ({申込成功数}/{max_applies})")
                        else:
                            apply_result = await session.apply_spot_via_ui(
                                fc, fn, 日付
                            )
                            ログ記録({
                                "timestamp": datetime.datetime.now().isoformat(),
                                "イベント": "申込実行",
                                "詳細": {
                                    "施設": fn,
                                    "日付": 日付,
                                    "結果": apply_result,
                                }
                            })
                            if apply_result == "applied":
                                申込成功数 += 1
                                print(f"  ★ 申込成功！ {fn} {日付} ({申込成功数}/{max_applies})")
                            検出済み.add(key)
                    else:
                        if status_texts:
                            print(f"  {fn} {日付}: {', '.join(status_texts[:3])}")

                    await asyncio.sleep(1)  # サーバー負荷軽減

            if 申込成功数 >= max_applies:
                print(f"\n申込上限到達（{申込成功数}件）。監視終了。")
                break

            # 5分待機
            print(f"[{now.strftime('%H:%M')}] 次回チェックまで5分待機")
            await asyncio.sleep(300)

    except KeyboardInterrupt:
        print("\n手動停止（Ctrl+C）")
    finally:
        await session.close()

        # 最終レポート
        ログ記録({
            "timestamp": datetime.datetime.now().isoformat(),
            "イベント": "キャンセル監視終了",
            "詳細": f"スキャン{cycle}回 / 申込成功{申込成功数}件"
        })


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="キャンセル枠監視 + 自動申込")
    parser.add_argument("--dry-run", action="store_true", help="検出のみ（申込しない）")
    parser.add_argument("--account", type=int, default=1, help="アカウント番号")
    args = parser.parse_args()
    asyncio.run(run(dry_run=args.dry_run, account_num=args.account))
