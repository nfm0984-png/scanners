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


def get(url, headers=None, tries=3):
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers=dict({"Accept": "application/json",
                                                           "User-Agent": "whale-scanner-data/1.0"}, **(headers or {})))
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


def wallets_data(wallets):
    pos, closed = {}, {}
    for i, w in enumerate(wallets):
        try:
            pos[w] = slim(rows_of(get(f"{DATA_API}/positions?user={w}&sizeThreshold=1&limit=60"
                                      "&sortBy=CURRENT&sortDirection=DESC")), POS_KEYS)
        except Exception as e:
            print("positions fail", w, e, file=sys.stderr)
        try:
            closed[w] = slim(rows_of(get(f"{DATA_API}/closed-positions?user={w}&limit=50")), CLOSED_KEYS)
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

    rec("polymarket positions", lambda: req(f"{DATA_API}/positions?user={wallet}&limit=2"))
    rec("polymarket closed", lambda: req(f"{DATA_API}/closed-positions?user={wallet}&limit=2"))
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
