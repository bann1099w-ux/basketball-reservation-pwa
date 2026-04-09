#!/usr/bin/env python3
"""
切替タイミング調査.py
目的: 「前（受付前）」→「○（利用可）」の切替がいつ発生するかを記録する
実行: 25日22:00〜26日10:00まで自動ループ
記録: ~/O-TN-SUN/記憶/切替タイミングログ.jsonl

既存APIとの対応:
  - HarpSession.get_day_availability() で lotDisplayType=5（受付前）を検出
  - lotDisplayType=5 が消えたら「前→○」の切替と判定
"""

import asyncio
import json
import datetime
import os
import sys
from pathlib import Path

sys.path.append(os.path.expanduser("~/BIGBAN/ツール/バスケ自動申込"))
from harp_api import HarpSession, load_env, resolve_account

LOG_PATH = Path(os.path.expanduser("~/O-TN-SUN/記憶/切替タイミングログ.jsonl"))

# 調査対象: 代表施設5件（区を分散させる）
調査施設 = [
    {"name": "丘珠小学校",    "code": "0310"},
    {"name": "伏古小学校",    "code": "0293"},
    {"name": "札苗緑小学校",  "code": "0309"},
    {"name": "手稲東小学校",  "code": "0100"},
    {"name": "白石中学校",    "code": "0200"},
]

# 対象日: 4月の土日（先着申込開始後の直近4週）
対象日 = [
    "2026-04-04", "2026-04-05",
    "2026-04-11", "2026-04-12",
    "2026-04-18", "2026-04-19",
    "2026-04-25", "2026-04-26",
]

前回状態 = {}  # key: "施設名_日付", value: 受付前フラグ


def ログ記録(entry):
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    print(f"[記録] {entry['timestamp']} {entry['施設']} {entry['日付']} {entry['イベント']}")


def 状態解析(result):
    """GetDay API結果から各コマのlotDisplayType等を抽出"""
    if not result:
        return []
    コマ一覧 = []
    for room in result.get("rooms", []):
        for court in room.get("courts", []):
            for day in court.get("dayBooks", []):
                for time_slot in day.get("usageTimes", []):
                    コマ一覧.append({
                        "lotDisplayType": time_slot.get("lotDisplayType"),
                        "statusType": time_slot.get("statusType"),
                        "usageTimeFrameId": time_slot.get("usageTimeFrameId"),
                    })
    return コマ一覧


async def run():
    env_data = load_env()
    account = resolve_account(env_data, 1)
    if not account:
        print("エラー: アカウント情報が見つかりません")
        return

    harp = HarpSession()
    if not await harp.login_via_browser(account):
        print("ログイン失敗")
        return

    print("=== 切替タイミング調査 開始 ===")
    print(f"ログ: {LOG_PATH}")
    print("Ctrl+C で停止")

    ログ記録({
        "timestamp": datetime.datetime.now().isoformat(),
        "施設": "SYSTEM",
        "日付": "-",
        "イベント": "調査開始",
        "詳細": {}
    })

    try:
        while True:
            now = datetime.datetime.now()

            # 10:00を超えたら終了（切替は確実に完了しているはず）
            if now.hour >= 10 and now.day == 26:
                ログ記録({
                    "timestamp": now.isoformat(),
                    "施設": "SYSTEM",
                    "日付": "-",
                    "イベント": "調査終了（10:00到達）",
                    "詳細": {}
                })
                print("=== 10:00到達。調査完了 ===")
                break

            for 施設 in 調査施設:
                for 日付 in 対象日[:4]:  # 直近4日分だけ確認（負荷軽減）
                    key = f"{施設['name']}_{日付}"

                    try:
                        result = harp.get_day_availability(施設["code"], 日付)
                        コマ = 状態解析(result)
                    except Exception as e:
                        print(f"[ERROR] {施設['name']} {日付}: {e}")
                        continue

                    # lotDisplayType=5（受付前）が含まれるか
                    受付前フラグ = any(
                        k.get("lotDisplayType") == 5
                        for k in コマ if isinstance(k, dict)
                    )

                    if key not in 前回状態:
                        前回状態[key] = 受付前フラグ
                        status_label = "受付前" if 受付前フラグ else "受付中/空き"
                        print(f"  [初回] {施設['name']} {日付}: {status_label}")
                        continue

                    if 前回状態[key] is True and 受付前フラグ is False:
                        # 「前」→「○」の切替を検出！
                        ログ記録({
                            "timestamp": now.isoformat(),
                            "施設": 施設["name"],
                            "日付": 日付,
                            "イベント": "切替検出（前→○）",
                            "詳細": コマ[:3]
                        })

                    前回状態[key] = 受付前フラグ

                await asyncio.sleep(2)  # 施設間のアクセス間隔

            # ポーリング間隔
            if now.hour < 8 or (now.hour == 8 and now.minute < 50):
                待機秒 = 600  # 10分
            elif now.hour == 8 and now.minute >= 50:
                待機秒 = 60   # 1分（切替直前）
            elif now.hour == 9 and now.minute < 10:
                待機秒 = 60   # 1分
            else:
                待機秒 = 300  # 5分

            print(f"[{now.strftime('%H:%M:%S')}] 次回チェックまで {待機秒}秒待機")
            await asyncio.sleep(待機秒)

    except KeyboardInterrupt:
        ログ記録({
            "timestamp": datetime.datetime.now().isoformat(),
            "施設": "SYSTEM",
            "日付": "-",
            "イベント": "手動停止（Ctrl+C）",
            "詳細": {}
        })
        print("\n=== 手動停止 ===")


if __name__ == "__main__":
    asyncio.run(run())
