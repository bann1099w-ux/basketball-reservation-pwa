#!/usr/bin/env python3
"""
harp_api_server.py
harp_api.py のFlask薄皮ラッパー（最小構成）

エンドポイント:
  GET  /api/ping        疎通確認
  POST /api/refresh     harp_api.py --status を実行（非同期）
  GET  /api/latest      最新のlogs/status_*.json を返す

使い方:
  python3 harp_api_server.py
  → http://localhost:5100 で起動

配置場所:
  ~/BIGBAN/ツール/バスケ自動申込/harp_api_server.py
"""

import json
import subprocess
import threading
from datetime import datetime, timezone, timedelta
from pathlib import Path
from flask import Flask, jsonify, request, send_from_directory

# === パス設定 ===
BASE_DIR  = Path(__file__).parent
LOG_DIR   = BASE_DIR / "logs"
SCRIPT    = BASE_DIR / "harp_api.py"

JST = timezone(timedelta(hours=9))

app = Flask(__name__)

# 実行中フラグ（二重実行防止）
_running = False

# ── CORS対応（スマホPWAからlocalhostへのリクエストを許可） ──
@app.after_request
def add_cors(response):
    response.headers["Access-Control-Allow-Origin"]  = "*"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return response

# ── 静的ファイル配信（home.html を同じオリジンから開くため）──
# http://100.77.24.4:5100/       → home.html
# http://100.77.24.4:5100/login  → login.html（将来統合用）
@app.route("/")
def index():
    return send_from_directory(BASE_DIR, "home.html")

@app.route("/<path:filename>")
def static_files(filename):
    return send_from_directory(BASE_DIR, filename)


@app.route("/api/ping", methods=["GET", "OPTIONS"])
def ping():
    """疎通確認"""
    return jsonify({"ok": True, "message": "harp_api_server 起動中"})


@app.route("/api/latest", methods=["GET"])
def latest():
    """
    logs/ フォルダ内の最新 status_*.json を返す。
    home.html はこれを読んで表示する。
    ?account=00156442 を指定すると、そのアカウントの最新ファイルを返す。
    """
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    files = sorted(LOG_DIR.glob("status_*.json"), reverse=True)

    if not files:
        return jsonify({
            "ok": False,
            "message": "まだデータがありません。更新ボタンを押してください。",
            "reserves": [],
        })

    target_account = request.args.get("account", "")

    # アカウント指定がある場合、該当アカウントの最新ファイルを探す
    latest_file = None
    if target_account:
        for f in files[:20]:  # 直近20件まで検索
            try:
                with open(f, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
                if data.get("account", "") == target_account:
                    latest_file = f
                    break
            except Exception:
                continue
    if not latest_file:
        latest_file = files[0]

    try:
        with open(latest_file, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except Exception as e:
        return jsonify({"ok": False, "message": f"ファイル読み込みエラー: {e}", "reserves": []}), 500

    # harp_api.py の出力形式 → home.html 用フォーマットに変換
    reserves = _convert_to_reserves(raw)

    return jsonify({
        "ok": True,
        "reserves":   reserves,
        "fetchedAt":  raw.get("timestamp", ""),
        "account":    raw.get("account", ""),
        "sourceFile": latest_file.name,
    })


@app.route("/api/refresh", methods=["POST", "OPTIONS"])
def refresh():
    """
    harp_api.py --status をバックグラウンドで実行する。
    実行中は二重起動しない。
    レスポンスはすぐ返す（実行完了を待たない）。
    完了後は /api/latest で結果を取得する。
    """
    global _running

    if request.method == "OPTIONS":
        return "", 204

    if _running:
        return jsonify({"ok": False, "message": "取得中です。しばらくお待ちください。"})

    # リクエストボディからアカウント番号を受け取る（省略時は1）
    body        = request.get_json(silent=True) or {}
    account_num = int(body.get("account", 1))

    def run():
        global _running
        _running = True
        try:
            result = subprocess.run(
                ["python3", str(SCRIPT), "--status", "--account", str(account_num)],
                capture_output=True,
                text=True,
                timeout=120,  # 最大2分
            )
            if result.returncode != 0:
                # エラーログを保存
                err_path = LOG_DIR / f"error_{datetime.now(JST).strftime('%Y%m%d_%H%M%S')}.txt"
                LOG_DIR.mkdir(parents=True, exist_ok=True)
                err_path.write_text(result.stderr or result.stdout, encoding="utf-8")
        except subprocess.TimeoutExpired:
            err_path = LOG_DIR / f"error_{datetime.now(JST).strftime('%Y%m%d_%H%M%S')}_timeout.txt"
            LOG_DIR.mkdir(parents=True, exist_ok=True)
            err_path.write_text("harp_api.py タイムアウト（120秒超過）", encoding="utf-8")
        except Exception as e:
            err_path = LOG_DIR / f"error_{datetime.now(JST).strftime('%Y%m%d_%H%M%S')}_exc.txt"
            LOG_DIR.mkdir(parents=True, exist_ok=True)
            err_path.write_text(str(e), encoding="utf-8")
        finally:
            _running = False

    threading.Thread(target=run, daemon=True).start()

    return jsonify({
        "ok": True,
        "message": "取得開始しました。15〜20秒後に /api/latest で結果を確認してください。",
        "account": account_num,
    })


@app.route("/api/status", methods=["GET"])
def status():
    """実行中かどうかを返す（home.html がポーリングに使う）"""
    return jsonify({"ok": True, "running": _running})


SETTINGS_FILE = BASE_DIR / "設定.json"


@app.route("/api/settings", methods=["GET", "POST", "OPTIONS"])
def settings_api():
    """
    PWAの施設選択・曜日・時間帯設定を設定.jsonと同期する。
    GET  → 設定.jsonから facilities / days / timeSlots を返す
    POST → 設定.jsonに facilities / days / timeSlots を書き込む
    """
    if request.method == "OPTIONS":
        return "", 204

    if request.method == "GET":
        try:
            with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                cfg = json.load(f)
        except Exception as e:
            return jsonify({"ok": False, "message": f"設定ファイル読み込みエラー: {e}"}), 500

        # target_facilities → facilities 形式に変換
        facilities = []
        for tf in cfg.get("target_facilities", []):
            facilities.append({
                "fc": tf.get("code", ""),
                "fn": tf.get("name", ""),
                "an": tf.get("area", ""),
            })

        # 曜日: target_weekdays (設定.jsonでは 5=金,6=土, JS側は 0=日,1=月...6=土)
        days = cfg.get("target_weekdays", [])

        # 時間帯: preferred_time_slot → timeSlots
        slot_map = {"午前": "morning", "午後": "afternoon", "夜間": "night"}
        preferred = cfg.get("preferred_time_slot", "")
        time_slots = [slot_map[preferred]] if preferred in slot_map else []

        # monitoring.preferred_periods からも取得
        period_map = {0: "morning", 1: "afternoon", 2: "night"}
        monitoring = cfg.get("monitoring", {})
        for p in monitoring.get("preferred_periods", []):
            ts = period_map.get(p, "")
            if ts and ts not in time_slots:
                time_slots.append(ts)

        return jsonify({
            "ok": True,
            "facilities": facilities,
            "days": days,
            "timeSlots": time_slots,
        })

    # POST: 設定を書き込む
    body = request.get_json(silent=True) or {}

    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except Exception as e:
        return jsonify({"ok": False, "message": f"設定ファイル読み込みエラー: {e}"}), 500

    # バックアップ
    try:
        backup = SETTINGS_FILE.parent / f"設定.json.bak_{datetime.now(JST).strftime('%Y%m%d_%H%M%S')}"
        backup.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        app.logger.warning(f"設定バックアップ失敗: {e}")

    # facilities → target_facilities（バリデーション付き）
    if "facilities" in body:
        if not isinstance(body["facilities"], list):
            return jsonify({"ok": False, "message": "facilitiesはリストである必要があります"}), 400
        validated = []
        for f in body["facilities"]:
            if not isinstance(f, dict):
                continue
            fc = str(f.get("fc", ""))[:10]
            fn = str(f.get("fn", ""))[:50]
            an = str(f.get("an", ""))[:20]
            validated.append({"code": fc, "name": fn, "area": an})
        cfg["target_facilities"] = validated

    # days → target_weekdays + target_weekday_names（バリデーション付き）
    if "days" in body:
        if not isinstance(body["days"], list):
            return jsonify({"ok": False, "message": "daysはリストである必要があります"}), 400
        day_names = {0: "日", 1: "月", 2: "火", 3: "水", 4: "木", 5: "金", 6: "土"}
        valid_days = [d for d in body["days"] if isinstance(d, int) and 0 <= d <= 6]
        cfg["target_weekdays"] = valid_days
        cfg["target_weekday_names"] = [day_names.get(d, "") for d in valid_days]

    # timeSlots → preferred_time_slot + monitoring.preferred_periods（バリデーション付き）
    if "timeSlots" in body:
        if not isinstance(body["timeSlots"], list):
            return jsonify({"ok": False, "message": "timeSlotsはリストである必要があります"}), 400
        slot_map_rev = {"morning": "午前", "afternoon": "午後", "night": "夜間"}
        period_rev = {"morning": 0, "afternoon": 1, "night": 2}
        slots = [s for s in body["timeSlots"] if isinstance(s, str) and s in slot_map_rev]
        if slots:
            cfg["preferred_time_slot"] = slot_map_rev.get(slots[-1], cfg.get("preferred_time_slot", ""))
        if "monitoring" not in cfg:
            cfg["monitoring"] = {}
        cfg["monitoring"]["preferred_periods"] = [period_rev[s] for s in slots if s in period_rev]

    # 書き戻し
    try:
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
            f.write("\n")
    except Exception as e:
        return jsonify({"ok": False, "message": f"書き込みエラー: {e}"}), 500

    updated_at = datetime.now(JST).isoformat()
    return jsonify({"ok": True, "updated_at": updated_at})


# ── 変換ヘルパー ──
def _convert_to_reserves(raw):
    """
    harp_api.py が保存する status_*.json を
    home.html が期待する形式に変換する。

    入力例:
      raw["statuses"]["lottery"]   = [ {"st":"L03","f":"伏古小","ud":"2026-04-13",...} ]
      raw["statuses"]["reservation"] = [ {"st":"R01","f":"丘珠小","ud":"2026-04-20",...} ]

    出力例:
      [
        {"date":"4月13日(日)", "place":"伏古小学校", "status":"pending"},
        {"date":"4月20日(日)", "place":"丘珠小学校", "status":"confirmed"},
      ]
    """
    STATUS_MAP = {
        "L01": "applied",    # 抽選受付中
        "L03": "pending",    # 抽選待ち
        "L05": "confirmed",  # 当選
        "L07": "lost",       # 落選
        "L08": "confirmed",  # 利用済み（確定扱い）
        "L09": "cancelled",  # 取消
        "R01": "confirmed",  # 予約確定
    }
    # 取消・利用終了は表示しない（L07はフロント側でON/OFF）
    SKIP = {"L09", "R06"}

    statuses = raw.get("statuses", {})
    all_records = (
        statuses.get("lottery", []) +
        statuses.get("reservation", [])
    )

    reserves = []
    seen = set()  # 重複除去（lottery + reservation で同一レコードが出ることがある）

    for rec in all_records:
        st = rec.get("st", "")
        if st in SKIP:
            continue

        # 日付整形: "2026-04-13" → "4月13日(日)"
        ud = rec.get("ud", "")[:10]
        date_label = _format_date(ud)

        # 施設名: f（施設名）+ r（部屋名）+ c（コート名）
        place = rec.get("f", "") or ""
        room  = rec.get("r", "")
        if room and room not in place:
            place = place + " " + room

        key = (ud, place)
        if key in seen:
            continue
        seen.add(key)

        reserves.append({
            "date":   date_label,
            "place":  place.strip(),
            "status": STATUS_MAP.get(st, "applied"),
            "st":     st,  # デバッグ用（home.htmlでは使わない）
            "ud":     ud,  # ソート用ISO日付
        })

    # 日付昇順にソート
    reserves.sort(key=lambda x: x.get("ud", ""))
    return reserves


def _format_date(date_str):
    """
    "2026-04-13" → "4月13日(日)"
    変換できない場合はそのまま返す
    """
    WEEKDAYS = ["月", "火", "水", "木", "金", "土", "日"]
    try:
        from datetime import date
        y, m, d = map(int, date_str.split("-"))
        dt = date(y, m, d)
        wd = WEEKDAYS[dt.weekday()]
        return f"{m}月{d}日({wd})"
    except Exception:
        return date_str


if __name__ == "__main__":
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    print("=" * 50)
    print("harp_api_server 起動")
    print(f"  ホーム画面 : http://100.77.24.4:5100/")
    print(f"  ローカル   : http://localhost:5100/")
    print(f"  スクリプト: {SCRIPT}")
    print(f"  ログ   : {LOG_DIR}")
    print("=" * 50)
    app.run(host="0.0.0.0", port=5100, debug=False)
