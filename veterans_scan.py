#!/usr/bin/env python3
"""
Veteran scanner for Kalshi.

1. Reads the Kalshi Sports profit leaderboard (all-time and year) through Apify.
2. Veterans = nicknames ranked in the top TOP_N of BOTH lists.
   (Falls back to the FOLLOW list if the leaderboard call fails or comes back empty.)
3. Pulls each veteran's profile + recent trades through Apify.
4. Writes data.json for the dashboard (index.html) and pushes ntfy alerts for
   fresh veteran buys.

Environment variables:
  APIFY_TOKEN     required (GitHub secret)
  NTFY_TOPIC      optional. Leave unset for website-only (no phone alerts)
  TOP_N           leaderboard depth, default 50
  MAX_VETS        max veterans to follow per run, default 15 (each costs scraper calls)
  FOLLOW          fallback comma list of nicknames
  VET_MIN_USD     minimum buy size to alert on, default 500
  FRESH_MIN       minimum freshness window in minutes (default 120); runs 4x a day, so the
                  window automatically stretches back to the previous run (max 12h)
  MAX_PRICE       skip buys above this price (0-1), default 0.95
  MAX_SLIP        skip if the live price is this far above their average entry (default 0.08)
  LB_INPUT_ALL    JSON input for the all-time leaderboard actor run (optional override)
  LB_INPUT_YEAR   JSON input for the yearly leaderboard actor run (optional override)
  DRY_RUN         1 = print alerts instead of sending

Run with --inspect once to print raw rows from Apify. Field names are found by
keyword, but if something looks wrong in data.json, that output shows why.
"""
import hashlib
import json
import os
import sys
import time
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

APIFY_TOKEN = os.environ.get("APIFY_TOKEN", "").strip()
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "").strip()
TOP_N = int(os.environ.get("TOP_N", "50"))
MAX_VETS = int(os.environ.get("MAX_VETS", "15"))
FOLLOW = [n.strip() for n in os.environ.get("FOLLOW", "").split(",") if n.strip()]
MIN_USD = float(os.environ.get("VET_MIN_USD", "500"))
FRESH_MIN = int(os.environ.get("FRESH_MIN", "120"))
MAX_PRICE = float(os.environ.get("MAX_PRICE", "0.95"))
MAX_SLIP = float(os.environ.get("MAX_SLIP", "0.08"))  # skip if price is >8c above their entry
DRY_RUN = os.environ.get("DRY_RUN", "0") == "1"
INSPECT = "--inspect" in sys.argv
STATE_FILE = "veterans_state.json"
OUT_FILE = "veterans-data.json"

LB_ACTOR = "saswave~kalshi-leaderboard-scraper"
PROFILE_ACTOR = "automation-lab~kalshi-trader-social-profile-scraper"
# These input names are my best guess; check the actor's Input tab on Apify and
# override with LB_INPUT_ALL / LB_INPUT_YEAR if they differ.
LB_ALL = {"leaderboard": "profit", "category": "sports", "timeframe": "all_time", "limit": TOP_N}
LB_YEAR = {"leaderboard": "profit", "category": "sports", "timeframe": "year", "limit": TOP_N}


def apify(actor, body):
    url = f"https://api.apify.com/v2/acts/{actor}/run-sync-get-dataset-items?token={APIFY_TOKEN}"
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            data = json.loads(r.read().decode())
        return data if isinstance(data, list) else []
    except Exception as e:
        print(f"[warn] Apify {actor} failed: {e}", file=sys.stderr)
        return []


def flat(o, path=""):
    if isinstance(o, dict):
        for k, v in o.items():
            yield from flat(v, f"{path}.{k}".lower())
    elif isinstance(o, list):
        for i, v in enumerate(o):
            yield from flat(v, f"{path}.{i}")
    else:
        yield path, o


def fget(row, *token_sets):
    """First scalar whose key path contains all tokens of any token set (in order of sets)."""
    items = list(flat(row))
    for toks in token_sets:
        for p, v in items:
            if v not in (None, "") and all(t in p for t in toks):
                return v
    return None


def num(v):
    try:
        return float(str(v).replace("$", "").replace(",", ""))
    except (TypeError, ValueError):
        return None


def money(n, signed=False):
    if n is None:
        return "n/a"
    a = abs(n)
    s = f"{a / 1e6:.2f}M" if a >= 1e6 else (f"{a / 1000:.1f}k".replace(".0k", "k") if a >= 1000 else f"{a:.0f}")
    return (("+" if n >= 0 else "−") if signed else "") + "$" + s


def parse_ts(v):
    if v is None:
        return None
    f = num(v)
    if f:
        return f / 1000 if f > 1e11 else f
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def ranks(rows):
    out = {}
    for i, r in enumerate(rows):
        nick = fget(r, ("nickname",), ("username",), ("name",))
        if nick:
            rk = num(fget(r, ("rank",))) or (i + 1)
            if rk <= TOP_N:
                out[str(nick)] = int(rk)
    return out


def veterans():
    all_in = json.loads(os.environ["LB_INPUT_ALL"]) if os.environ.get("LB_INPUT_ALL") else LB_ALL
    yr_in = json.loads(os.environ["LB_INPUT_YEAR"]) if os.environ.get("LB_INPUT_YEAR") else LB_YEAR
    a_rows, y_rows = apify(LB_ACTOR, all_in), apify(LB_ACTOR, yr_in)
    if INSPECT:
        print("--- leaderboard all-time (first 2) ---", json.dumps(a_rows[:2], indent=1)[:1200])
        print("--- leaderboard year (first 2) ---", json.dumps(y_rows[:2], indent=1)[:1200])
    a, y = ranks(a_rows), ranks(y_rows)
    both = sorted(set(a) & set(y), key=lambda n: a[n])[:MAX_VETS]
    if both:
        return [(n, a[n], y[n]) for n in both]
    print("[info] no leaderboard overlap; using FOLLOW list", file=sys.stderr)
    return [(n, None, None) for n in FOLLOW[:MAX_VETS]]


def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {"seen": [], "init": False}


def notify(title, body, url=None, priority="high"):
    if DRY_RUN:
        print(f"[ALERT] {title}\n        {body}")
        return
    if not NTFY_TOPIC:
        return  # alerts are optional; the website still updates
    h = {"Title": title.encode("ascii", "ignore").decode(), "Priority": priority, "Tags": "star"}
    if url:
        h["Click"] = url
    try:
        urllib.request.urlopen(urllib.request.Request(
            f"https://ntfy.sh/{NTFY_TOPIC}", data=body.encode(), headers=h, method="POST"), timeout=15).read()
    except Exception as e:
        print(f"[warn] ntfy failed: {e}", file=sys.stderr)


def trade_of(row):
    price = num(fget(row, ("price",)))
    if price and price > 1:
        price /= 100
    count = num(fget(row, ("count",), ("contracts",), ("quantity",), ("size",)))
    usd = num(fget(row, ("notional",), ("cost",), ("usd",), ("amount",)))
    if usd is None and price is not None and count is not None:
        usd = price * count
    action = str(fget(row, ("action",), ("type",)) or "buy").lower()
    ident = fget(row, ("trade", "id"), ("id",)) or hashlib.md5(json.dumps(row, sort_keys=True, default=str).encode()).hexdigest()
    return {
        "id": str(ident),
        "title": str(fget(row, ("market", "title"), ("title",), ("ticker",)) or "Unknown market"),
        "ticker": str(fget(row, ("ticker",)) or ""),
        "side": str(fget(row, ("side",), ("outcome",)) or "").upper(),
        "buy": "sell" not in action,
        "price": price, "usd": usd,
        "ts": parse_ts(fget(row, ("created",), ("timestamp",), ("time",), ("date",))),
    }


def event_key(t):
    parts = t["ticker"].split("-")
    if len(parts) >= 3:
        return "-".join(parts[:-1])
    return t["title"].split(":")[0].strip().lower()


def hedged_events(trades):
    """Events where the trader is on both sides: same market YES+NO, or opposing
    moneyline teams (2+ team-code tickers on the same side). Errs toward hiding."""
    by = defaultdict(list)
    for t in trades:
        if t["buy"]:
            by[event_key(t)].append(t)
    bad = set()
    for ev, ts in by.items():
        sides = defaultdict(set)
        for t in ts:
            sides[t["ticker"] or t["title"]].add(t["side"])
        if any(len(v) > 1 for v in sides.values()):
            bad.add(ev)
            continue
        for want in ("YES", "NO"):
            teams = {t["ticker"].split("-")[-1] for t in ts if t["side"] == want and t["ticker"]}
            if len([x for x in teams if x.isalpha()]) >= 2:
                bad.add(ev)
    return bad


def current_price(ticker, side):
    """Live price of the side they bought, from Kalshi's public market endpoint. None if unknown."""
    if not ticker:
        return None
    try:
        req = urllib.request.Request(f"https://api.elections.kalshi.com/trade-api/v2/markets/{ticker}",
                                     headers={"User-Agent": "kalshi-scanner/1.0"})
        with urllib.request.urlopen(req, timeout=15) as r:
            m = json.loads(r.read().decode()).get("market", {})
    except Exception:
        return None
    def px(*keys):
        for k in keys:
            v = num(m.get(k))
            if v is not None:
                return v / 100 if v > 1 else v
        return None
    if str(side).upper() == "NO":
        return px("no_ask_dollars", "no_ask")
    return px("yes_ask_dollars", "yes_ask")


def ago(sec):
    m = int(sec // 60)
    return f"{m}m" if m < 60 else f"{m // 60}h"


def main():
    if not APIFY_TOKEN:
        print("Set APIFY_TOKEN.", file=sys.stderr)
        sys.exit(1)

    vets = veterans()
    if not vets:
        print("No veterans found (empty leaderboard and no FOLLOW list).")
        return
    rows = apify(PROFILE_ACTOR, {"nicknames": [v[0] for v in vets], "modes": ["profile", "trades"]})
    if INSPECT:
        print("--- profile/trade rows (first 4) ---", json.dumps(rows[:4], indent=1, default=str)[:2500])
        return

    by = {v[0]: {"profile": None, "trades": []} for v in vets}
    for r in rows:
        nick = str(fget(r, ("nickname",)) or "")
        if nick not in by:
            continue
        kind = str(r.get("recordType", "")).lower()
        if kind.startswith("trade"):
            by[nick]["trades"].append(r)
        elif kind.startswith("profile"):
            by[nick]["profile"] = r

    state = load_state()
    first = not state.get("init")
    seen, now, sent, cappers = set(state["seen"]), time.time(), 0, []
    last = state.get("last")
    fresh_secs = min(max(now - last + 300, FRESH_MIN * 60), 12 * 3600) if last else FRESH_MIN * 60

    for nick, a_rk, y_rk in vets:
        prof = by[nick]["profile"] or {}
        pnl = num(fget(prof, ("all", "time", "pnl"), ("all", "time", "profit"), ("pnl",), ("profit",)))
        picks = []
        trades = [trade_of(raw) for raw in by[nick]["trades"]]
        hedge = hedged_events(trades)
        skipped = len({event_key(t) for t in trades} & hedge)
        groups = defaultdict(lambda: {"usd": 0.0, "pxw": 0.0, "ids": [], "ts": 0, "t": None})
        for t in trades:
            if event_key(t) in hedge:
                seen.add(t["id"])
                continue
            fresh_age = (now - t["ts"]) if t["ts"] else None
            if t["buy"] and t["usd"] and fresh_age is not None and fresh_age <= fresh_secs:
                g = groups[(t["ticker"] or t["title"], t["side"])]
                g["usd"] += t["usd"]
                g["pxw"] += (t["price"] or 0) * t["usd"]
                g["ids"].append(t["id"])
                g["ts"] = max(g["ts"], t["ts"])
                g["t"] = t
        for g in sorted(groups.values(), key=lambda x: -x["usd"]):
            t = g["t"]
            entry = g["pxw"] / g["usd"] if g["usd"] else 0
            if g["usd"] < MIN_USD or entry > MAX_PRICE:
                continue
            cur = current_price(t["ticker"], t["side"])
            moved = (cur - entry) if cur is not None else None
            if moved is not None and moved > MAX_SLIP:
                continue  # price already ran past their entry: no edge left
            age = now - g["ts"]
            picks.append(["Kalshi", "Position", "—", t["title"],
                          f"{t['side'] or 'BUY'} @ {int(entry * 100)}c" + (f" → now {int(cur * 100)}c" if cur is not None else ""),
                          round(g["usd"]), round(1 / entry, 1) if entry else 0, ago(age)])
            new_ids = [i for i in g["ids"] if i not in seen]
            if new_ids and not first:
                notify(f"[KALSHI] Veteran {nick}: BUY {money(g['usd'])}",
                       f"{t['title']}\n{t['side']} @ {int(entry * 100)}c avg, {len(g['ids'])} fills"
                       + (f", now {int(cur * 100)}c" if cur is not None else "")
                       + (f"\nall-time {money(pnl, True)}" if pnl is not None else "")
                       + (f"  |  #{a_rk} all-time" if a_rk else ""),
                       f"https://kalshi.com/profile/{nick}")
                sent += 1
        for t in trades:
            seen.add(t["id"])
        rank_txt = f"#{a_rk} all-time · #{y_rk} this year" if a_rk else "from your follow list"
        cappers.append({"n": nick, "wr": 0, "rec": f"{money(pnl, True)} all-time" if pnl is not None else "Veteran",
                        "sub": rank_txt + (f" · {skipped} two-sided skipped" if skipped else ""), "tag": "Veteran",
                        "st": [[money(pnl, True), "All-time P&L"], [f"#{a_rk}" if a_rk else "—", "Sports rank"]],
                        "p": picks})

    state["seen"] = list(seen)[-4000:]
    state["init"] = True
    state["last"] = now
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)
    stamp = datetime.now(timezone.utc).astimezone(ZoneInfo("America/Los_Angeles")).strftime("%a %b %-d · %-I:%M %p PT")
    with open(OUT_FILE, "w") as f:
        json.dump({"scanned": stamp, "ts": int(now), "cappers": cappers}, f)
    print(f"Done. {len(vets)} veterans, {sum(len(c['p']) for c in cappers)} fresh picks, {sent} alerts sent."
          + (" (first run: state seeded)" if first else ""))


if __name__ == "__main__":
    main()
