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
TOP = int(os.environ.get("TOP_N", "100"))
KALSHI_MIN_USD = float(os.environ.get("KALSHI_MIN_USD", "499"))


UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/124.0 Safari/537.36")


def get(url, headers=None, tries=3):
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers=dict({"Accept": "application/json",
                                                           "User-Agent": UA, "Origin": "https://polymarket.com", "Referer": "https://polymarket.com/",
                                                           "Accept-Language": "en-US,en;q=0.9"}, **(headers or {})))
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


def leaderboard():
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
            "cashPnl", "percentPnl", "endDate", "redeemable", "slug")
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
    j = get(f"{DATA_API}/v2/positions?user={w}&status={status}&limit={limit}")
    return rows_of(j)


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
            "timestamp": g("last_event_at", "first_entry_at")}


def wallets_data(wallets):
    pos, closed = {}, {}
    for i, w in enumerate(wallets):
        try:
            pos[w] = slim([from_v2(r) for r in v2_rows(w, "OPEN", 60)], POS_KEYS)
        except Exception as e:
            print("positions fail", w, e, file=sys.stderr)
            try:
                pos[w] = orca_positions(w)
            except Exception as e2:
                print("orca positions fail", w, e2, file=sys.stderr)
        try:
            closed[w] = slim([from_v2(r) for r in v2_rows(w, "CLOSED", 50)], CLOSED_KEYS)
        except Exception as e:
            print("closed fail", w, e, file=sys.stderr)
        time.sleep(0.25)
    return pos, closed


def kalshi_combos():
    meta, trades = {}, []
    try:
        j = get(KALSHI + "/markets?mve_filter=only&status=open&limit=1000")
        for m in j.get("markets", []):
            meta[m["ticker"]] = {"yes_sub_title": m.get("yes_sub_title"), "title": m.get("title"),
                                 "mve_selected_legs": m.get("mve_selected_legs") or []}
    except Exception as e:
        print("kalshi markets fail", e, file=sys.stderr)
    cursor, pages = "", 0
    since = int(time.time()) - 86400
    while pages < 8:
        q = f"{KALSHI}/markets/trades?limit=1000&min_ts={since}" + (f"&cursor={urllib.parse.quote(cursor)}" if cursor else "")
        try:
            j = get(q)
        except Exception as e:
            print("kalshi trades fail", e, file=sys.stderr)
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
            "seen": len(trades), "combo": combo}


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
        dump("positions.json", pos)
        dump("closed.json", closed)
    k = kalshi_combos()
    print("kalshi combo trades:", k["combo"], "big:", len(k["rows"]))
    dump("kalshi.json", k)
    dump("meta.json", {"t": t, "lb": len(lb)})


if __name__ == "__main__":
    main()
