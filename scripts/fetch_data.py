#!/usr/bin/env python3
"""Server-side data fetch for the Whale Scanner (runs in GitHub Actions).
Browsers block some APIs (CORS); this runs on a server and writes small JSON
files into data/ that the page reads from the same site."""
import json, os, sys, time, urllib.request, urllib.error, urllib.parse

ORCA = "https://orcalayer.com"
LB_PATHS = ["/api/public/v1/whales/leaderboard", "/api/v2/whales/leaderboard",
            "/api/v2/leaderboard", "/api/v2/whale-leaderboard"]
DATA_API = "https://data-api.polymarket.com"
KALSHI = "https://api.elections.kalshi.com/trade-api/v2"
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data")
TOP = int(os.environ.get("TOP_WALLETS", "200"))
KALSHI_MIN_USD = float(os.environ.get("KALSHI_MIN_USD", "499"))


UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/124.0 Safari/537.36")


def get(url, headers=None, tries=3):
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers=dict({"Accept": "application/json", "User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"},
                                                 **({"Origin": "https://polymarket.com", "Referer": "https://polymarket.com/"} if "polymarket" in url else {}), **(headers or {})))
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            last = e
            if e.code in (429, 502, 503):
                time.sleep(2 * (i + 1))
                continue
            raise
        except Exception as e:  # network
            last = e
            time.sleep(1 + i)
    raise last


def rows_of(j):
    if isinstance(j, list):
        return j
    if isinstance(j, dict):
        for k in ("whales", "data", "results", "leaderboard", "rows", "items"):
            if isinstance(j.get(k), list):
                return j[k]
    return []


def wallet_of(r):
    for k in ("wallet", "proxy_wallet", "wallet_address", "user", "address"):
        if r.get(k):
            return str(r[k]).lower()
    w = r.get("whale")
    if isinstance(w, dict):
        return wallet_of(w)
    return ""


def clip(v, lo=0.0, hi=1.0):
    return max(lo, min(hi, v))


def quality(r, now=None):
    """0-100 copy-quality score for a sports wallet. Built on skill, not size:
    skill z-score, ROI, edge per bet, sample size and profit factor, with
    penalties for styles you can't copy in time (in-play / fast)."""
    import math
    now = now or time.time()
    res = max(int(r.get("resolved_markets") or 0), int(r.get("market_wins") or 0) + int(r.get("market_losses") or 0))
    vol = float(r.get("total_volume") or 0)
    pnl = float(r.get("total_pnl") or 0)
    roi = pnl / vol if vol > 0 else 0.0
    z = float(r.get("skill_z") or 0)
    edge = float(r.get("skill_edge_cents") or 0)
    pf = min(float(r.get("profit_factor") or 0), 10.0)
    s = (0.30 * clip(z / 8) + 0.25 * clip(roi / 0.10) + 0.15 * clip(edge / 10)
         + 0.15 * clip(math.log10(max(res, 1)) / 3.5) + 0.15 * clip((pf - 1) / 2))
    st = ",".join(r.get("styles") or []).lower()
    copy = "ok"
    if "fast" in st:
        s *= 0.55; copy = "fast"
    elif "inplay" in st:
        s *= 0.85; copy = "inplay"
    days = (now - float(r.get("last_trade_ts") or 0)) / 86400
    if days > 7:
        s *= 0.85
    if float(r.get("avg_entry_price") or 0.5) > 0.80:
        s *= 0.85
    return round(100 * s), {"res": res, "roi": round(roi, 4), "days": round(days, 1), "copy": copy}


def sports_pool():
    """Top sports wallets by copy-quality, not by all-time P&L. Drops inactive
    wallets, market makers / LPs, favorite farmers and tiny samples."""
    now, raw, seen = time.time(), [], set()
    for off in range(0, 1000, 100):
        try:
            got = rows_of(get(f"{ORCA}/api/v2/whales/leaderboard?category=sports&limit=100&offset={off}"))
        except Exception as e:
            print("sports lb fail", off, e, file=sys.stderr)
            break
        if not got:
            break
        raw += got
        time.sleep(0.3)
    pool = []
    for r in raw:
        w = wallet_of(r)
        if not w or w in seen:
            continue
        seen.add(w)
        q, d = quality(r, now)
        st = ",".join(r.get("styles") or []).lower()
        if (d["days"] > float(os.environ.get("MAX_IDLE_DAYS", "7")) or d["res"] < 20 or d["roi"] < 0.01 or float(r.get("profit_factor") or 0) < 1.1
                or float(r.get("total_pnl") or 0) < 5000 or r.get("liquidity_provider")
                or "maker" in st or "mm" in st.split(",") or float(r.get("avg_entry_price") or 0.5) > 0.88):
            continue
        r.update({"q": q, "roi": d["roi"], "copy": d["copy"], "res": d["res"]})
        pool.append(r)
    pool.sort(key=lambda r: -r["q"])
    print("sports pool:", len(pool), "of", len(raw))
    return pool


def activity(closed_sorted, window=14):
    """Share of days with settled bets over the last `window` days (0-1).
    If the latest 50 bets all fall inside the window, measure over their span."""
    now = time.time()
    ts = []
    for p in closed_sorted:
        t = float(p.get("timestamp") or 0)
        if t > 1e11:
            t /= 1000
        if t and now - t <= window * 86400:
            ts.append(t)
    if not ts:
        return 0.0
    days = {int(t // 86400) for t in ts}
    span = window
    if len(ts) == len(closed_sorted) and len(ts) >= 40:
        span = max(1, min(window, int((now - min(ts)) // 86400) + 1))
    return round(min(1.0, len(days) / span), 2)


def dead_open(open_rows, now=None):
    """Polymarket leaves LOSING positions 'open' at ~0c (nobody redeems a loser), so a closed-only
    history is mostly wins. Return resolved-but-unredeemed open positions as settled rows."""
    import datetime
    now = now or time.time()
    out = []
    for p in open_rows or []:
        c, cost = num(p.get("curPrice")), num(p.get("initialValue")) or 0
        if c is None or cost <= 0:
            continue
        t = 0
        try:
            t = datetime.datetime.strptime(str(p.get("endDate") or "")[:10], "%Y-%m-%d").replace(tzinfo=datetime.timezone.utc).timestamp()
        except Exception:
            pass
        over = bool(p.get("redeemable")) or (t and t < now - 6 * 3600)
        if not over or not (c <= 0.02 or c >= 0.98):
            continue
        win = c >= 0.98
        cv, ap = num(p.get("currentValue")) or 0, num(p.get("avgPrice"))
        pnl = (cv - cost if cv > 0 else (cost * (1 / ap - 1) if ap else 0.01)) if win else -cost
        out.append({"title": p.get("title"), "outcome": p.get("outcome"), "avgPrice": ap,
                    "realizedPnl": max(pnl, 0.01) if win else pnl, "timestamp": (t + 86399) if t else now - 86400,
                    "slug": p.get("slug"), "unred": 1})
    return out


def settled(closed_rows, open_rows):
    seen, out = set(), []
    for p in list(closed_rows or []) + dead_open(open_rows):
        k = (str(p.get("title") or "").lower(), str(p.get("outcome") or "").lower())
        if k in seen:
            continue
        seen.add(k)
        out.append(p)
    return out


def recent_form(lb, closed, pos=None):
    """Blend lifetime skill with the last 30 settled bets, so a wallet with a
    great history but a cold current run drops down the list."""
    for r in lb:
        w = wallet_of(r)
        allc = sorted(settled(closed.get(w), (pos or {}).get(w)), key=lambda p: -(float(p.get("timestamp") or 0)))
        act = activity(allc)
        c = allc[:30]
        res = [float(p.get("realizedPnl") or 0) for p in c if float(p.get("realizedPnl") or 0) != 0]
        r["active_days"] = act
        amult = 1.1 if act >= 0.7 else 1.0 if act >= 0.5 else 0.75 if act >= 0.3 else 0.5
        if len(res) < 5:
            r["recent"] = {"n": len(res), "act": act}
            r["drop"] = "too few recent bets"
            continue
        w = sum(1 for x in res if x > 0)
        pnl = sum(res)
        mult = 1.0
        if pnl < 0 and w / len(res) < 0.45:
            mult, tag = 0.5, "cold"
        elif pnl < 0:
            mult, tag = 0.8, "cooling"
        elif w / len(res) >= 0.55:
            mult, tag = 1.08, "hot"
        else:
            tag = "steady"
        r["q_life"] = r["q"]
        r["q"] = min(100, round(r["q"] * mult * amult))
        r["recent"] = {"n": len(res), "w": w, "pnl": round(pnl), "form": tag, "act": act}
        if pnl < 0 and w / len(res) < 0.45:
            r["drop"] = "losing lately"
    dropped = [r for r in lb if r.get("drop")]
    if len(lb) - len(dropped) >= 15:
        lb[:] = [r for r in lb if not r.get("drop")]
    print("recent-form drops:", len(dropped), "kept:", len(lb))
    lb.sort(key=lambda r: -r["q"])
    for i, r in enumerate(lb):
        r["rank"] = i + 1


def leaderboard():
    pool = sports_pool()
    if len(pool) >= 30:
        for i, r in enumerate(pool):
            r["rank"] = i + 1
        return pool
    rows, seen = [], set()
    for qs in ("limit=100", "limit=100&offset=100"):
        got = []
        for p in LB_PATHS:
            try:
                got = rows_of(get(ORCA + p + "?" + qs))
            except Exception as e:
                print("lb fail", p, qs, e, file=sys.stderr)
                continue
            if got:
                break
        for r in got:
            w = wallet_of(r)
            if w and w not in seen:
                seen.add(w)
                rows.append(r)
        if not got:
            break
    for i, r in enumerate(rows):
        r["rank"] = i + 1
    return rows


POS_KEYS = ("title", "outcome", "avgPrice", "curPrice", "currentValue", "initialValue",
            "cashPnl", "percentPnl", "endDate", "redeemable", "slug", "boughtAt", "lastAt")
CLOSED_KEYS = ("title", "outcome", "avgPrice", "realizedPnl", "timestamp", "slug")


def slim(rows, keys):
    return [{k: r.get(k) for k in keys if k in r} for r in rows if isinstance(r, dict)]


def post(url, body, headers=None):
    h = {"Accept": "application/json", "Content-Type": "application/json", "User-Agent": UA}
    h.update(headers or {})
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST", headers=h)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def pick(o, *keys):
    for k in keys:
        v = o.get(k)
        if v is not None and v != "":
            return v
    return None


def num(v):
    try:
        return float(v)
    except Exception:
        return None


def norm_pos(r):
    m = r.get("market") if isinstance(r.get("market"), dict) else {}
    title = pick(r, "title", "market_title", "question", "market_question", "name") or m.get("question") or m.get("title") \
        or (r.get("market") if isinstance(r.get("market"), str) else "") or ""
    cv = num(pick(r, "currentValue", "current_value", "value", "usd_value", "market_value"))
    iv = num(pick(r, "initialValue", "initial_value", "cost", "cost_basis", "invested", "initial_cost"))
    ap = num(pick(r, "avgPrice", "avg_price", "average_price", "entry_price", "avg_entry_price"))
    cp = num(pick(r, "curPrice", "cur_price", "current_price", "price", "mark_price"))
    sh = num(pick(r, "size", "shares", "quantity", "position_size"))
    if cv is None and sh is not None and cp is not None:
        cv = sh * cp
    if iv is None and sh is not None and ap is not None:
        iv = sh * ap
    return {"title": str(title), "outcome": str(pick(r, "outcome", "outcome_name", "side", "position") or ""),
            "avgPrice": ap, "curPrice": cp, "currentValue": cv, "initialValue": iv,
            "cashPnl": num(pick(r, "cashPnl", "pnl", "unrealized_pnl", "cash_pnl")),
            "endDate": pick(r, "endDate", "end_date", "end_time"), "slug": pick(r, "slug", "event_slug") or m.get("slug"),
            "redeemable": bool(pick(r, "redeemable", "is_resolved", "resolved"))}


ORCA_FORM = [None]


def orca_positions(w):
    key = os.environ.get("ORCA_KEY", "")
    forms = ["wallets", "addresses", "wallet", "wallet_addresses"]
    if ORCA_FORM[0]:
        forms = [ORCA_FORM[0]] + [f for f in forms if f != ORCA_FORM[0]]
    last = None
    for f in forms:
        for hdr in ([{"Authorization": "Bearer " + key, "X-API-Key": key}] if key else []) + [{}]:
            try:
                j = post(ORCA + "/api/public/v1/wallets/positions", {f: (w if f == "wallet" else [w])}, hdr)
            except Exception as e:
                last = e
                continue
            rows = None
            if isinstance(j, list):
                rows = j
            elif isinstance(j, dict):
                q = j.get(w) or (j.get("results") or {}).get(w) if isinstance(j.get("results"), dict) else j.get(w)
                if isinstance(q, list):
                    rows = q
                elif isinstance(q, dict) and isinstance(q.get("positions"), list):
                    rows = q["positions"]
                elif isinstance(j.get("positions"), list):
                    rows = j["positions"]
                else:
                    a = rows_of(j)
                    if a and isinstance(a[0], dict) and ("positions" in a[0] or "wallet" in a[0]):
                        z = next((y for y in a if str(y.get("wallet") or y.get("address") or "").lower() == w), a[0])
                        rows = z.get("positions") or []
                    else:
                        rows = a
            if rows is not None:
                ORCA_FORM[0] = f
                return [p for p in (norm_pos(r) for r in rows if isinstance(r, dict)) if p["title"]]
    raise last or RuntimeError("orca positions failed")


def v2_rows(w, status, limit):
    j = get(f"{DATA_API}/v2/positions?user={w}&status={status}&limit={limit}" + ("&sort_by=TIMESTAMP&sort_direction=DESC" if status == "CLOSED" else ""))
    return rows_of(j)


def v2_deep(w, status, sizes):
    """Ask for the deepest history first; fall back to smaller pages if the API refuses a big limit."""
    last = None
    for n in sizes:
        try:
            return v2_rows(w, status, n)
        except Exception as e:
            last = e
    raise last


def v2_year(w, max_rows=400):
    """Closed bets going back up to a year (pages with offset; stops if the API ignores offset)."""
    rows = v2_deep(w, "CLOSED", (100, 50))
    first = (rows[0].get("title"), rows[0].get("outcome")) if rows else None
    cutoff = time.time() - 365 * 86400
    off = len(rows)
    while rows and len(rows) < max_rows and off >= 50:
        oldest = min(tsec(r.get("last_event_at") or r.get("timestamp")) or time.time() for r in rows)
        if oldest < cutoff:
            break
        try:
            more = rows_of(get(f"{DATA_API}/v2/positions?user={w}&status=CLOSED&limit=100&offset={off}&sort_by=TIMESTAMP&sort_direction=DESC"))
        except Exception:
            break
        if not more or (more[0].get("title"), more[0].get("outcome")) == first:
            break
        rows += more
        off += len(more)
        if len(more) < 50:
            break
        time.sleep(0.2)
    return rows


def tsec(x):
    try:
        v = float(x)
        return v / 1000 if v > 1e11 else v
    except Exception:
        pass
    try:
        import datetime
        return datetime.datetime.fromisoformat(str(x).replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0


def period_stats(rows):
    """P&L over the last 30 days and the last 365 days of settled bets (losses included)."""
    now = time.time()
    out = {"p30": 0.0, "n30": 0, "p365": 0.0, "n365": 0, "days": 0}
    ts = []
    for p in rows:
        x = float(p.get("realizedPnl") or 0)
        t = tsec(p.get("timestamp"))
        if not x or not t:
            continue
        ts.append(t)
        age = now - t
        if age <= 365 * 86400:
            out["p365"] += x
            out["n365"] += 1
        if age <= 30 * 86400:
            out["p30"] += x
            out["n30"] += 1
    out["days"] = round((now - min(ts)) / 86400) if ts else 0
    out["p30"], out["p365"] = round(out["p30"]), round(out["p365"])
    return out


def from_v2(r):
    """Convert a v2 (snake_case) row to the camelCase shape the page already reads."""
    def g(*k):
        for x in k:
            if r.get(x) is not None:
                return r.get(x)
    ent = num(g("entry_cost_usdc", "total_cost_usdc"))
    return {"title": g("title"), "outcome": g("outcome"), "avgPrice": num(g("avg_price")),
            "curPrice": num(g("current_price")), "currentValue": num(g("current_value")),
            "initialValue": ent, "cashPnl": num(g("unrealized_pnl", "total_pnl")),
            "realizedPnl": num(g("realized_pnl", "total_pnl")), "endDate": g("end_date"),
            "slug": g("slug"), "redeemable": bool(g("redeemable")),
            "timestamp": g("last_event_at", "first_entry_at"),
            "boughtAt": g("first_entry_at"), "lastAt": g("last_event_at")}


def wallets_data(wallets):
    pos, closed = {}, {}
    for i, w in enumerate(wallets):
        try:
            pos[w] = slim([from_v2(r) for r in v2_deep(w, "OPEN", (100, 60))], POS_KEYS)
        except Exception as e:
            print("positions fail", w, e, file=sys.stderr)
            try:
                pos[w] = orca_positions(w)
            except Exception as e2:
                print("orca positions fail", w, e2, file=sys.stderr)
        try:
            full = [from_v2(r) for r in v2_year(w)]
            STATS[w] = period_stats(settled(full, pos.get(w)))
            closed[w] = slim(full[:100], CLOSED_KEYS)
        except Exception as e:
            print("closed fail", w, e, file=sys.stderr)
        time.sleep(0.25)
    return pos, closed


ERR = [""]
STATS = {}


def period_filter(lb):
    """Keep only cappers who are UP over the last 30 days AND the last 12 months."""
    for r in lb:
        st = STATS.get(wallet_of(r))
        if not st:
            continue
        r["per"] = st
        if st["n30"] < 3:
            r["drop"] = r.get("drop") or "too few bets this month"
        elif st["p30"] <= 0:
            r["drop"] = r.get("drop") or "down this month"
        elif st["p365"] <= 0:
            r["drop"] = r.get("drop") or "down over 12 months"
    dropped = [r for r in lb if r.get("drop")]
    if len(lb) - len(dropped) >= 15:
        lb[:] = [r for r in lb if not r.get("drop")]
    print("period drops:", len(dropped), "kept:", len(lb))
    for i, r in enumerate(lb):
        r["rank"] = i + 1


def kalshi_combos():
    meta, trades = {}, []
    try:
        j = get(KALSHI + "/markets?mve_filter=only&status=open&limit=1000")
        for m in j.get("markets", []):
            meta[m["ticker"]] = {"yes_sub_title": m.get("yes_sub_title"), "title": m.get("title"),
                                 "mve_selected_legs": m.get("mve_selected_legs") or []}
    except Exception as e:
        print("kalshi markets fail", e, file=sys.stderr); ERR[0] += "markets: " + str(e)[:120] + " "
    cursor, pages = "", 0
    since = int(time.time()) - 3 * 3600
    while pages < 30:
        q = f"{KALSHI}/markets/trades?limit=1000&min_ts={since}" + (f"&cursor={urllib.parse.quote(cursor)}" if cursor else "")
        try:
            j = get(q)
        except Exception as e:
            print("kalshi trades fail", e, file=sys.stderr); ERR[0] += "trades: " + str(e)[:120] + " "
            break
        trades += j.get("trades", [])
        pages += 1
        cursor = j.get("cursor") or ""
        if not cursor:
            break

    def price(t, side):
        for k in (side + "_price_dollars", side + "_price_fixed"):
            if t.get(k) is not None:
                try:
                    return float(t[k])
                except Exception:
                    pass
        c = t.get(side + "_price")
        return c / 100.0 if c is not None else float("nan")

    big, combo = [], 0
    for t in trades:
        tk = str(t.get("ticker", ""))
        if not (tk in meta or tk.upper().startswith("KXMVE")):
            continue
        combo += 1
        try:
            n = float(t.get("count_fp") if t.get("count_fp") is not None else t.get("count"))
        except Exception:
            continue
        side = "no" if str(t.get("taker_side", "yes")).lower() == "no" else "yes"
        p = price(t, side)
        usd = n * p
        if usd >= KALSHI_MIN_USD:
            try:
                ts = int(time.mktime(time.strptime(t["created_time"][:19], "%Y-%m-%dT%H:%M:%S"))) * 1000
            except Exception:
                ts = 0
            big.append({"tk": tk, "usd": round(usd, 2), "side": side, "p": p, "ts": ts})
    big.sort(key=lambda x: -x["usd"])
    big = big[:25]
    need = [x["tk"] for x in big if x["tk"] not in meta][:25]
    for tk in need:
        try:
            m = get(f"{KALSHI}/markets/{urllib.parse.quote(tk)}").get("market") or {}
            meta[tk] = {"yes_sub_title": m.get("yes_sub_title"), "title": m.get("title"),
                        "mve_selected_legs": m.get("mve_selected_legs") or []}
        except Exception as e:
            print("kalshi market fail", tk, e, file=sys.stderr)
    keep = {x["tk"] for x in big}
    return {"rows": big, "meta": {k: v for k, v in meta.items() if k in keep},
            "seen": len(trades), "combo": combo, "pages": pages, "mkts": len(meta), "err": ERR[0]}


def probe(wallet):
    """Record what each data source returns for one wallet so problems are visible in data/debug.json."""
    tests = []
    key = os.environ.get("ORCA_KEY", "")

    def rec(name, fn):
        try:
            code, body = fn()
            tests.append({"name": name, "status": code, "len": len(body), "snippet": body[:400]})
        except urllib.error.HTTPError as e:
            try:
                b = e.read().decode("utf-8", "replace")[:300]
            except Exception:
                b = ""
            tests.append({"name": name, "status": e.code, "snippet": b})
        except Exception as e:
            tests.append({"name": name, "error": str(e)[:200]})

    def req(url, method="GET", body=None, headers=None):
        data = json.dumps(body).encode() if body is not None else None
        h = {"Accept": "application/json", "User-Agent": "Mozilla/5.0 (whale-scanner-data)"}
        if data:
            h["Content-Type"] = "application/json"
        h.update(headers or {})
        r = urllib.request.urlopen(urllib.request.Request(url, data=data, method=method, headers=h), timeout=30)
        return r.status, r.read().decode("utf-8", "replace")

    rec("polymarket positions", lambda: req(f"{DATA_API}/v2/positions?user={wallet}&limit=2"))
    rec("polymarket closed", lambda: req(f"{DATA_API}/v2/positions?user={wallet}&status=CLOSED&limit=2"))
    rec("orca wallet GET", lambda: req(f"{ORCA}/api/v2/wallet/{wallet}"))
    for field in ("wallets", "addresses", "wallet", "wallet_addresses"):
        body = {field: [wallet] if field != "wallet" else wallet}
        rec("orca positions POST " + field, lambda b=body: req(f"{ORCA}/api/public/v1/wallets/positions", "POST", b))
        if key:
            rec("orca positions POST+key " + field, lambda b=body: req(f"{ORCA}/api/public/v1/wallets/positions", "POST", b, {"X-API-Key": key}))
    return tests


def dump(name, obj):
    os.makedirs(OUT, exist_ok=True)
    with open(os.path.join(OUT, name), "w") as f:
        json.dump(obj, f, separators=(",", ":"))


def main():
    t = int(time.time() * 1000)
    lb = leaderboard()
    print("leaderboard rows:", len(lb))
    if lb:
        dump("leaderboard.json", lb)
        wallets = [wallet_of(r) for r in lb[:TOP]]
        try:
            dump("debug.json", {"t": t, "wallet": wallets[0], "tests": probe(wallets[0])})
        except Exception as e:
            print("probe fail", e, file=sys.stderr)
        pos, closed = wallets_data(wallets)
        print("positions:", len(pos), "closed:", len(closed))
        if lb and lb[0].get("q") is not None:
            recent_form(lb, closed, pos)
            period_filter(lb)
            dump("leaderboard.json", lb)
        dump("positions.json", pos)
        dump("closed.json", closed)
    k = kalshi_combos()
    print("kalshi combo trades:", k["combo"], "big:", len(k["rows"]))
    dump("kalshi.json", k)
    dump("meta.json", {"t": t, "lb": len(lb)})


if __name__ == "__main__":
    main()
