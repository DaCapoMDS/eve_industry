"""Campaign hand-ins: is delivering an item to the campaign Item Trader worth more than selling it?

Per item delivered (1 contribution) you get the objective's ISK + LP, plus 1/per_token of a trader token.
LP is valued from the LP store's best offers at the market station (instant = sell into buy orders,
listed = your own sell order), the token from the market or the best trader reward per token.
Config: campaign.json (numbers read from the objective window). Daily snapshots: campaign_history.json.
"""
import datetime as dt
import json
import math
import os
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.path.join(HERE, "campaign.json")
LP_CACHE = os.path.join(HERE, "campaign_lp_cache.json")
HISTORY = os.path.join(HERE, "campaign_history.json")
MIN_OFFERS_DAY = 5  # ...and the market takes at least 5 of them a day (big-ticket items have stale asks)
MIN_LP_DAY = 20000  # an LP offer only sets the LP value if the market absorbs ~20k LP of it a day (thin implants would inflate it)


def _lp_offers(corps, esi_get):
    """LP store offers per corporation, refreshed once a day."""
    today = str(dt.date.today())
    cache = {}
    if os.path.exists(LP_CACHE):
        with open(LP_CACHE, encoding="utf8") as f:
            cache = json.load(f)
    todo = [c for c in corps if cache.get(str(c), {}).get("fetched") != today]
    if todo:
        def one(c):
            try:
                return c, esi_get(f"/loyalty/stores/{c}/offers/") or []
            except Exception:
                return c, None
        with ThreadPoolExecutor(max_workers=4) as ex:
            for c, offers in ex.map(one, todo):
                if offers is not None:
                    cache[str(c)] = {"fetched": today, "offers": offers}
        with open(LP_CACHE, "w", encoding="utf8") as f:
            json.dump(cache, f)
    return {c: cache.get(str(c), {}).get("offers", []) for c in corps}


def _book(station_orders, t):
    o = station_orders.get(t, [])
    sells = [x["price"] for x in o if not x["is_buy_order"]]
    buys = [x["price"] for x in o if x["is_buy_order"]]
    return (min(sells) if sells else None, max(buys) if buys else None,
            sum(x["volume_remain"] for x in o if not x["is_buy_order"]))


def _days(rows, days, skip=0):
    """history rows of the last `days` calendar days (ESI only has rows for days with trades)"""
    import datetime as _dt
    today = _dt.datetime.now(_dt.timezone.utc).date()
    lo, hi = str(today - _dt.timedelta(days=days + skip)), str(today - _dt.timedelta(days=skip))
    return sorted((r for r in rows if lo <= r["date"] < hi), key=lambda r: r["date"])


def _vol(rows, days=7):
    return sum(r["volume"] for r in _days(rows, days)) / days


def build(station_orders, histories, region, esi_get, names, corps, cfg, bp_mfg=None, adj=None, idx=None):
    if not os.path.exists(CONFIG):
        return None
    with open(CONFIG, encoding="utf8") as f:
        C = json.load(f)
    tax, broker = cfg["sales_tax"], cfg["broker_fee"]
    inst_f, list_f = 1 - tax, 1 - tax - broker
    names = dict(names)

    # ---- LP stores: every offer with today's prices, stock and sales (LP Store tab), and the LP value per corporation
    lp_corps = sorted({o["lp_corp"] for o in C["objectives"] if o.get("lp_corp")})
    store_ids = sorted(set(lp_corps) | set(C.get("lp_stores", [])))
    offers = _lp_offers(store_ids, esi_get)
    store_rows = {}
    for corp, offs in offers.items():
        rows = []
        for o in offs:
            if o.get("lp_cost", 0) <= 0 or not o.get("quantity"):
                continue
            req, reqs, ok = 0.0, [], True
            for r in o.get("required_items", []):
                s, _, _ = _book(station_orders, r["type_id"])
                reqs.append([r["type_id"], r["quantity"], s])
                if not s:
                    ok = False
                else:
                    req += s * r["quantity"]
            cost, q, lp = o.get("isk_cost", 0) + req, o["quantity"], o["lp_cost"]
            tid, build = o["type_id"], None
            bp = (bp_mfg or {}).get(tid)
            if bp:  # a blueprint copy (ME0, quantity = runs): value it as the product you build from it
                me_b = cfg.get("structure_me_bonus", 0.0)
                mats, eiv = 0.0, 0.0
                for mt, mq in bp["m"]:
                    ms, _, _ = _book(station_orders, mt)
                    n_m = max(q, math.ceil(round(q * mq * (1 - me_b), 2)))
                    if not ms:
                        ok = False
                    else:
                        mats += ms * n_m
                    eiv += (adj or {}).get(mt, 0) * mq * q
                job = eiv * ((idx or {}).get("manufacturing", 0) * (1 - cfg.get("structure_job_cost_bonus", 0)) + cfg.get("facility_tax", 0) + cfg.get("scc_surcharge", 0))
                build = {"bp": tid, "runs": q, "mats": round(mats), "job": round(job), "nmats": len(bp["m"])}
                cost += mats + job
                tid, q = bp["p"], q * bp["q"]
            s, b, listed = _book(station_orders, tid)
            rows.append({"tid": tid, "q": q, "lp": lp, "cost": cost, "isk": o.get("isk_cost", 0), "req": round(req), "build": build,
                         "nreq": len(reqs), "reqs": reqs, "ok": ok, "sell": s, "buy": b, "listed": listed,
                         "inst": (b * q * inst_f - cost) / lp if b and ok else None,
                         "list": (s * q * list_f - cost) / lp if s and ok else None})
        store_rows[corp] = rows

    # ---- market history for everything we show
    watch = [o["type_id"] for o in C["objectives"]] + [o["type_id"] for o in C.get("also_watch", [])] + [C["token"]["type_id"]]
    reward_ids = [r["type_id"] for r in C.get("rewards", [])]
    offer_ids = {r["tid"] for rs in store_rows.values() for r in rs}
    hist = histories(region, set(watch) | set(reward_ids) | offer_ids)

    req_ids = {x[0] for rs in store_rows.values() for r in rs for x in r["reqs"]} | {r["build"]["bp"] for rs in store_rows.values() for r in rs if r["build"]}
    missing = [t for t in set(watch) | set(reward_ids) | offer_ids | req_ids if t not in names]
    if missing:
        try:
            for i in range(0, len(missing), 900):
                for n in esi_get_names(esi_get, missing[i:i + 900]):
                    names[n["id"]] = n["name"]
        except Exception:
            pass

    lp, lpstore = {}, {}
    for corp, rows in store_rows.items():
        for r in rows:
            h = sorted(hist.get(r["tid"], []), key=lambda x: x["date"])
            h7, h30 = _days(h, 7), _days(h, 23, skip=7)
            wavg = lambda hh: sum(x["average"] * x["volume"] for x in hh) / max(sum(x["volume"] for x in hh), 1) if hh else None
            avg, avg_old = wavg(h7), wavg(h30)
            r["avg"] = avg
            r["trend"] = avg / avg_old - 1 if avg and avg_old else None
            r["vol"] = _vol(h)
            r["per_day"] = r["vol"] / r["q"]
            r["lp_day"] = r["per_day"] * r["lp"]
            r["stock_days"] = r["listed"] / r["vol"] if r["vol"] else None
            if r["sell"] and avg and r["ok"]:  # list at the lower of today's ask and the week's average: no stale asks
                r["list"] = (min(r["sell"], avg) * r["q"] * list_f - r["cost"]) / r["lp"]
            r["name"] = names.get(r["tid"], str(r["tid"]))
            if r["build"]:
                r["build"]["bp_name"] = names.get(r["build"]["bp"], str(r["build"]["bp"]))
            r["reqs"] = [[names.get(t, str(t)), qn, pr] for t, qn, pr in r["reqs"]]
        deep = [r for r in rows if r["lp_day"] >= MIN_LP_DAY and r["per_day"] >= MIN_OFFERS_DAY]
        best_i = max((r["inst"] for r in deep if r["inst"] is not None), default=None)
        best_l = max((r["list"] for r in deep if r["list"] is not None), default=None)
        show = sorted(deep, key=lambda r: -(r["list"] or -1e9))[:10]  # best ISK/LP via sell order first
        summary = {"name": corps.get(corp, str(corp)), "inst": best_i, "list": best_l, "offers": show, "n_offers": len(rows)}
        if corp in lp_corps:
            lp[str(corp)] = summary
        lpstore[str(corp)] = {"name": summary["name"], "inst": best_i, "list": best_l, "rows": rows}
    lp_val = lambda corp, k: (lp.get(str(corp)) or {}).get(k) or cfg.get("lp_isk_value", 700)

    # ---- token: sell it, or turn it into the best trader reward
    tk = C["token"]["type_id"]
    ts, tb, _ = _book(station_orders, tk)
    rewards = []
    for r in C.get("rewards", []):
        s, b, n = _book(station_orders, r["type_id"])
        v = _vol(hist.get(r["type_id"], []))
        h7 = _days(hist.get(r["type_id"], []), 7)
        avg = sum(x["average"] * x["volume"] for x in h7) / max(sum(x["volume"] for x in h7), 1) if h7 else None
        s = min(s, avg) if s and avg else (s if v >= 1 else None)  # no stale asks for things that barely trade
        rewards.append({"name": r["name"], "tid": r["type_id"], "tokens": r["tokens"], "sell": s, "buy": b, "listed": n, "vol": v,
                        "inst": b * inst_f / r["tokens"] if b else None, "list": s * list_f / r["tokens"] if s else None})
    tok_inst = max([tb * inst_f if tb else 0] + [r["inst"] or 0 for r in rewards if r["vol"] >= 1])
    tok_list = max([ts * list_f if ts else 0] + [r["list"] or 0 for r in rewards if r["vol"] >= 1])
    token = {"name": C["token"]["name"], "tid": tk, "sell": ts, "buy": tb, "vol": _vol(hist.get(tk, [])),
             "inst": tok_inst or None, "list": tok_list or None}

    def item_row(o, objective=True):
        s, b, n = _book(station_orders, o["type_id"])
        h = sorted(hist.get(o["type_id"], []), key=lambda r: r["date"])[-21:]
        row = {"name": o["name"], "tid": o["type_id"], "sell": s, "buy": b, "listed": n, "vol": _vol(h),
               "hist": [[r["date"], round(r["average"]), r["volume"]] for r in h], "note": o.get("note") or o.get("how")}
        if objective:
            pt = o.get("per_token")
            tt = C.get("turnin_tax", 0.0)  # tax taken from the objective's ISK payout
            lt = C.get("turnin_tax_lp", 0.0)  # ...and from its LP (a number; true = same as ISK)
            lt = tt if lt is True else (lt or 0.0)
            isk_g, lp_g = o.get("isk") or 0, o.get("lp") or 0
            row.update({"isk_gross": isk_g, "lp_gross": lp_g, "isk": isk_g * (1 - tt), "lp": lp_g * (1 - lt), "unknown": o.get("isk") is None or o.get("lp") is None, "corp": lp.get(str(o["lp_corp"]), {}).get("name"), "corp_id": str(o["lp_corp"]), "per_token": pt})
            for k, tv in (("inst", token["inst"]), ("list", token["list"])):
                # LP at a safe flat value (campaign.json lp_value), or the market's when that is lower
                rate = min(C["lp_value"], lp_val(o["lp_corp"], k)) if C.get("lp_value") else lp_val(o["lp_corp"], k)
                row["lp_rate_" + k] = rate
                row["lp_" + k] = row["lp"] * rate
                row["tok_" + k] = tv / pt if pt and tv else 0
                row["value_" + k] = row["isk"] + row["lp_" + k] + row["tok_" + k]
        return row

    out = {"campaign": C.get("campaign"), "ends": C.get("ends"), "trader": C.get("trader"),
           "tax": tax, "broker": broker, "turnin_tax": C.get("turnin_tax", 0.0), "turnin_tax_lp": C.get("turnin_tax", 0.0) if C.get("turnin_tax_lp") is True else (C.get("turnin_tax_lp") or 0.0), "lp_value": C.get("lp_value"), "min_lp_day": MIN_LP_DAY, "min_offers_day": MIN_OFFERS_DAY, "lp": lp, "token": token, "rewards": rewards,
           "objectives": [item_row(o) for o in C["objectives"]], "lpstore": lpstore,
           "watch": [item_row(o, False) for o in C.get("also_watch", [])]}

    # ---- one snapshot per day, to see how hand-in value and prices move
    snaps = []
    if os.path.exists(HISTORY):
        with open(HISTORY, encoding="utf8") as f:
            snaps = json.load(f)
    today = str(dt.date.today())
    snap = {"date": today, "token": [ts, tb], "lp": {k: [v["inst"], v["list"]] for k, v in lp.items()},
            "items": {o["name"]: [o["sell"], o["buy"], round(o["value_inst"]), round(o["value_list"])] for o in out["objectives"]}}
    snaps = [s for s in snaps if s["date"] != today] + [snap]
    with open(HISTORY, "w", encoding="utf8") as f:
        json.dump(snaps[-120:], f)
    out["snaps"] = snaps[-120:]
    return out


def esi_get_names(esi_get, ids):
    import requests
    r = requests.post("https://esi.evetech.net/latest/universe/names/", json=list(ids), timeout=30,
                      headers={"User-Agent": "eve-invention-roi/1.0 (personal industry tool)"})
    r.raise_for_status()
    return r.json()
