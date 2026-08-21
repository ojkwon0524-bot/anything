#!/usr/bin/env python3
"""
CGV IMAX 예매 오픈 알리미.

Asks CGV which dates a given movie is bookable at each theater. When a date
appears that wasn't there last time, it looks up that date's showtimes, keeps
the IMAX ones, and sends a Telegram message.

    python check.py --probe      # print current date horizons, no alerts
    python check.py --baseline   # record what's open now, no alerts
    python check.py              # normal run
"""

import argparse
import datetime
import json
import os
import random
import sys
import time

import requests

KST = datetime.timezone(datetime.timedelta(hours=9))

API = "https://cgv.co.kr/api/v1/booking"
CO_CD = "A420"

THEATERS = {
    "0013": "용산아이파크몰",
    "0074": "왕십리",
    "0199": "천호",
}

MOV_NO = os.environ.get("MOV_NO", "30001323")          # 오디세이
MOV_LABEL = os.environ.get("MOV_LABEL", "오디세이")
STATE_PATH = os.environ.get("STATE_PATH", "state.json")

HEADERS = {
    "accept": "application/json",
    "accept-language": "ko-KR",
    "referer": "https://cgv.co.kr/cnm/movieBook/movie",
    "user-agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36"
    ),
}


class CgvError(RuntimeError):
    pass


def get(path, params):
    """GET a CGV booking endpoint and return the `data` payload."""
    resp = requests.get(f"{API}/{path}", params=params, headers=HEADERS, timeout=25)
    if resp.status_code != 200:
        raise CgvError(f"{path}: HTTP {resp.status_code} — {resp.text[:200]}")
    try:
        body = resp.json()
    except ValueError:
        # Almost always a Cloudflare interstitial rather than real JSON.
        raise CgvError(f"{path}: non-JSON response — {resp.text[:200]}")
    if body.get("statusCode") != 0:
        raise CgvError(f"{path}: {body.get('statusMessage')}")
    return body.get("data") or []


def open_dates(site_no):
    """Dates (YYYYMMDD strings) currently bookable for MOV_NO at this theater."""
    data = get(
        "searchSiteScnscYmdListByMov",
        {
            "coCd": CO_CD,
            "siteNo": site_no,
            "movNo": MOV_NO,
            "div": "CUST_EXPO_MOVTYP_CD",
            "attrCd": "04",
        },
    )
    return sorted({row["scnYmd"] for row in data if row.get("scnYmd")})


def is_imax(row):
    haystack = " ".join(
        str(row.get(k) or "")
        for k in ("movkndDsplNm", "scnsNm", "expoScnsNm", "tcscnsGradNm")
    ).upper()
    return "IMAX" in haystack or "아이맥스" in haystack


def fmt_time(raw):
    """CGV writes past-midnight times as 2435. Render that as 00:35 (익일)."""
    raw = str(raw or "").zfill(4)
    try:
        hour, minute = int(raw[:2]), raw[2:]
    except ValueError:
        return raw
    if hour >= 24:
        return f"{hour - 24:02d}:{minute} (익일)"
    return f"{hour:02d}:{minute}"


def imax_showtimes(site_no, ymd):
    """IMAX showtimes for MOV_NO at this theater on this date."""
    data = get(
        "searchSchByMov",
        {
            "coCd": CO_CD,
            "siteNo": site_no,
            "scnYmd": ymd,
            "movNo": MOV_NO,
            "rtctlScopCd": "08",
        },
    )
    out = []
    for row in data:
        if not is_imax(row):
            continue
        try:
            raw_sort = int(row.get("scnsrtTm") or 0)
        except (TypeError, ValueError):
            raw_sort = 0
        out.append(
            {
                "sort": raw_sort,   # 2435 must sort after 1745, not before
                "time": fmt_time(row.get("scnsrtTm")),
                "screen": row.get("scnsNm") or "IMAX",
                "format": row.get("movkndDsplNm") or "",
                "seats_free": row.get("frSeatCnt"),
                "seats_total": row.get("stcnt"),
            }
        )
    out.sort(key=lambda s: s["sort"])
    return out


def pretty_date(ymd):
    try:
        d = datetime.date(int(ymd[:4]), int(ymd[4:6]), int(ymd[6:8]))
    except (ValueError, IndexError):
        return ymd
    return f"{d.month}/{d.day} ({'월화수목금토일'[d.weekday()]})"


# --------------------------------------------------------------------------
# State
# --------------------------------------------------------------------------

def load_state():
    if not os.path.exists(STATE_PATH):
        return {"movNo": MOV_NO, "seen": {}}
    with open(STATE_PATH, encoding="utf-8") as fh:
        state = json.load(fh)
    if state.get("movNo") != MOV_NO:  # tracking a different film — start clean
        return {"movNo": MOV_NO, "seen": {}}
    state.setdefault("seen", {})
    return state


def save_state(state):
    state["updated"] = datetime.datetime.now(KST).isoformat(timespec="seconds")
    with open(STATE_PATH, "w", encoding="utf-8") as fh:
        json.dump(state, fh, ensure_ascii=False, indent=2, sort_keys=True)


# --------------------------------------------------------------------------
# Telegram
# --------------------------------------------------------------------------

def notify(text):
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("[no telegram credentials — message would have been]\n" + text)
        return
    resp = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data={"chat_id": chat_id, "text": text, "disable_web_page_preview": True},
        timeout=20,
    )
    if resp.status_code != 200:
        print(f"telegram error {resp.status_code}: {resp.text}", file=sys.stderr)


def build_message(findings):
    lines = [f"🎬 {MOV_LABEL} — IMAX 예매 오픈!", ""]
    for theater, dates in findings.items():
        lines.append(f"▪ CGV {theater}")
        for ymd, shows in dates:
            if shows:
                times = "  ".join(
                    f"{s['time']}({s['seats_free']}/{s['seats_total']})" for s in shows
                )
                lines.append(f"  {pretty_date(ymd)}  {times}")
            else:
                lines.append(f"  {pretty_date(ymd)}  (IMAX 회차 없음)")
        lines.append("")
    lines.append("https://cgv.co.kr/cnm/movieBook/movie")
    return "\n".join(lines)


# --------------------------------------------------------------------------

def run(alerts=True, quiet=False):
    state = load_state()
    findings = {}
    errors = []

    for site_no, name in THEATERS.items():
        try:
            dates = open_dates(site_no)
        except CgvError as exc:
            errors.append(f"{name}: {exc}")
            print(f"  ! {name}: {exc}", file=sys.stderr)
            continue

        seen = set(state["seen"].get(site_no, []))
        fresh = [d for d in dates if d not in seen]

        horizon = max(dates) if dates else "—"
        print(f"{name}: {len(dates)} dates open, horizon {horizon}, {len(fresh)} new")

        if fresh and alerts:
            detail = []
            for ymd in fresh:
                try:
                    detail.append((ymd, imax_showtimes(site_no, ymd)))
                except CgvError as exc:
                    detail.append((ymd, []))
                    print(f"  ! showtimes {ymd}: {exc}", file=sys.stderr)
                time.sleep(random.uniform(0.4, 0.9))
            # Only shout if at least one new date actually has IMAX.
            if any(shows for _, shows in detail):
                findings[name] = detail

        state["seen"][site_no] = sorted(seen | set(dates))
        time.sleep(random.uniform(0.4, 0.9))

    if errors and len(errors) == len(THEATERS):
        # Everything failed — worth knowing about, the watcher is blind.
        notify("⚠️ CGV 알리미: 모든 극장 조회 실패\n\n" + "\n".join(errors))
        save_state(state)
        return 1

    save_state(state)

    if findings and alerts:
        notify(build_message(findings))
        print("ALERT SENT")
    elif not alerts and not quiet:
        total = sum(len(v) for v in state["seen"].values())
        print(f"baseline recorded: {total} dates across {len(THEATERS)} theaters")
    return 0


def probe():
    print(f"movNo={MOV_NO} ({MOV_LABEL})\n")
    ok = True
    for site_no, name in THEATERS.items():
        try:
            dates = open_dates(site_no)
        except CgvError as exc:
            print(f"{name:<12} FAILED — {exc}")
            ok = False
            continue
        if not dates:
            print(f"{name:<12} no dates (movie not showing here?)")
            continue
        print(f"{name:<12} {len(dates)} dates, {dates[0]} → {dates[-1]}")
        try:
            shows = imax_showtimes(site_no, dates[0])
            for s in shows:
                print(f"             {s['time']}  {s['screen']}  {s['format']}"
                      f"  {s['seats_free']}/{s['seats_total']}석")
            if not shows:
                print("             (no IMAX showtimes on the first date)")
        except CgvError as exc:
            print(f"             showtime lookup failed — {exc}")
            ok = False
        time.sleep(0.5)
    return 0 if ok else 1


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--probe", action="store_true")
    ap.add_argument("--baseline", action="store_true")
    args = ap.parse_args()
    if args.probe:
        sys.exit(probe())
    sys.exit(run(alerts=not args.baseline))
