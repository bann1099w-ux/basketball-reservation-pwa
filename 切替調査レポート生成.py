#!/usr/bin/env python3
"""
切替タイミング調査結果を集計してレポート生成
実行: 26日10:00（cron）または手動
"""

import json
import os
from datetime import datetime
from pathlib import Path

LOG_PATH = Path(os.path.expanduser("~/O-TN-SUN/記憶/切替タイミングログ.jsonl"))
REPORT_PATH = Path(os.path.expanduser("~/O-TN-SUN/記憶/2026-03-26_切替タイミング調査結果.md"))


def generate():
    if not LOG_PATH.exists():
        print("ログファイルが見つかりません")
        return

    with open(LOG_PATH, "r", encoding="utf-8") as f:
        lines = f.readlines()

    events = [json.loads(l) for l in lines if l.strip()]
    切替イベント = [e for e in events if "切替検出" in e.get("イベント", "")]
    開始時刻 = next((e["timestamp"] for e in events if e.get("イベント") == "調査開始"), "不明")
    終了時刻 = next((e["timestamp"] for e in events if "調査終了" in e.get("イベント", "") or "手動停止" in e.get("イベント", "")), "不明")

    report = []
    report.append("# 切替タイミング調査結果")
    report.append(f"調査日: 2026-03-25〜26")
    report.append(f"開始: {開始時刻}")
    report.append(f"終了: {終了時刻}")
    report.append(f"総イベント数: {len(events)}")
    report.append("")

    report.append("## 切替検出イベント")
    if 切替イベント:
        report.append(f"検出数: {len(切替イベント)}件")
        report.append("")
        report.append("| 時刻 | 施設 | 日付 |")
        report.append("|---|---|---|")
        for e in 切替イベント:
            report.append(f"| {e['timestamp']} | {e['施設']} | {e['日付']} |")
    else:
        report.append("切替検出なし（09:00以前に切替済みか、調査対象外のタイミングで発生）")

    report.append("")
    report.append("## 判断")
    if not 切替イベント:
        report.append("- 調査期間中に切替が観測されなかった")
        report.append("- 可能性: 09:00ちょうどにシステム一括切替、または調査前に切替済み")
    else:
        times = [datetime.fromisoformat(e["timestamp"]) for e in 切替イベント]
        earliest = min(times)
        report.append(f"- 最初の切替検出: {earliest.strftime('%Y-%m-%d %H:%M:%S')}")
        if earliest.hour < 9:
            report.append("- → 09:00以前に切替。次回は深夜監視が有効")
        elif earliest.hour == 9 and earliest.minute < 5:
            report.append("- → 09:00直後に切替。先着開始時刻と同時")
        else:
            report.append("- → 段階的切替。5分ポーリングで十分")

    report_text = "\n".join(report) + "\n"

    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(REPORT_PATH, "w", encoding="utf-8") as f:
        f.write(report_text)

    print(report_text)
    print(f"\n保存先: {REPORT_PATH}")


if __name__ == "__main__":
    generate()
