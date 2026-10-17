#!/usr/bin/env python3
"""鉄道撮影向け 休日の天気予報を計算して ntfy / メールで通知する。

GitHub Actions から毎日実行する想定。外部ライブラリ不要（標準ライブラリのみ）。
環境変数:
  NTFY_TOPIC   ntfy のトピック名（必須ではないが通知に必要）
  NTFY_SERVER  既定 https://ntfy.sh（自前サーバーなら https://ntfy.example.com）
  NTFY_TOKEN   （任意）自前サーバーでアクセス制御している場合のアクセストークン
  MAIL_TO, SMTP_USER, SMTP_PASS  （任意）Gmail 等でメール送信する場合
  DRY_RUN=1    送信せず標準出力に表示
"""
import datetime as dt
import json
import os
import smtplib
import sys
import urllib.parse
import urllib.request
from email.mime.text import MIMEText
from pathlib import Path

HERE = Path(__file__).resolve().parent
CONFIG = json.loads((HERE / "config.json").read_text(encoding="utf-8"))
_tp = HERE / "trains.json"
TRAINS = json.loads(_tp.read_text(encoding="utf-8")) if _tp.exists() else None
LABEL = {"sun": "晴れ", "partly": "晴れ間", "cloudy": "薄日", "overcast": "曇天",
         "drizzle": "小雨", "rain": "雨"}


def trains_at(station):
    """指定駅を通る狙いの列車を (時刻, 列車) の時刻順で返す。"""
    out = []
    for t in (TRAINS or {}).get("trains", []):
        for s in t["stops"]:
            if s["st"] == station:
                out.append((s["dep"] or s["arr"], t))
    return sorted(out, key=lambda x: x[0])
JST = dt.timezone(dt.timedelta(hours=9))
WD = "月火水木金土日"  # Python weekday(): 0=月

HOURLY = ["cloud_cover", "cloud_cover_low", "cloud_cover_mid", "cloud_cover_high",
          "precipitation", "wind_speed_10m"]

# ---------- 判定ロジック（index.html の JS と同じ） ----------
def effective_cloud(low, mid, high):
    """太陽を遮る実効雲量(%)。低層雲ほど強く効き、上層の巻雲は薄日程度とみなす。"""
    low, mid, high = (v or 0 for v in (low, mid, high))
    return 100 * (1 - (1 - low / 100) * (1 - 0.7 * mid / 100) * (1 - 0.3 * high / 100))


def hour_class(low, mid, high, precip):
    if precip is not None and precip >= 0.5:
        return "rain"
    if precip is not None and precip >= 0.1:
        return "drizzle"
    e = effective_cloud(low, mid, high)
    if e < 30:
        return "sun"
    if e < 60:
        return "partly"
    if e < 85:
        return "cloudy"
    return "overcast"


def day_verdict(classes, winds):
    sun = classes.count("sun") + 0.5 * classes.count("partly")
    wet = classes.count("rain") + 0.5 * classes.count("drizzle")
    if wet >= 3:
        mark, label = "✕", "雨"
    elif sun >= 5:
        mark, label = "◎", "晴れ"
    elif sun >= 2:
        mark, label = "○", "晴れ間あり"
    elif wet >= 1:
        mark, label = "△", "曇り時々雨"
    else:
        mark, label = "△", "曇り（編成記録向き）"
    # 晴れ間（sun/partly）の最長連続区間
    best, cur, start = (None, 0), 0, None
    for i, c in enumerate(classes):
        if c in ("sun", "partly"):
            start = i if cur == 0 else start
            cur += 1
            if cur > best[1]:
                best = (start, cur)
        else:
            cur = 0
    wind = max(winds) if winds else 0
    return {"mark": mark, "label": label, "sun_hours": sun, "best": best, "max_wind": wind}


# ---------- データ取得 ----------
def fetch(loc):
    q = {
        "latitude": loc["lat"], "longitude": loc["lon"],
        "hourly": ",".join(HOURLY), "daily": "sunrise,sunset",
        "models": "jma_seamless", "timezone": "Asia/Tokyo",
        "wind_speed_unit": "ms", "forecast_days": CONFIG.get("forecast_days", 11),
    }
    url = "https://api.open-meteo.com/v1/forecast?" + urllib.parse.urlencode(q)
    with urllib.request.urlopen(url, timeout=30) as r:
        return json.load(r)


def summarize(data, weekdays):
    """休日ごとに日中（日の出〜日の入り）の判定を返す。"""
    h = data["hourly"]
    d = data["daily"]
    out = {}
    for i, day in enumerate(d["time"]):
        date = dt.date.fromisoformat(day)
        if (date.isoweekday() % 7) not in weekdays:  # 0=日 … 6=土 に揃える
            continue
        rise = int(d["sunrise"][i][11:13])
        sset = int(d["sunset"][i][11:13])
        idx = [k for k, t in enumerate(h["time"])
               if t[:10] == day and rise <= int(t[11:13]) <= sset]
        if not idx or h["cloud_cover_low"][idx[0]] is None:
            continue  # 予報範囲外
        classes = [hour_class(h["cloud_cover_low"][k], h["cloud_cover_mid"][k],
                              h["cloud_cover_high"][k], h["precipitation"][k]) for k in idx]
        winds = [h["wind_speed_10m"][k] or 0 for k in idx]
        v = day_verdict(classes, winds)
        hours = [int(h["time"][k][11:13]) for k in idx]
        v["hours"] = dict(zip(hours, classes))
        v["sun"] = (d["sunrise"][i][11:16], d["sunset"][i][11:16])
        if v["best"][0] is not None:
            s = hours[v["best"][0]]
            v["window"] = f"{s}–{s + v['best'][1]}時"
        else:
            v["window"] = ""
        out[day] = v
    return out


def build_message(results):
    days = sorted({d for r in results.values() for d in r})
    lines = []
    for day in days:
        date = dt.date.fromisoformat(day)
        lines.append(f"{date.month}/{date.day}({WD[date.weekday()]})")
        for loc in CONFIG["locations"]:
            v = results[loc["id"]].get(day)
            if not v:
                continue
            extra = f" {v['window']}" if v["window"] and v["mark"] in "◎○" else ""
            wind = " 強風" if v["max_wind"] >= 8 else ""
            lines.append(f"  {loc['name']} {v['mark']} {v['label']}{extra}{wind}")
            st = (TRAINS or {}).get("watch_stations", {}).get(loc["id"])
            if st:
                parts = []
                for time, t in trains_at(st):
                    lit = v["sun"][0] <= time <= v["sun"][1]
                    c = v["hours"].get(int(time[:2])) if lit else None
                    arrow = "↑" if t["dir"] == "上り" else "↓"
                    parts.append(f"{arrow}{time}{LABEL[c] if c else '暗'}")
                if parts:
                    lines.append("   " + " ".join(parts))
    marks = [v["mark"] for r in results.values() for v in r.values()]
    title = "鉄道撮影予報"
    nearest = days[0] if days else None
    if nearest and any(results[l["id"]].get(nearest, {}).get("mark") == "◎" for l in CONFIG["locations"]):
        title += " ◎次の休みは晴れ"
    return title, "\n".join(lines) or "予報範囲内に休日がありません", ("◎" in marks)


# ---------- 送信 ----------
def send_ntfy(title, body, good):
    topic = os.environ.get("NTFY_TOPIC")
    if not topic:
        print("NTFY_TOPIC 未設定のため ntfy 送信をスキップ")
        return
    server = (os.environ.get("NTFY_SERVER") or "https://ntfy.sh").rstrip("/")
    payload = {"topic": topic, "title": title, "message": body,
               "tags": ["sunny" if good else "cloud", "train"],
               "priority": 4 if good else 3}
    if CONFIG.get("dashboard_url"):
        payload["click"] = CONFIG["dashboard_url"]
    headers = {"Content-Type": "application/json"}
    if os.environ.get("NTFY_TOKEN"):  # アクセス制御付きの自前サーバー用
        headers["Authorization"] = "Bearer " + os.environ["NTFY_TOKEN"]
    req = urllib.request.Request(server, data=json.dumps(payload).encode("utf-8"), headers=headers)
    urllib.request.urlopen(req, timeout=30).read()


def send_mail(title, body):
    to, user, pw = (os.environ.get(k) for k in ("MAIL_TO", "SMTP_USER", "SMTP_PASS"))
    if not (to and user and pw):
        return
    msg = MIMEText(body + ("\n\n" + CONFIG["dashboard_url"] if CONFIG.get("dashboard_url") else ""),
                   _charset="utf-8")
    msg["Subject"], msg["From"], msg["To"] = title, user, to
    with smtplib.SMTP_SSL(os.environ.get("SMTP_HOST", "smtp.gmail.com"), 465) as s:
        s.login(user, pw)
        s.send_message(msg)


def main():
    weekdays = set(CONFIG["weekdays"])
    results = {loc["id"]: summarize(fetch(loc), weekdays) for loc in CONFIG["locations"]}
    title, body, good = build_message(results)
    now = dt.datetime.now(JST).strftime("%m/%d %H:%M")
    body = f"{body}\n（{now} 気象庁モデル）"
    print(title)
    print(body)
    if os.environ.get("DRY_RUN"):
        return
    send_ntfy(title, body, good)
    send_mail(title, body)


if __name__ == "__main__":
    sys.exit(main())
