"""
Tracking of the character's real industry and market activity.

Everything ESI only returns for a limited time (transactions ~30 days, journal
~30 days, jobs 90 days) is merged into local json files, so history keeps
growing as long as the tool runs every few weeks:
    transactions.json, journal.json, jobs.json

Cost accounting:
  - a material's cost = weighted average of what you actually paid for it
    (all stored purchases); if you never bought it (mined, looted, ...) the
    current market price is used instead and flagged as "market"
  - a manufacturing job's cost = materials the job consumed (blueprint recipe
    x runs at the blueprint's ME) x material cost + the real install fee
    + for T2: your real invention cost per successful BPC (datacores +
    invention fees over *your* successes), or the report estimate if you
    have no invention history for it yet
"""
import datetime as dt
import json
import math
import os
import time
from concurrent.futures import ThreadPoolExecutor

import requests

import esi_auth

HERE = os.path.dirname(os.path.abspath(__file__))
ESI = "https://esi.evetech.net/latest"
HEADERS = {"User-Agent": "eve-invention-roi/1.0 (personal industry tool)"}
STRUCTURE_ID_MIN = 1_000_000_000_000  # player structures have huge ids
ACTIVITIES = {1: "Manufacturing", 3: "TE research", 4: "ME research", 5: "Copying",
              8: "Invention", 9: "Reaction", 11: "Reaction"}
NOW = dt.datetime.now(dt.timezone.utc)


def _job_hours(j):
    """How long the job really ran (ESI duration, start to end)."""
    if j.get("duration"):
        return j["duration"] / 3600
    return (_ts(j["end_date"]) - _ts(j["start_date"])).total_seconds() / 3600


def _ts(s):
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))


def _age_days(s):
    return (NOW - _ts(s)).total_seconds() / 86400


def _store(name, new, key):
    """Merge freshly fetched rows into a local json file and return all rows."""
    path = os.path.join(HERE, name)
    rows = {}
    if os.path.exists(path):
        with open(path, encoding="utf8") as f:
            rows = {r[key]: r for r in json.load(f)}
    for r in new or []:
        rows[r[key]] = r  # newer data wins (e.g. a job going active -> delivered)
    out = list(rows.values())
    with open(path, "w", encoding="utf8") as f:
        json.dump(out, f)
    return out


def _resolve_names(ids, known, client_id, can_read_structures):
    names = {i: known[i] for i in ids if i in known}
    todo = [i for i in ids if i not in names and i < STRUCTURE_ID_MIN]
    for chunk in (todo[i:i + 900] for i in range(0, len(todo), 900)):
        r = requests.post(f"{ESI}/universe/names/", json=chunk, headers=HEADERS, timeout=30)
        if r.ok:
            names.update({n["id"]: n["name"] for n in r.json()})
    for sid in (i for i in ids if i >= STRUCTURE_ID_MIN and i not in names):
        names[sid] = (esi_auth.structure_name(client_id, sid) if can_read_structures else None) or f"Structure {sid}"
    return names


def _tick(price):
    """Smallest price step EVE allows at this price (4 significant digits)."""
    return max(0.01, 10 ** (len(str(int(price))) - 4)) if price > 0 else 0.01


def _mat_qty(base, runs, me, struct_bonus):
    return max(runs, math.ceil(round(runs * base * (1 - me / 100) * (1 - struct_bonus), 2)))


# ---------------------------------------------------------------------------- orders

def _get(url, **params):
    """requests.get with a back-off when ESI says we're going too fast."""
    for attempt in range(6):
        r = requests.get(url, params=params, headers=HEADERS, timeout=30)
        if r.status_code not in (420, 429, 502, 503, 504):
            return r
        time.sleep(min(float(r.headers.get("Retry-After") or 2 ** attempt), 60))
    return r


def _typical_volume(history, days=30):
    """Units per day over the last `days`, without the single biggest day (same as the report)."""
    cutoff = (dt.date.today() - dt.timedelta(days=days)).isoformat()
    vols = [h["volume"] for h in history if h["date"] >= cutoff]
    return (sum(vols) - max(vols, default=0)) / (days - 1) if vols else 0.0


def _competition(orders):
    """Per order: best competing price, number of competitors, units queued ahead of you
    at the same station, and the region's typical daily volume."""
    own = {o["order_id"] for o in orders}
    keys = {(o["region_id"], o["type_id"]) for o in orders}

    def fetch(key):
        r = _get(f"{ESI}/markets/{key[0]}/orders/", type_id=key[1], order_type="all")
        hst = _get(f"{ESI}/markets/{key[0]}/history/", type_id=key[1])
        return key, (r.json() if r.ok else [], hst.json() if hst.ok else [])

    with ThreadPoolExecutor(max_workers=6) as ex:
        market = dict(ex.map(fetch, keys))
    out = {}
    for o in orders:
        buy = bool(o.get("is_buy_order"))
        book, history = market[(o["region_id"], o["type_id"])]
        others = [m for m in book if m["location_id"] == o["location_id"] and m["order_id"] not in own
                  and m["is_buy_order"] == buy]
        prices = [m["price"] for m in others]
        # orders that fill before yours: better price, or same price listed earlier
        ahead = sum(m["volume_remain"] for m in others
                    if (m["price"] > o["price"] if buy else m["price"] < o["price"])
                    or (m["price"] == o["price"] and m["issued"] < o["issued"]))
        out[o["order_id"]] = {"best": (max(prices) if buy else min(prices)) if prices else None,
                              "n": len(others), "ahead": ahead, "vol": _typical_volume(history)}
    return out


def _orders(orders, names, fee, unit_cost, cfg):
    comp = _competition(orders)
    # relisting (modifying) an order costs the broker fee on the order's new value,
    # discounted 50% + 6% per Advanced Broker Relations level
    abr = cfg["skills"].get("overrides", {}).get("Advanced Broker Relations", 0) or 0
    relist_rate = cfg["broker_fee"] * (1 - min(1.0, 0.5 + 0.06 * abr))
    patience = cfg.get("relist_patience_days", 2)
    out = []
    for o in orders:
        buy = bool(o.get("is_buy_order"))
        c = comp[o["order_id"]]
        best, n = c["best"], c["n"]
        if best is None:
            status, gap = "alone", 0.0
        elif (best > o["price"]) if buy else (best < o["price"]):
            status, gap = ("outbid" if buy else "undercut"), abs(best - o["price"])
        elif best == o["price"]:
            status, gap = "tied", 0.0
        else:
            status, gap = "best", abs(best - o["price"])
        fix = (best + _tick(best) if buy else best - _tick(best)) if status in ("outbid", "undercut", "tied") else None
        expires = _ts(o["issued"]) + dt.timedelta(days=o["duration"])
        cpu = unit_cost.get(o["type_id"])
        out.append({
            "id": o["order_id"], "tid": o["type_id"], "name": names.get(o["type_id"], str(o["type_id"])),
            "buy": buy, "price": o["price"], "remain": o["volume_remain"], "total": o["volume_total"],
            "loc": names.get(o["location_id"], str(o["location_id"])), "issued": o["issued"][:10],
            "expires_days": (expires - NOW).total_seconds() / 86400,
            "value": o["price"] * o["volume_remain"], "escrow": o.get("escrow"),
            "status": status, "gap": gap, "best": best, "competitors": n, "fix": fix,
            "cpu": cpu, "margin": (o["price"] * (1 - fee) - cpu) / cpu if cpu and not buy else None,
            **_undercut_advice(o, c, fix, status, relist_rate, patience, buy),
        })
    return out


def _undercut_advice(o, c, fix, status, relist_rate, patience, buy):
    """Is undercutting worth it, or will daily volume eat the order anyway?"""
    vol, remain = c["vol"], o["volume_remain"]
    fill_days = (c["ahead"] + remain) / vol if vol else None        # as it is now
    fill_days_top = remain / vol if vol else None                    # if you were first in line
    relist_fee = max(100.0, relist_rate * fix * remain) if fix else None
    price_drop = abs(o["price"] - fix) * remain if fix else None
    advice = None
    if status in ("undercut", "outbid", "tied"):
        if fill_days is not None and fill_days <= patience:
            advice = "wait"       # the market eats everything ahead of you + your order soon anyway
        elif not vol:
            advice = "no volume"  # nothing trades - undercutting won't make it sell
        else:
            advice = "undercut"
    return {"ahead": c["ahead"], "vol": vol, "fill_days": fill_days, "fill_days_top": fill_days_top,
            "relist_fee": relist_fee, "price_drop": price_drop, "advice": advice}


def _self_built(tid, produced, ctx):
    """For inputs you make yourself: units coming out of running jobs and what one unit costs you."""
    p = produced.get(tid)
    if not p:
        return {"building": 0, "you_build": False}
    cpu = p["cost"] / p["units"] if p["units"] else ctx["est_cpu"].get(tid)
    return {"building": p["active_units"], "you_build": True, "build_cpu": cpu, "build_cpu_real": bool(p["units"])}


def _planets(char, journal, ctx, nm):
    """Your PI colonies: what each one extracts, makes, imports and exports per day, and the
    customs tax you really paid there (wallet journal, per planet)."""
    colonies = char.get("planets")
    if colonies is None:
        return None
    sch = {x["id"]: x for x in ctx.get("pi", [])}
    tier = {}
    for x in ctx.get("pi", []):
        tier[x["out"]] = x["tier"]
        for t, _, tr in x["in"]:
            tier[t] = tr
    taxes = {}
    for e in journal:
        if e.get("context_id_type") == "planet_id" and e["ref_type"] in ("planetary_import_tax", "planetary_export_tax", "planetary_construction"):
            t = taxes.setdefault(e["context_id"], {"import": 0.0, "export": 0.0, "construction": 0.0, "export30": 0.0, "import30": 0.0, "last": ""})
            kind = {"planetary_import_tax": "import", "planetary_export_tax": "export", "planetary_construction": "construction"}[e["ref_type"]]
            t[kind] -= e["amount"]
            if kind != "construction" and _age_days(e["date"]) <= 30:
                t[kind + "30"] -= e["amount"]
            t["last"] = max(t["last"], e["date"])
    out = []
    for c in colonies:
        pins = c.get("pins", [])
        prod, extractors, factories, storage = {}, [], {}, {}
        for pin in pins:
            kind = nm(pin["type_id"])
            ex = pin.get("extractor_details")
            if ex and ex.get("product_type_id"):
                exp = pin.get("expiry_time")
                hours_left = (_ts(exp) - NOW).total_seconds() / 3600 if exp else None
                per_day = (ex.get("qty_per_cycle") or 0) * 86400 / ex["cycle_time"] if ex.get("cycle_time") else 0
                running = hours_left is not None and hours_left > 0
                if running:
                    prod[ex["product_type_id"]] = prod.get(ex["product_type_id"], 0) + per_day
                extractors.append({"tid": ex["product_type_id"], "name": nm(ex["product_type_id"]), "per_day": per_day,
                                   "heads": len(ex.get("heads") or []), "hours_left": hours_left})
            sid = pin.get("schematic_id") or (pin.get("factory_details") or {}).get("schematic_id")
            if sid and sid in sch:
                factories[sid] = factories.get(sid, 0) + 1
            if "Launchpad" in kind or "Storage" in kind or "Command Center" in kind:
                for it in pin.get("contents") or []:
                    storage[it["type_id"]] = storage.get(it["type_id"], 0) + it["amount"]
        # run the factories, lowest tier first: inputs made on the planet limit them, the rest is imported
        avail, imports, made = dict(prod), {}, []
        for sid in sorted(factories, key=lambda i: sch[i]["tier"]):
            x, n = sch[sid], factories[sid]
            per = 86400 / x["cycle"] * n
            scale = 1.0
            for t, q, _ in x["in"]:
                if t in avail:
                    scale = min(scale, avail[t] / (q * per)) if q * per else scale
            for t, q, _ in x["in"]:
                need = q * per * scale
                if t in avail:
                    avail[t] -= need
                else:
                    imports[t] = imports.get(t, 0) + q * per  # bought in, factory kept supplied
            outq = x["q"] * per * (scale if any(t in prod or t in avail for t, _, _ in x["in"]) else 1.0)
            avail[x["out"]] = avail.get(x["out"], 0) + outq
            made.append({"tid": x["out"], "name": nm(x["out"]), "count": n, "per_day": outq, "cap_per_day": x["q"] * per,
                         "tier": x["tier"]})
        # P1+ left over gets exported; raw P0 the factories can't use just piles up (not exported)
        exports = [{"tid": t, "name": nm(t), "per_day": q, "tier": tier.get(t, 0)} for t, q in avail.items() if q > 0.5 and tier.get(t, 0) >= 1]
        unused = [{"tid": t, "name": nm(t), "per_day": q} for t, q in avail.items() if q > 0.5 and tier.get(t, 0) == 0]
        t = taxes.get(c["planet_id"], {})
        out.append({
            "id": c["planet_id"], "name": c.get("name"), "type": c.get("planet_type"), "system": nm(c.get("solar_system_id")),
            "level": c.get("upgrade_level"), "pins": len(pins), "last_update": c.get("last_update"),
            "extractors": extractors, "factories": made,
            "exports": sorted(exports, key=lambda e: -e["per_day"]),
            "unused": unused,
            "imports": [{"tid": k, "name": nm(k), "per_day": v, "tier": tier.get(k, 0)} for k, v in imports.items()],
            "storage": [{"tid": k, "name": nm(k), "qty": v, "tier": tier.get(k, 0)} for k, v in storage.items()],
            "tax": t,
        })
    return out


def _build_locations(jobs, nm):
    """Where you run your jobs, most used first (to preselect 'Stock at')."""
    count = {}
    for j in jobs:
        loc = j.get("facility_id") or j.get("station_id")
        if loc:
            count[nm(loc)] = count.get(nm(loc), 0) + 1
    return [n for n, _ in sorted(count.items(), key=lambda x: -x[1])]


def _real_pnl(char, journal, txs, produced, real_cpu, paid, nm):
    """Profit from your own records only: sales, the sales tax and broker fees you actually
    paid (matched from the wallet journal), and what the sold items really cost you."""
    # every state of every order we've seen (placed / modified), so broker fees can be matched
    events = []
    for o in (char.get("orders") or []) + (char.get("orders_history") or []):
        events.append({"key": f'{o["order_id"]}|{o["issued"]}', "order_id": o["order_id"], "issued": o["issued"],
                       "type_id": o["type_id"], "buy": bool(o.get("is_buy_order"))})
    events = _store("order_events.json", events, "key")
    by_time = {}
    for e in events:
        by_time.setdefault(e["issued"], []).append(e)

    listed, instant = _listed_spans(char, events, txs)

    # sales tax: booked the same second as its sale, as the neighbouring journal entry
    sale_entry = {e["context_id"]: e for e in journal if e["ref_type"] == "market_transaction"}
    taxes = {}
    for e in journal:
        if e["ref_type"] == "transaction_tax":
            taxes.setdefault(e["date"], []).append(e)
    sales, tax_by_tx = [], {}
    for t in txs:
        if t["is_buy"]:
            continue
        m = sale_entry.get(t["transaction_id"])
        tax = None
        if m:
            near = [x for x in taxes.get(m["date"], []) if abs(x["id"] - m["id"]) <= 2]
            if near:
                tax = -min(near, key=lambda x: abs(x["id"] - m["id"]))["amount"]
        sales.append({"age": _age_days(t["date"]), "tid": t["type_id"], "qty": t["quantity"],
                      "rev": t["quantity"] * t["unit_price"], "tax": tax})
        if tax is not None:
            tax_by_tx[t["transaction_id"]] = tax

    # broker fees: booked the second an order is placed or modified
    fees = []
    for e in journal:
        if e["ref_type"] != "brokers_fee":
            continue
        hits = {(x["type_id"], x["buy"]) for x in by_time.get(e["date"], [])}
        tid, buy = next(iter(hits)) if len(hits) == 1 else (None, None)
        fees.append({"age": _age_days(e["date"]), "tid": tid, "buy": buy, "amt": -e["amount"]})

    # what a sold unit really cost you: your own production, or what you paid when trading it
    cost = {}
    for tid in {s["tid"] for s in sales}:
        p = produced.get(tid)
        if tid in real_cpu and p:
            cov = p["paid"] / p["mat"] if p["mat"] else 1.0
            cost[str(tid)] = {"cpu": real_cpu[tid], "src": "built", "coverage": cov, "inv_est": p["inv_est"],
                              "jobs": p["jobs"], "real": cov >= 0.995 and not p["inv_est"],
                              # real slot time per unit from your finished jobs (lab: your invention jobs per success)
                              "mfg_h_u": p["mfg_h"] / p["units"],
                              "lab_h_u": None if p["lab_est"] else p["lab_h"] / p["units"]}
        elif paid.get(tid, {}).get("q"):
            cost[str(tid)] = {"cpu": paid[tid]["isk"] / paid[tid]["q"], "src": "bought", "coverage": 1.0, "real": True}
    return {"fees": fees, "cost": cost, "tax_by_tx": tax_by_tx, "listed": listed, "instant": instant}


def _listed_spans(char, events, txs):
    """When each item really had a sell order up: per type, merged [from, to] in days ago.
    Start = first time we saw the order placed; end = now (open), its last sale (sold out / cancelled)
    or its expiry. Orders are kept locally so the history grows past ESI's 90 days."""
    open_ids = {o["order_id"] for o in char.get("orders") or []}
    rows = []
    for o in (char.get("orders") or []) + (char.get("orders_history") or []):
        if o.get("is_buy_order"):
            continue
        rows.append({"order_id": o["order_id"], "type_id": o["type_id"], "loc": o["location_id"], "issued": o["issued"],
                     "duration": o.get("duration", 90), "remain": o.get("volume_remain", 0),
                     "state": "open" if o["order_id"] in open_ids else o.get("state", "closed")})
    orders = _store("sell_orders.json", rows, "order_id")
    first = {}
    for e in events:
        if not e["buy"]:
            first[e["order_id"]] = min(first.get(e["order_id"], e["issued"]), e["issued"])
    sales = {}
    for t in txs:
        if not t["is_buy"]:
            sales.setdefault((t["type_id"], t["location_id"]), []).append(_ts(t["date"]))
    # a sale within 2 minutes of placing/changing a sell order of that item = dumped into a buy order, not sold off the shelf
    touched = {}
    for e in events:
        if not e["buy"]:
            touched.setdefault(e["type_id"], []).append(_ts(e["issued"]))
    instant = [t["transaction_id"] for t in txs if not t["is_buy"] and any(
        0 <= (_ts(t["date"]) - x).total_seconds() <= 120 for x in touched.get(t["type_id"], []))]
    spans = {}
    for o in orders:
        start = _ts(min(first.get(o["order_id"], o["issued"]), o["issued"]))
        expiry = start + dt.timedelta(days=o["duration"])
        if o["state"] == "open" and o["order_id"] in open_ids:
            end = NOW
        else:
            sold = [d for d in sales.get((o["type_id"], o["loc"]), []) if start <= d <= expiry]
            last_mod = _ts(o["issued"])
            if o["state"] == "expired" and o["remain"]:
                end = min(expiry, NOW)
            else:  # sold out or cancelled: the last sale (or change) we know of
                end = max([last_mod] + sold)
        if end > start:
            spans.setdefault(o["type_id"], []).append((start, min(end, NOW)))
    out = {}
    for tid, xs in spans.items():
        xs.sort()
        merged = [list(xs[0])]
        for a, b in xs[1:]:
            if a <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], b)
            else:
                merged.append([a, b])
        out[str(tid)] = [[round((NOW - a).total_seconds() / 86400, 3), round((NOW - b).total_seconds() / 86400, 3)] for a, b in merged]
    return out, instant


# ---------------------------------------------------------------------------- main

def build(char, cfg, ctx):
    """Everything the tracking tabs need, or None if the character data isn't available.

    ctx: names, bp_mfg, bp_inv (SDE recipes), market (type -> current price),
         est_cpu (product -> report cost/unit), est_inv (T2 bp -> report invention cost per BPC)
    """
    if char is None or char.get("orders") is None:
        return None
    client_id = cfg.get("esi_client_id")
    fee = cfg["sales_tax"] + cfg["broker_fee"]
    bp_mfg, bp_inv, market = ctx["bp_mfg"], ctx["bp_inv"], ctx["market"]

    txs = sorted(_store("transactions.json", char.get("transactions"), "transaction_id"),
                 key=lambda t: t["date"], reverse=True)
    journal = sorted(_store("journal.json", char.get("journal"), "id"), key=lambda j: j["date"], reverse=True)
    jobs = sorted(_store("jobs.json", char.get("jobs"), "job_id"), key=lambda j: j["start_date"], reverse=True) \
        if char.get("jobs") is not None or os.path.exists(os.path.join(HERE, "jobs.json")) else []
    have_industry = char.get("jobs") is not None

    job_fee_real = {}
    for e in journal:
        if e.get("context_id_type") == "industry_job_id" and e.get("amount", 0) < 0:
            job_fee_real[e["context_id"]] = job_fee_real.get(e["context_id"], 0.0) - e["amount"]

    def job_fee(j):
        """What the job really cost to install (from your wallet journal), else ESI's estimate."""
        return job_fee_real.get(j["job_id"], j.get("cost", 0.0))

    # stock on hand: packaged stacks in hangars, cargo and containers. Assembled items
    # (your own ships, fitted modules, drones in a drone bay) aren't stock for sale.
    fitted = ("HiSlot", "MedSlot", "LoSlot", "RigSlot", "SubSystemSlot", "ServiceSlot", "DroneBay",
              "FighterBay", "FighterTube", "Implant", "Skill", "Booster")
    stock, stock_at = {}, {}
    assets = char.get("assets") or []
    by_item = {a["item_id"]: a for a in assets}

    def root_location(a):
        """The station / structure an item is in, through any containers or ships."""
        loc, seen = a["location_id"], set()
        while loc in by_item and loc not in seen:
            seen.add(loc)
            loc = by_item[loc]["location_id"]
        return loc

    for a in assets:
        if a.get("is_blueprint_copy") or a.get("is_singleton") or a.get("location_flag", "").startswith(fitted):
            continue
        stock[a["type_id"]] = stock.get(a["type_id"], 0) + a["quantity"]
        where = stock_at.setdefault(a["type_id"], {})
        loc = root_location(a)
        where[loc] = where.get(loc, 0) + a["quantity"]

    # ---- material cost basis: weighted average of what you paid
    paid = {}
    for t in txs:
        if t["is_buy"]:
            p = paid.setdefault(t["type_id"], {"q": 0, "isk": 0.0, "last": None, "last_date": ""})
            p["q"] += t["quantity"]
            p["isk"] += t["quantity"] * t["unit_price"]
            if t["date"] > p["last_date"]:
                p["last"], p["last_date"] = t["unit_price"], t["date"]

    def unit_cost(tid):
        p = paid.get(tid)
        if p and p["q"]:
            return p["isk"] / p["q"], "paid"
        m = market.get(tid)
        return (m, "market") if m else (0.0, "unknown")

    blueprints = {b["item_id"]: b for b in char.get("blueprints") or []}
    t2_bps = {p for inv in bp_inv.values() for p in inv["p"]}
    struct = cfg.get("structure_me_bonus", 0.0)

    # ---- invention: your real cost per successful BPC, per T2 blueprint
    inv_stats = {}
    consumed = []  # (type_id, qty, start_date) for the material ledger
    for j in jobs:
        if j["activity_id"] != 8 or j["status"] == "cancelled":
            continue
        recipe = bp_inv.get(j["blueprint_type_id"], {"m": []})
        spend = job_fee(j)
        for tid, q in recipe["m"]:
            spend += q * j["runs"] * unit_cost(tid)[0]
            consumed.append((tid, q * j["runs"], j["start_date"]))
        s = inv_stats.setdefault(j["product_type_id"], {"attempts": 0, "success": 0, "spend": 0.0, "pending": 0, "hours": 0.0})
        if j["status"] == "delivered" or j.get("successful_runs") is not None and j["status"] != "active":
            s["attempts"] += j["runs"]
            s["success"] += j.get("successful_runs") or 0
            s["spend"] += spend
            s["hours"] += _job_hours(j)
        else:
            s["pending"] += j["runs"]
    for s in inv_stats.values():
        s["per_bpc"] = s["spend"] / s["success"] if s["success"] else None
        s["lab_h_bpc"] = s["hours"] / s["success"] if s["success"] else None  # lab time per successful BPC
        s["rate"] = s["success"] / s["attempts"] if s["attempts"] else None

    # ---- every job, costed
    out_jobs, produced = [], {}
    for j in jobs:
        act = j["activity_id"]
        product = j.get("product_type_id")
        row = {
            "id": j["job_id"], "act": ACTIVITIES.get(act, f"Activity {act}"), "status": j["status"],
            "bp": j["blueprint_type_id"], "product": product, "runs": j["runs"], "fee": job_fee(j),
            "start": j["start_date"][:16].replace("T", " "), "end": j["end_date"][:16].replace("T", " "),
            "left_h": max(0.0, (_ts(j["end_date"]) - NOW).total_seconds() / 3600),
            "age": _age_days(j["start_date"]), "success": j.get("successful_runs"),
        }
        if act in (1, 9, 11) and j["status"] != "cancelled" and j["blueprint_type_id"] in bp_mfg:
            recipe = bp_mfg[j["blueprint_type_id"]]
            bp = blueprints.get(j["blueprint_id"])
            if recipe.get("r"):
                me = 0  # reactions have no material efficiency
            else:
                me = bp["material_efficiency"] if bp else (2 if j["blueprint_type_id"] in t2_bps else cfg.get("bpo_me", 10))
            mats, mat_cost, paid_cost, sources = [], 0.0, 0.0, set()
            for tid, base in recipe["m"]:
                q = _mat_qty(base, j["runs"], me, struct)
                c, src = unit_cost(tid)
                sources.add(src)
                mats.append({"tid": tid, "qty": q, "unit": c, "src": src})
                mat_cost += q * c
                if src == "paid":
                    paid_cost += q * c
                consumed.append((tid, q, j["start_date"]))
            inv_cost, inv_src, lab_h = 0.0, None, 0.0
            if j["blueprint_type_id"] in t2_bps:
                bpc_runs = ctx["bpc_runs"].get(j["blueprint_type_id"], 10)
                real = inv_stats.get(j["blueprint_type_id"], {}).get("per_bpc")
                per_bpc, inv_src = (real, "yours") if real else (ctx["est_inv"].get(j["blueprint_type_id"]), "estimate")
                inv_cost = (per_bpc or 0.0) * j["runs"] / bpc_runs
                lab_bpc = inv_stats.get(j["blueprint_type_id"], {}).get("lab_h_bpc") if real else None
                lab_h = lab_bpc * j["runs"] / bpc_runs if lab_bpc else None
            units = j["runs"] * recipe["q"]
            total = mat_cost + job_fee(j) + inv_cost
            row.update(units=units, me=me, mats=mats, mat_cost=mat_cost, inv_cost=inv_cost, inv_src=inv_src,
                       total=total, cpu=total / units, value=units * (market.get(product) or 0),
                       basis="paid" if sources == {"paid"} else "mixed" if "paid" in sources else "market")
            p = produced.setdefault(product, {"units": 0, "cost": 0.0, "jobs": 0, "active_units": 0,
                                              "mat": 0.0, "paid": 0.0, "inv_est": False, "mfg_h": 0.0, "lab_h": 0.0, "lab_est": False})
            p["jobs"] += 1
            if j["status"] in ("delivered", "ready"):
                p["units"] += units
                p["cost"] += total
                p["mat"] += mat_cost
                p["paid"] += paid_cost
                p["inv_est"] = p["inv_est"] or inv_src == "estimate"
                p["mfg_h"] += _job_hours(j)
                if lab_h is None:
                    p["lab_est"] = True
                else:
                    p["lab_h"] += lab_h
            else:
                p["active_units"] += units
        elif act == 8:
            s = inv_stats.get(product, {})
            row.update(rate=s.get("rate"), per_bpc=s.get("per_bpc"))
        out_jobs.append(row)

    busy = {j["blueprint_id"]: ACTIVITIES.get(j["activity_id"], "job") for j in jobs
            if j["status"] in ("active", "ready", "paused") and j.get("blueprint_id")}
    bp_list = []
    for b in char.get("blueprints") or []:
        parent = by_item.get(b["location_id"])
        rec = bp_mfg.get(b["type_id"])
        inv = bp_inv.get(b["type_id"])
        bp_list.append({"id": b["item_id"], "tid": b["type_id"], "bpo": b["quantity"] != -2,
                        "runs": b.get("runs", -1), "me": b.get("material_efficiency", 0), "te": b.get("time_efficiency", 0),
                        "loc": root_location(b) if parent else b["location_id"],
                        "cont": parent["type_id"] if parent and parent.get("type_id") else None,
                        "cont_id": parent["item_id"] if parent else None,
                        "cont_label": (char.get("asset_names") or {}).get(parent["item_id"]) if parent else None,
                        "job": busy.get(b["item_id"]),
                        "product": rec["p"] if rec else None, "per_run": rec["q"] if rec else 1,
                        "invents": sorted(inv["p"]) if inv else []})
    ids = {o["type_id"] for o in char["orders"]} | {o["location_id"] for o in char["orders"]} \
        | {t["type_id"] for t in txs} | {t["location_id"] for t in txs} \
        | {j["bp"] for j in out_jobs} | {j["product"] for j in out_jobs if j["product"]} \
        | {tid for tid, _, _ in consumed} | set(stock) | {loc for w in stock_at.values() for loc in w}         | {j[k] for j in jobs for k in ("facility_id", "station_id") if j.get(k)} \
        | {c["solar_system_id"] for c in char.get("planets") or []} \
        | {p["type_id"] for c in char.get("planets") or [] for p in c.get("pins", [])} \
        | {p["extractor_details"]["product_type_id"] for c in char.get("planets") or [] for p in c.get("pins", [])
           if (p.get("extractor_details") or {}).get("product_type_id")} \
        | {it["type_id"] for c in char.get("planets") or [] for p in c.get("pins", []) for it in p.get("contents") or []}
    ids |= {b["tid"] for b in bp_list} | {b["loc"] for b in bp_list} | {b["cont"] for b in bp_list if b["cont"]} \
        | {b["product"] for b in bp_list if b["product"]}
    names = _resolve_names(ids, ctx["names"], client_id, char.get("can_read_structures"))
    nm = lambda i: names.get(i, str(i))
    for b in bp_list:
        b.update(name=nm(b["tid"]), loc_name=nm(b["loc"]), cont_name=nm(b["cont"]) if b["cont"] else None,
                 product_name=nm(b["product"]) if b["product"] else None)
    for j in out_jobs:
        j["bp_name"], j["product_name"] = nm(j["bp"]), nm(j["product"]) if j["product"] else ""
        for m in j.get("mats", []):
            m["name"] = nm(m["tid"])

    # ---- real unit cost per product: your jobs first, report estimate otherwise
    real_cpu = {tid: p["cost"] / p["units"] for tid, p in produced.items() if p["units"]}
    cost_for = {**ctx["est_cpu"], **real_cpu}

    # ---- sales / purchases per item
    sold = {}
    for t in txs:
        s = sold.setdefault(t["type_id"], {"sq": 0, "sisk": 0.0, "bq": 0, "bisk": 0.0, "s30": 0, "s30isk": 0.0})
        if t["is_buy"]:
            s["bq"] += t["quantity"]
            s["bisk"] += t["quantity"] * t["unit_price"]
        else:
            s["sq"] += t["quantity"]
            s["sisk"] += t["quantity"] * t["unit_price"]
            if _age_days(t["date"]) <= 30:
                s["s30"] += t["quantity"]
                s["s30isk"] += t["quantity"] * t["unit_price"]

    listed = {}
    for o in char["orders"]:
        if not o.get("is_buy_order"):
            listed[o["type_id"]] = listed.get(o["type_id"], 0) + o["volume_remain"]

    production = []
    for tid in set(produced) | {t for t, s in sold.items() if s["sq"] and t in ctx["est_cpu"]}:
        p = produced.get(tid, {"units": 0, "cost": 0.0, "jobs": 0, "active_units": 0})
        s = sold.get(tid, {"sq": 0, "sisk": 0.0})
        cpu = real_cpu.get(tid)
        est = ctx["est_cpu"].get(tid)
        use = cpu if cpu is not None else est
        avg_sell = s["sisk"] / s["sq"] if s["sq"] else None
        production.append({
            "tid": tid, "name": nm(tid), "jobs": p["jobs"], "built": p["units"], "building": p["active_units"],
            "cpu": cpu, "est": est, "vs_est": (cpu - est) / est if cpu and est else None,
            "sold": s["sq"], "avg_sell": avg_sell, "revenue": s["sisk"],
            "profit": s["sisk"] * (1 - fee) - s["sq"] * use if use is not None and s["sq"] else None,
            "margin": (avg_sell * (1 - fee) - use) / use if use and avg_sell else None,
            "stock": stock.get(tid, 0), "listed": listed.get(tid, 0), "market": market.get(tid),
            "stock_value": (stock.get(tid, 0) + listed.get(tid, 0)) * (use or 0),
            "cost_source": "your jobs" if cpu is not None else "estimate",
        })
    production.sort(key=lambda r: -(r["revenue"] or 0))

    # ---- what you already have of each product: hangar + your sell orders + running jobs
    holdings = {}
    for tid in set(stock) | set(listed) | {t for t, p in produced.items() if p["active_units"]}:
        h = {"stock": stock.get(tid, 0), "listed": listed.get(tid, 0),
             "building": produced.get(tid, {}).get("active_units", 0)}
        if any(h.values()):
            holdings[str(tid)] = h

    # ---- restocking: how fast your jobs consume each input vs what you have / have on order
    first_job = max((_age_days(j["start_date"]) for j in jobs), default=0)
    window = max(1.0, min(30.0, first_job))  # days of job history we can average over
    on_order = {}
    for o in char["orders"]:
        if o.get("is_buy_order"):
            on_order[o["type_id"]] = on_order.get(o["type_id"], 0) + o["volume_remain"]
    usage = {}
    for tid, q, start in consumed:
        if tid in bp_mfg or tid in bp_inv:  # blueprint copies aren't bought
            continue
        u = usage.setdefault(tid, {"used7": 0, "used30": 0})
        age = _age_days(start)
        if age <= 7:
            u["used7"] += q
        if age <= 30:
            u["used30"] += q
    restock = []
    for tid, u in usage.items():
        if not u["used30"]:
            continue
        restock.append({"tid": tid, "name": nm(tid), "used7": u["used7"], "used30": u["used30"],
                        "per_day": u["used30"] / window, "stock": stock.get(tid, 0), "on_order": on_order.get(tid, 0),
                        "market": market.get(tid), **_self_built(tid, produced, ctx)})

    # ---- T2 blueprints you already have: invented BPCs in your hangars + inventions still running
    bp_hold = {}
    for b in char.get("blueprints") or []:
        if b["quantity"] == -2:
            h = bp_hold.setdefault(str(b["type_id"]), {"bpc": 0, "runs": 0, "inventing": 0})
            h["bpc"] += 1
            h["runs"] += max(b.get("runs", 0), 0)
    for j in jobs:
        if j["activity_id"] == 8 and j["status"] in ("active", "ready", "paused") and j.get("product_type_id"):
            h = bp_hold.setdefault(str(j["product_type_id"]), {"bpc": 0, "runs": 0, "inventing": 0})
            h["inventing"] += j["runs"]

    # ---- material ledger
    used, used30 = {}, {}
    for tid, q, start in consumed:
        used[tid] = used.get(tid, 0) + q
        if _age_days(start) <= 30:
            used30[tid] = used30.get(tid, 0) + q
    # blueprints you bought aren't materials
    not_materials = set(produced) | set(ctx["est_cpu"]) | set(bp_mfg) | set(bp_inv)
    ledger = []
    for tid in (set(paid) | set(used)) - not_materials:
        p = paid.get(tid, {"q": 0, "isk": 0.0, "last": None})
        avg = p["isk"] / p["q"] if p["q"] else None
        cost, src = unit_cost(tid)
        per_day = used30.get(tid, 0) / 30
        ledger.append({
            "tid": tid, "name": nm(tid), "bought": p["q"], "spent": p["isk"], "avg": avg, "last": p["last"],
            "market": market.get(tid), "vs_market": (avg - market[tid]) / market[tid] if avg and market.get(tid) else None,
            "used": used.get(tid, 0), "used30": used30.get(tid, 0), "stock": stock.get(tid, 0),
            "stock_value": stock.get(tid, 0) * cost, "src": src,
            "days_left": stock.get(tid, 0) / per_day if per_day else None,
        })
    ledger.sort(key=lambda r: -(r["spent"] or r["stock_value"] or 0))

    # ---- wallet journal
    out_journal = [{"date": j["date"], "age": _age_days(j["date"]), "type": j["ref_type"], "amount": j.get("amount", 0.0)}
                   for j in journal]

    pnl = _real_pnl(char, journal, txs, produced, real_cpu, paid, nm)
    instant = set(pnl["instant"])

    out_tx = [{"date": t["date"][:16].replace("T", " "), "age": _age_days(t["date"]), "tid": t["type_id"],
               "name": nm(t["type_id"]), "buy": t["is_buy"], "qty": t["quantity"], "price": t["unit_price"],
               "total": t["quantity"] * t["unit_price"], "loc": nm(t["location_id"]),
               "tax": pnl["tax_by_tx"].get(t["transaction_id"]), "inst": t["transaction_id"] in instant} for t in txs]

    return {
        "pnl": {"fees": pnl["fees"], "cost": pnl["cost"], "listed": pnl["listed"]},
        "orders": _orders(char["orders"], names, fee, cost_for, cfg),
        "relist_patience": cfg.get("relist_patience_days", 2),
        "tx": out_tx[:3000], "tx_count": len(txs), "tx_since": txs[-1]["date"][:10] if txs else None,
        "journal": out_journal[:20000], "journal_since": journal[-1]["date"][:10] if journal else None,
        "balance": char.get("balance"), "jobs": out_jobs[:1500], "have_industry": have_industry,
        "production": production, "ledger": ledger, "cost_for": {str(k): v for k, v in cost_for.items()},
        "inv_stats": {str(k): v for k, v in inv_stats.items()}, "fee": fee, "holdings": holdings, "bp_hold": bp_hold,
        "blueprints": bp_list,
        "used30": {str(t): q for t, q in used30.items()},  # everything your jobs used up in 30 days, incl. things you build
        "mat_stock": {str(t): q for t, q in stock.items() if t in ctx.get("mat_ids", ())},
        # stock per location (station / structure name -> qty) for materials and restock inputs
        "stock_at": {str(t): {nm(loc): q for loc, q in where.items()} for t, where in stock_at.items()
                     if t in ctx.get("mat_ids", ()) or t in usage},
        "build_locations": _build_locations(jobs, nm),
        "planets": _planets(char, journal, ctx, nm),
        # inputs coming out of your running jobs (for the planned shopping list)
        "mat_building": {str(t): p["active_units"] for t, p in produced.items() if p["active_units"] and t in ctx.get("mat_ids", ())},
        "restock": restock, "restock_window": window,
    }
