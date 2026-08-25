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

# KST times to camp on, comma separated "HH:MM". Empty by default: CGV has no
# fixed schedule (each theater opens dates manually). Set this only if the
# openings log in state.json shows a real pattern.
TARGET_TIMES = os.environ.get("TARGET_TIMES", "")
# How early the script starts standing at the door, and how long it waits after.
LEAD_MINUTES = int(os.environ.get("LEAD_MINUTES", "20"))
TRAIL_MINUTES = int(os.environ.get("TRAIL_MINUTES", "6"))
POLL_SECONDS = int(os.environ.get("POLL_SECONDS", "20"))

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
    try:
        with open(STATE_PATH, encoding="utf-8") as fh:
            state = json.load(fh)
    except (json.JSONDecodeError, OSError) as exc:
        # A hand-edit left the file unreadable. Don't crash and don't alert on
        # everything — rebuild quietly from what CGV says right now.
        print(f"!! {STATE_PATH} is unreadable ({exc})")
        print("   rebuilding it this run; no alerts will be sent this time.")
        return {"movNo": MOV_NO, "seen": {}, "recovered": True}
    if state.get("movNo") != MOV_NO:  # tracking a different film — start clean
        return {"movNo": MOV_NO, "seen": {}, "recovered": True}
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
    """Send to Telegram. Returns True only if Telegram actually accepted it."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        missing = [
            n for n, v in (("TELEGRAM_BOT_TOKEN", token), ("TELEGRAM_CHAT_ID", chat_id))
            if not v
        ]
        print(f"!! NOT SENT — missing secret(s): {', '.join(missing)}")
        print("   (check Settings > Secrets and variables > Actions > Secrets tab)")
        print("   message would have been:\n" + text)
        return False
    print(f"sending to chat {chat_id} using token ending ...{token[-6:]}")
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data={"chat_id": chat_id, "text": text, "disable_web_page_preview": True},
            timeout=20,
        )
    except requests.RequestException as exc:
        print(f"!! NOT SENT — network error: {exc}", file=sys.stderr)
        return False
    if resp.status_code != 200:
        print(f"!! NOT SENT — telegram {resp.status_code}: {resp.text}", file=sys.stderr)
        return False
    return True


def build_message(findings, detected=None):
    detected = detected or datetime.datetime.now(KST)
    lines = [f"🎬 {MOV_LABEL} — IMAX 예매 오픈!",
             f"감지: {detected:%m/%d} ({'월화수목금토일'[detected.weekday()]}) {detected:%H:%M}",
             ""]
    for site_no, (theater, dates) in findings.items():
        lines.append(f"▪ CGV {theater}")
        for ymd, shows in dates:
            if shows:
                times = "  ".join(
                    f"{s['time']}({s['seats_free']}/{s['seats_total']})" for s in shows
                )
                lines.append(f"  {pretty_date(ymd)}  {times}")
            else:
                lines.append(f"  {pretty_date(ymd)}  (IMAX 회차 없음)")
        # bzplcNo is siteNo + "001" — straight to this theater's page.
        lines.append(f"  https://cgv.co.kr/cnm/bzplcCgv/{site_no}001")
        lines.append("")
    lines.append("전체 예매: https://cgv.co.kr/cnm/movieBook/movie")
    return "\n".join(lines)


# --------------------------------------------------------------------------

def run(alerts=True, quiet=False):
    state = load_state()
    if state.pop("recovered", False):
        alerts = False
    findings = {}
    pending = {}
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
                findings[site_no] = (name, detail)
                pending[site_no] = fresh

        state["seen"][site_no] = sorted(seen | set(dates))
        time.sleep(random.uniform(0.4, 0.9))

    if errors and len(errors) == len(THEATERS):
        # Everything failed — worth knowing about, the watcher is blind.
        notify("⚠️ CGV 알리미: 모든 극장 조회 실패\n\n" + "\n".join(errors))
        save_state(state)
        return 1

    save_state(state)

    if findings and alerts:
        detected = datetime.datetime.now(KST)
        if notify(build_message(findings, detected)):
            print("ALERT DELIVERED")
            # Keep a running log of when openings actually happen. After a few
            # of these, a real pattern may show up — that's when TARGET_TIMES
            # becomes worth setting.
            log = state.setdefault("openings", [])
            for site_no, (theater, dates) in findings.items():
                log.append({
                    "detected": detected.isoformat(timespec="seconds"),
                    "weekday": "월화수목금토일"[detected.weekday()],
                    "theater": theater,
                    "dates": [ymd for ymd, _ in dates],
                })
            state["openings"] = log[-60:]
            save_state(state)
            return 2
        else:
            # Delivery failed — forget these dates so the next run retries
            # instead of silently swallowing the one alert that mattered.
            for site_no, fresh in pending.items():
                state["seen"][site_no] = sorted(
                    set(state["seen"].get(site_no, [])) - set(fresh)
                )
            save_state(state)
            print("ALERT FAILED — will retry next run")
            return 1
    elif not alerts and not quiet:
        total = sum(len(v) for v in state["seen"].values())
        print(f"baseline recorded: {total} dates across {len(THEATERS)} theaters")
    return 0


def parse_targets():
    """TARGET_TIMES -> list of (hour, minute), bad entries skipped."""
    out = []
    for chunk in TARGET_TIMES.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            h, m = chunk.split(":")
            h, m = int(h), int(m)
        except ValueError:
            print(f"  ! ignoring bad TARGET_TIMES entry: {chunk!r}")
            continue
        if 0 <= h < 24 and 0 <= m < 60:
            out.append((h, m))
    return out


def next_window(now):
    """
    The opening time this run should camp on, or None.

    Returns a target datetime if now is inside [target - LEAD, target + TRAIL].
    Checks yesterday/today/tomorrow so late-night targets work across midnight.
    """
    lead = datetime.timedelta(minutes=LEAD_MINUTES)
    trail = datetime.timedelta(minutes=TRAIL_MINUTES)
    for day_offset in (-1, 0, 1):
        day = now.date() + datetime.timedelta(days=day_offset)
        for hour, minute in parse_targets():
            target = datetime.datetime.combine(
                day, datetime.time(hour, minute), tzinfo=KST
            )
            if target - lead <= now <= target + trail:
                return target
    return None


def burst(target):
    """Poll tightly across an opening moment until something opens or time's up."""
    now = datetime.datetime.now(KST)
    end = target + datetime.timedelta(minutes=TRAIL_MINUTES)

    # Wake up a few seconds before the hour rather than exactly on it.
    start_at = target - datetime.timedelta(seconds=30)
    if now < start_at:
        wait = (start_at - now).total_seconds()
        print(f"target {target:%H:%M} KST — sleeping {int(wait)}s until {start_at:%H:%M:%S}")
        time.sleep(wait)
    else:
        print(f"target {target:%H:%M} KST — already inside the window, polling now")

    polls = 0
    while datetime.datetime.now(KST) <= end:
        polls += 1
        print(f"--- poll {polls} at {datetime.datetime.now(KST):%H:%M:%S} ---")
        rc = run()
        if rc == 2:
            print(f"opened on poll {polls}")
            return 0
        time.sleep(POLL_SECONDS + random.uniform(-2, 2))

    print(f"window closed after {polls} polls — nothing new")
    return 0
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
    ap.add_argument("--no-burst", action="store_true",
                    help="force a single check even inside an opening window")
    args = ap.parse_args()
    if args.probe:
        sys.exit(probe())
    if args.baseline:
        sys.exit(run(alerts=False))

    window = next_window(datetime.datetime.now(KST))
    if window and not args.no_burst:
        sys.exit(burst(window))
    rc = run()
    sys.exit(0 if rc == 2 else rc)
