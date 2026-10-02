"""
EVE Online T2 invention ROI calculator.

Pulls blueprint data from CCP's static data export (SDE) and live market data
from ESI (default: Amarr hub), then calculates the full cost of inventing and
manufacturing each T2 item and compares it to the market price and volume.

Usage:
    python eve_invention.py              # run with config.json
    python eve_invention.py --update-sde # re-download the SDE first (after a patch)
"""
import argparse
import csv
import datetime as dt
import html
import io
import json
import math
import os
import sys
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor

import requests

import esi_auth
import tracking

HERE = os.path.dirname(os.path.abspath(__file__))
SDE_DIR = os.path.join(HERE, "sde")
SDE_URL = "https://developers.eveonline.com/static-data/eve-online-static-data-latest-jsonl.zip"
SDE_CACHE = os.path.join(HERE, "sde_cache.json")
SDE_CACHE_VERSION = 14  # bump when build_sde_cache() extracts new things
OUT_DIR = os.path.join(HERE, "output")
ESI = "https://esi.evetech.net/latest"
HEADERS = {"User-Agent": "eve-invention-roi/1.0 (personal industry tool)"}

CHARGE_CATEGORY = 8
MODULE_CATEGORY = 7
SHIP_CATEGORY = 6
DRONE_CATEGORIES = {18, 87}  # Drone, Fighter
SKILL_CATEGORY = 16
TECH2_META_GROUP = 2
DECRYPTOR_GROUP = 1304
LASER_CRYSTAL_GROUPS = {"Frequency Crystal", "Advanced Beam Laser Crystal", "Advanced Pulse Laser Crystal"}
COMPONENT_GROUPS = {"Capital Construction Components", "Advanced Capital Construction Components",
                    "Construction Components", "Hybrid Tech Components", "Structure Components"}
MINING_GROUPS = {"Compressors", "Mining Laser", "Frequency Mining Laser", "Strip Miner", "Mining Upgrade",
                 "Gas Cloud Scoops", "Gas Cloud Harvesters", "Mining Survey Chipset", "Mining Foreman Burst Charges",
                 "Survey Probe", "Survey Probe Launcher"}
MINERALS = {34, 35, 36, 37, 38, 39, 40, 11399}  # Tritanium .. Megacyte, Morphite
TRITANIUM = 34

# tab key -> title. Every item (T1 from a BPO, T2 from invention) goes to its topic tab;
# T1 items that fit no topic but need only minerals go to "mineral_builds".
CATEGORIES = {
    "missiles": "Missiles, Rockets & Torpedoes",
    "laser_crystals": "Laser Frequency Crystals",
    "mining_crystals": "Mining Crystals",
    "components": "Components",
    "mining": "Mining & Industry",
    "modules": "Modules",
    "rigs": "Rigs",
    "fuel": "Fuel Blocks",
    "drones": "Drones & Fighters",
    "ships": "Ships",
    "mineral_builds": "Mineral Builds",
    "other_ammo": "Ammo & Charges",
}


def topic(name, group, category_id, category):
    """Which tab an item belongs to by what it is, or None."""
    if category_id == CHARGE_CATEGORY and any(w in group for w in ("Missile", "Rocket", "Torpedo"))             and "Structure" not in group and "Script" not in group:
        return "missiles"
    if group in LASER_CRYSTAL_GROUPS:
        return "laser_crystals"
    if "Mining Crystal" in group:
        return "mining_crystals"
    if category == "Commodity" and group in COMPONENT_GROUPS:
        return "components"
    if group in MINING_GROUPS or "Industrial Core" in name:
        return "mining"
    if group == "Fuel Block":
        return "fuel"
    if category_id == MODULE_CATEGORY and group.startswith("Rig "):
        return "rigs"
    if category_id == MODULE_CATEGORY:
        return "modules"
    if category_id in DRONE_CATEGORIES:
        return "drones"
    if category_id == SHIP_CATEGORY:
        return "ships"
    if category_id == CHARGE_CATEGORY:
        return "other_ammo"
    return None


# decryptor dogma attributes
ATTR_PROB, ATTR_ME, ATTR_TE, ATTR_RUNS = 1112, 1113, 1114, 1124

session = requests.Session()
session.headers.update(HEADERS)


# --------------------------------------------------------------------------- SDE

def en(v):
    return v.get("en") if isinstance(v, dict) else v


def read_jsonl(name):
    with open(os.path.join(SDE_DIR, name), encoding="utf8") as f:
        for line in f:
            yield json.loads(line)


def download_sde():
    print("Downloading SDE (~100 MB)...")
    r = session.get(SDE_URL, timeout=600)
    r.raise_for_status()
    os.makedirs(SDE_DIR, exist_ok=True)
    wanted = {"types.jsonl", "groups.jsonl", "categories.jsonl", "blueprints.jsonl", "typeDogma.jsonl",
              "mapSolarSystems.jsonl", "_sde.jsonl"}
    with zipfile.ZipFile(io.BytesIO(r.content)) as z:
        for n in z.namelist():
            if os.path.basename(n) in wanted:
                with open(os.path.join(SDE_DIR, os.path.basename(n)), "wb") as f:
                    f.write(z.read(n))


def build_sde_cache():
    """Extract just what we need from the SDE into a small json file."""
    print("Parsing SDE (one-time, takes ~30s)...")
    groups = {g["_key"]: g for g in read_jsonl("groups.jsonl")}
    types = {}
    for t in read_jsonl("types.jsonl"):
        types[t["_key"]] = {
            "name": en(t.get("name")), "group": t.get("groupID"),
            "meta": t.get("metaGroupID"), "portion": t.get("portionSize", 1),
            "published": t.get("published", False), "market": t.get("marketGroupID"),
            "base_price": t.get("basePrice", 0),
        }
    bps = {b["_key"]: b for b in read_jsonl("blueprints.jsonl")}

    # which T1 blueprint invents into which T2 blueprint
    invented_from = {}
    for bp_id, b in bps.items():
        inv = b["activities"].get("invention")
        if not inv:
            continue
        for p in inv.get("products", []):
            invented_from[p["typeID"]] = {
                "t1_bp": bp_id, "prob": p.get("probability", 0), "runs": p["quantity"],
                "materials": inv.get("materials", []), "skills": inv.get("skills", []),
                "time": inv.get("time", 0),
            }

    cat_names = {c["_key"]: en(c["name"]) for c in read_jsonl("categories.jsonl")}
    items = []
    for bp_id, b in bps.items():
        m = b["activities"].get("manufacturing")
        if not m or not m.get("products") or not m.get("materials"):
            continue
        prod = m["products"][0]
        pt, bt = types.get(prod["typeID"]), types[bp_id]
        g = groups.get(pt["group"]) if pt else None
        if not pt or not pt["published"] or not pt["market"] or not g:
            continue
        gname = en(g["name"])
        tab = topic(pt["name"], gname, g["categoryID"], cat_names.get(g["categoryID"]))
        base = {"type_id": prod["typeID"], "name": pt["name"], "group": gname,
                "units_per_run": prod["quantity"], "bp_id": bp_id,
                "mfg_materials": m["materials"], "mfg_time": m.get("time", 0)}

        if bp_id in invented_from and pt["meta"] == TECH2_META_GROUP:
            # T2: invented from a T1 blueprint copy
            if not tab:
                continue
            inv = invented_from[bp_id]
            t1_bp = bps[inv["t1_bp"]]
            items.append({**base, "cat_hint": tab,
                "t1_bp_id": inv["t1_bp"], "t1_bp_name": types[inv["t1_bp"]]["name"],
                "t1_product_id": t1_bp["activities"]["manufacturing"]["products"][0]["typeID"],
                "t1_mfg_materials": t1_bp["activities"]["manufacturing"]["materials"],
                "inv_prob": inv["prob"], "inv_runs": inv["runs"], "inv_time": inv["time"],
                "copy_time": t1_bp["activities"].get("copying", {}).get("time", 0),
                "inv_materials": inv["materials"],
                "inv_skills": [{"name": types[s["typeID"]]["name"], "level": s["level"]} for s in inv["skills"]]})
            continue

        # T1: built from a BPO you can buy from NPCs
        if not bt["published"] or not bt["market"] or pt["meta"] not in (None, 1):
            continue
        if tab is None:
            if not all(x["typeID"] in MINERALS for x in m["materials"]):
                continue
            tab = "mineral_builds"
        items.append({**base, "cat_hint": tab, "bpo": True, "bpo_price": bt["base_price"],
            **({"group": cat_names.get(g["categoryID"], "Other"), "subgroup": gname} if tab == "mineral_builds" else {}),
            "t1_bp_id": bp_id, "t1_bp_name": bt["name"], "t1_product_id": prod["typeID"],
            "t1_mfg_materials": m["materials"], "inv_prob": 1.0, "inv_runs": 1, "inv_time": 0,
            "inv_materials": [], "inv_skills": []})

    decryptors = {}
    dec_ids = {k for k, t in types.items() if t["group"] == DECRYPTOR_GROUP and t["published"]}
    for d in read_jsonl("typeDogma.jsonl"):
        if d["_key"] in dec_ids:
            a = {x["attributeID"]: x["value"] for x in d["dogmaAttributes"]}
            decryptors[d["_key"]] = {
                "name": types[d["_key"]]["name"], "prob": a.get(ATTR_PROB, 1.0),
                "me": int(a.get(ATTR_ME, 0)), "te": int(a.get(ATTR_TE, 0)),
                "runs": int(a.get(ATTR_RUNS, 0)),
            }

    needed = set()
    for it in items:
        needed |= {x["typeID"] for x in it["mfg_materials"] + it["inv_materials"] + it["t1_mfg_materials"]}
        needed |= {it["type_id"], it["t1_product_id"]}
    names = {k: types[k]["name"] for k in needed | set(decryptors)}
    systems = {en(s["name"]).lower(): s["_key"] for s in read_jsonl("mapSolarSystems.jsonl")}
    sde_ver = next(read_jsonl("_sde.jsonl"), {})

    # every blueprint's manufacturing / invention recipe, for costing your real jobs
    bp_mfg, bp_inv = {}, {}
    for bp_id, b in bps.items():
        m = b["activities"].get("manufacturing")
        if m and m.get("products"):
            bp_mfg[bp_id] = {"p": m["products"][0]["typeID"], "q": m["products"][0]["quantity"],
                             "m": [[x["typeID"], x["quantity"]] for x in m.get("materials", [])]}
        inv = b["activities"].get("invention")
        if inv and inv.get("products"):
            bp_inv[bp_id] = {"p": {p["typeID"]: p["quantity"] for p in inv["products"]},
                             "m": [[x["typeID"], x["quantity"]] for x in inv.get("materials", [])]}
    all_names = {k: t["name"] for k, t in types.items() if t["published"] or k in bps}
    skill_names = {k: t["name"] for k, t in types.items()
                   if t["published"] and groups.get(t["group"], {}).get("categoryID") == SKILL_CATEGORY}
    cache = {"version": SDE_CACHE_VERSION, "skill_names": skill_names, "bp_mfg": bp_mfg, "bp_inv": bp_inv,
             "all_names": all_names, "sde": sde_ver, "items": items, "decryptors": decryptors, "names": names, "systems": systems}
    with open(SDE_CACHE, "w", encoding="utf8") as f:
        json.dump(cache, f)
    return cache


def load_sde(update=False):
    if update or not os.path.exists(os.path.join(SDE_DIR, "blueprints.jsonl")):
        download_sde()
        update = True
    if update or not os.path.exists(SDE_CACHE):
        return build_sde_cache()
    with open(SDE_CACHE, encoding="utf8") as f:
        c = json.load(f)
    if c.get("version") != SDE_CACHE_VERSION:
        return build_sde_cache()
    c["decryptors"] = {int(k): v for k, v in c["decryptors"].items()}
    c["names"] = {int(k): v for k, v in c["names"].items()}
    c["skill_names"] = {int(k): v for k, v in c["skill_names"].items()}
    c["all_names"] = {int(k): v for k, v in c["all_names"].items()}
    c["bp_mfg"] = {int(k): v for k, v in c["bp_mfg"].items()}
    c["bp_inv"] = {int(k): {"p": {int(a): b for a, b in v["p"].items()}, "m": v["m"]} for k, v in c["bp_inv"].items()}
    return c


# --------------------------------------------------------------------------- ESI

def esi_get(path, raw=False, **params):
    """GET from ESI with retries. Backs off when ESI says we're going too fast (429 / 420)."""
    for attempt in range(6):
        try:
            r = session.get(f"{ESI}{path}", params=params, timeout=30)
            if r.status_code == 404:
                return (r, []) if raw else []
            if r.status_code in (420, 429, 502, 503, 504):
                wait = float(r.headers.get("Retry-After") or r.headers.get("X-Esi-Error-Limit-Reset") or 2 ** attempt)
                time.sleep(min(wait, 60))
                continue
            r.raise_for_status()
            return (r, r.json()) if raw else r.json()
        except requests.RequestException:
            if attempt == 5:
                raise
            time.sleep(2 ** attempt)
    raise RuntimeError(f"ESI kept refusing {path} - try the update again in a minute")


def fetch_station_orders(region, station):
    """All orders in the region, page by page (far fewer requests than one per item),
    keeping only those at the market station. Returns {type_id: [orders]}."""
    first, data = esi_get(f"/markets/{region}/orders/", raw=True, order_type="all", page=1)
    pages = int(first.headers.get("X-Pages", 1))
    with ThreadPoolExecutor(max_workers=6) as ex:
        rest = list(ex.map(lambda p: esi_get(f"/markets/{region}/orders/", order_type="all", page=p), range(2, pages + 1)))
    by_type, for_sale = {}, set()
    for page in [data] + rest:
        for o in page:
            if not o["is_buy_order"]:
                for_sale.add(o["type_id"])  # anything someone sells anywhere in the region
            if o["location_id"] == station:
                by_type.setdefault(o["type_id"], []).append(o)
    return by_type, pages, for_sale


HISTORY_CACHE = os.path.join(HERE, "history_cache.json")


def fetch_histories(region, type_ids, keep_days=40):
    """Price history per item. ESI only updates it once a day, so it's cached on disk
    and only fetched again the next day."""
    today = dt.datetime.now(dt.timezone.utc).date().isoformat()
    cache = {}
    if os.path.exists(HISTORY_CACHE):
        with open(HISTORY_CACHE, encoding="utf8") as f:
            cache = json.load(f)
    key = lambda t: f"{region}:{t}"
    todo = [t for t in type_ids if cache.get(key(t), {}).get("fetched") != today]
    cutoff = (dt.date.today() - dt.timedelta(days=keep_days)).isoformat()

    def one(t):
        rows = esi_get(f"/markets/{region}/history/", type_id=t)
        return t, [h for h in rows if h["date"] >= cutoff]

    if todo:
        print(f"  price history: {len(todo)} items to refresh (cached for the rest of the day)")
        with ThreadPoolExecutor(max_workers=6) as ex:
            for t, rows in ex.map(one, todo):
                cache[key(t)] = {"fetched": today, "rows": rows}
        with open(HISTORY_CACHE, "w", encoding="utf8") as f:
            json.dump(cache, f)
    return {t: cache.get(key(t), {}).get("rows", []) for t in type_ids}


def fill_price(sells, qty):
    """Average price to buy `qty` units by walking the sell order book
    (cheapest first). If the book is too thin, the rest is priced at the
    most expensive order we had to touch."""
    if not sells:
        return None
    got, cost, last = 0, 0.0, sells[0]["price"]
    for o in sells:
        take = min(o["volume_remain"], qty - got)
        got += take
        cost += take * o["price"]
        last = o["price"]
        if got >= qty:
            break
    return (cost + (qty - got) * last) / qty


def summarize_market(orders):
    sells = sorted((o for o in orders if not o["is_buy_order"]), key=lambda o: o["price"])
    buys = [o for o in orders if o["is_buy_order"]]
    return {
        "sells": sells,
        "sell": sells[0]["price"] if sells else None,
        "buy": max((o["price"] for o in buys), default=None),
        "sell_units": sum(o["volume_remain"] for o in sells),
        "sell_orders": len(sells),
    }


def summarize_history(hist, days):
    if not hist:
        return {"avg_vol": 0.0, "typ_vol": 0.0, "avg_price": None, "vol_7d": 0.0, "trend": None}
    cutoff = (dt.date.today() - dt.timedelta(days=days)).isoformat()
    cut7 = (dt.date.today() - dt.timedelta(days=7)).isoformat()
    recent = [h for h in hist if h["date"] >= cutoff]
    last7 = [h for h in hist if h["date"] >= cut7]
    vol = sum(h["volume"] for h in recent)
    avg_vol = vol / days  # days without trades count as zero
    # "typical" volume: average without the single biggest day, so one dump
    # doesn't inflate it, but lumpy items (sold 20 at a time) don't drop to 0
    # the way a median would
    peak = max((h["volume"] for h in recent), default=0)
    typ_vol = (vol - peak) / (days - 1) if days > 1 else avg_vol
    avg_price = (sum(h["average"] * h["volume"] for h in recent) / vol) if vol else None
    vol_7d = sum(h["volume"] for h in last7) / 7
    trend = None
    if len(recent) >= 10:
        half = len(recent) // 2
        p1 = sum(h["average"] for h in recent[:half]) / half
        p2 = sum(h["average"] for h in recent[half:]) / (len(recent) - half)
        trend = (p2 - p1) / p1 if p1 else None
    return {"avg_vol": avg_vol, "typ_vol": typ_vol, "avg_price": avg_price, "vol_7d": vol_7d, "trend": trend}


# --------------------------------------------------------------------------- math

def invention_chance(item, cfg, dec):
    sk = cfg["skills"]
    science, enc = [], 0
    for s in item["inv_skills"]:
        lvl = sk.get("overrides", {}).get(s["name"])
        if "Encryption Methods" in s["name"]:
            enc = lvl if lvl is not None else sk["encryption"]
        else:
            science.append(lvl if lvl is not None else sk["science"])
    mult = 1 + sum(science) / 30 + enc / 40
    return min(1.0, item["inv_prob"] * mult * (dec["prob"] if dec else 1.0))


def mat_qty(base, runs, me, struct_bonus=0.0):
    """Material quantity for a job of `runs` runs at blueprint ME `me` (percent)
    in a structure with material bonus `struct_bonus` (fraction)."""
    return max(runs, math.ceil(round(runs * base * (1 - me / 100) * (1 - struct_bonus), 2)))


def evaluate(item, dec, cfg, px, adj, idx):
    """Full cost breakdown for one T2 BPC produced via invention with `dec`."""
    names = cfg["_names"]
    mat_mode = cfg["material_price_mode"]

    def price(tid, qty=1):
        p = px.get(tid, {})
        if mat_mode == "buy" and p.get("buy"):
            return p["buy"]
        return fill_price(p.get("sells"), qty) or p.get("buy")

    missing = []
    bpo = item.get("bpo", False)
    if bpo:  # built from your own BPO: no invention, researched ME
        dec = None
        owned_me = cfg.get("_owned_bpo", {}).get(item["bp_id"])
        chance, runs = 1.0, cfg.get("bpo_runs", 10)
        me = owned_me if owned_me is not None else cfg.get("bpo_me", 10)
    else:
        chance = invention_chance(item, cfg, dec)
        runs = item["inv_runs"] + (dec["runs"] if dec else 0)
        me = 2 + (dec["me"] if dec else 0)
    job_rate = cfg["facility_tax"] + cfg["scc_surcharge"]

    # invention attempt: datacores + decryptor + T1 copy job + invention job
    attempt = 0.0
    for m in item["inv_materials"]:
        p = price(m["typeID"], math.ceil(m["quantity"] / max(chance, 0.01)))
        if p is None:
            missing.append(names.get(m["typeID"], m["typeID"]))
            p = 0
        attempt += p * m["quantity"]
    if dec:
        p = price(dec["type_id"])
        if p is None:
            missing.append(dec["name"])
            p = 0
        attempt += p
    t1_eiv = sum(adj.get(m["typeID"], 0) * m["quantity"] for m in item["t1_mfg_materials"])
    t2_eiv = sum(adj.get(m["typeID"], 0) * m["quantity"] for m in item["mfg_materials"])
    if not bpo:
        attempt += t1_eiv * 0.02 * (idx["copying"] + job_rate)
        attempt += t2_eiv * 0.02 * (idx["invention"] + job_rate)
    invention_per_bpc = attempt / chance if chance else float("inf")

    # manufacturing the whole BPC
    materials, mat_cost = [], 0.0
    for m in item["mfg_materials"]:
        q = mat_qty(m["quantity"], runs, me, cfg["structure_me_bonus"])
        p = price(m["typeID"], q)
        if p is None:
            missing.append(names.get(m["typeID"], m["typeID"]))
            p = 0
        mat_cost += q * p
        materials.append({"tid": m["typeID"], "name": names.get(m["typeID"], str(m["typeID"])), "qty": q, "unit": p, "total": q * p})
    mfg_job = t2_eiv * runs * (idx["manufacturing"] * (1 - cfg["structure_job_cost_bonus"]) + job_rate)

    # job times (hours): Industry -4%/lvl on manufacturing, Advanced Industry -3%/lvl on all jobs,
    # Science -5%/lvl on copying; blueprint TE and structure TE bonus on manufacturing
    def lvl(name, default=4):
        v = cfg["skills"].get("overrides", {}).get(name)
        return v if v is not None else (0 if cfg.get("_character") else default)
    ind, adv, sci = lvl("Industry", 5), lvl("Advanced Industry"), lvl("Science", 5)
    if bpo:
        te = cfg.get("_owned_bpo_te", {}).get(item["bp_id"], cfg.get("bpo_te", 20))
    else:
        te = 4 + (dec["te"] if dec else 0)
    mfg_hours = (item["mfg_time"] * runs * (1 - te / 100) * (1 - 0.04 * ind) * (1 - 0.03 * adv)
                 * (1 - cfg.get("structure_te_bonus", 0.0)) / 3600)
    lab_hours = 0.0
    if not bpo and chance:
        per_attempt = (item["inv_time"] * (1 - 0.03 * adv)
                       + item.get("copy_time", 0) * (1 - 0.05 * sci) * (1 - 0.03 * adv))
        lab_hours = per_attempt / chance / 3600

    units = runs * item["units_per_run"]
    cost = invention_per_bpc + mat_cost + mfg_job

    mk = px.get(item["type_id"], {})
    if cfg["product_price_mode"] == "buy":
        sell_price, fee_rate = mk.get("buy"), cfg["sales_tax"]
    else:
        sell_price, fee_rate = mk.get("sell"), cfg["sales_tax"] + cfg["broker_fee"]
    if sell_price is None:
        sell_price = mk.get("hist_price")  # nothing listed at the hub: fall back to history
    revenue = (sell_price or 0) * units * (1 - fee_rate)
    profit = revenue - cost
    hist_price = mk.get("hist_price")
    profit_hist = (hist_price * units * (1 - fee_rate) - cost) if hist_price else None
    return {
        "decryptor": "-" if bpo else dec["name"] if dec else "None", "chance": chance, "bpo": bpo,
        "owned": bpo and item["bp_id"] in cfg.get("_owned_bpo", {}), "runs": runs, "me": me,
        "units": units, "attempt_cost": attempt, "invention_per_bpc": invention_per_bpc,
        "material_cost": mat_cost, "mfg_job": mfg_job, "cost": cost,
        "cost_per_unit": cost / units, "sell_price": sell_price, "revenue": revenue,
        "profit": profit, "profit_per_unit": profit / units, "no_market": sell_price is None,
        "roi": profit / cost if cost else 0,
        "hist_price": hist_price,
        "profit_per_unit_hist": profit_hist / units if profit_hist is not None else None,
        "roi_hist": profit_hist / cost if profit_hist is not None and cost else None,
        "profit_per_attempt": chance * revenue - chance * (mat_cost + mfg_job) - attempt,
        "mfg_hours": mfg_hours, "lab_hours": lab_hours, "te": te,
        "materials": materials, "missing": sorted(set(map(str, missing))),
    }


# --------------------------------------------------------------------------- character

def apply_character(cfg, sde, do_login):
    """Replace the guessed skills/fees in cfg with the real ones from ESI."""
    client_id = cfg.get("esi_client_id")
    if not client_id:
        if do_login:
            sys.exit("Put your application's Client ID into config.json (esi_client_id) first.")
        return None
    if do_login:
        esi_auth.login(client_id, cfg.get("esi_callback", "http://localhost:8765/callback"))
    char = esi_auth.load_character(client_id, cfg["market_station_id"])
    if not char:
        return None

    levels = {sde["skill_names"].get(k, str(k)): v for k, v in char["skills"].items()}
    # every invention skill is looked up by name; untrained = 0
    cfg["skills"] = {"science": 0, "encryption": 0, "overrides": levels}
    acc, br = levels.get("Accounting", 0), levels.get("Broker Relations", 0)
    cfg["sales_tax"] = 0.075 * (1 - 0.11 * acc)
    cfg["broker_fee"] = max(0.0, 0.03 - 0.003 * br - 0.0003 * char["faction_standing"] - 0.0002 * char["corp_standing"])
    # originals only (quantity -1); keep the best-researched copy of each
    owned, owned_te = {}, {}
    for b in char["blueprints"]:
        if b["quantity"] == -1:
            owned[b["type_id"]] = max(owned.get(b["type_id"], 0), b["material_efficiency"])
            owned_te[b["type_id"]] = max(owned_te.get(b["type_id"], 0), b["time_efficiency"])
    cfg["_owned_bpo"] = owned
    cfg["_owned_bpo_te"] = owned_te
    cfg["_char_raw"] = char
    print(f"Character: {char['name']} - Accounting {acc}, Broker Relations {br} -> sales tax {cfg['sales_tax']:.2%}, "
          f"broker fee {cfg['broker_fee']:.2%}; {len(owned)} BPOs owned")
    return {"name": char["name"], "id": char["id"], "accounting": acc, "broker": br,
            "faction_standing": char["faction_standing"], "corp_standing": char["corp_standing"],
            "bpos": len(owned), "sp": char["total_sp"],
            "skills": {n: l for n, l in levels.items()}}


# --------------------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--update-sde", action="store_true", help="re-download the SDE")
    ap.add_argument("--config", default=os.path.join(HERE, "config.json"))
    ap.add_argument("--login", action="store_true", help="log in with your EVE character")
    args = ap.parse_args()

    if not os.path.exists(args.config):  # first run from a fresh copy: start from the example
        import shutil
        shutil.copy(os.path.join(HERE, "config.example.json"), args.config)
        print("Created config.json from config.example.json - edit it to match your character.")
    with open(args.config, encoding="utf8") as f:
        cfg = json.load(f)
    sde = load_sde(args.update_sde)
    cfg["_names"] = sde["names"]
    cfg["_character"] = apply_character(cfg, sde, args.login)

    cats = cfg.get("categories") or list(CATEGORIES)
    bad = [c for c in cats if c not in CATEGORIES]
    if bad:
        sys.exit(f"Unknown category {bad}. Options: {', '.join(CATEGORIES)}")
    items = []
    for it in sde["items"]:
        it["cat"] = it["cat_hint"]
        if it["cat"] in cats:
            items.append(it)
    items.sort(key=lambda i: (cats.index(i["cat"]), i["group"], i["name"]))
    print(f"{len(items)} items (SDE build {sde['sde'].get('buildNumber')}): " +
          ", ".join(f"{sum(i['cat'] == c for i in items)} {CATEGORIES[c]}" for c in cats))

    decs = []
    for k, d in sde["decryptors"].items():
        decs.append(dict(d, type_id=k))
    dec_mode = cfg.get("decryptor", "none")
    if dec_mode not in ("none", "best") and not any(d["name"].lower() == dec_mode.lower() for d in decs):
        sys.exit(f"Unknown decryptor '{dec_mode}'. Options: none, best, " + ", ".join(d["name"] for d in decs))

    # ---- market data
    region, station = cfg["market_region_id"], cfg["market_station_id"]
    price_ids = set()
    for it in items:
        price_ids |= {m["typeID"] for m in it["mfg_materials"] + it["inv_materials"]}
        price_ids.add(it["type_id"])
    price_ids |= {d["type_id"] for d in decs}
    product_ids = {it["type_id"] for it in items}

    print(f"Fetching market orders + history for {len(price_ids)} types...")
    station_orders, pages, region_for_sale = fetch_station_orders(region, station)
    print(f"  {pages} pages of region orders")
    orders = {t: station_orders.get(t, []) for t in price_ids}
    cfg["_region_for_sale"] = region_for_sale
    history = fetch_histories(region, price_ids)
    prices = esi_get("/markets/prices/")
    adj = {p["type_id"]: p.get("adjusted_price", 0) for p in prices}
    avg_price = {p["type_id"]: p["average_price"] for p in prices if p.get("average_price")}

    sys_id = sde["systems"].get(cfg["industry_system"].lower())
    if not sys_id:
        sys.exit(f"Unknown solar system '{cfg['industry_system']}'")
    idx = {"manufacturing": 0.0, "invention": 0.0, "copying": 0.0}
    for s in esi_get("/industry/systems/"):
        if s["solar_system_id"] == sys_id:
            for c in s["cost_indices"]:
                if c["activity"] in idx:
                    idx[c["activity"]] = c["cost_index"]
    print(f"Cost indices in {cfg['industry_system']}: " + ", ".join(f"{k} {v:.2%}" for k, v in idx.items()))

    px = {t: summarize_market(o) for t, o in orders.items()}
    hist = {t: summarize_history(h, cfg["history_days"]) for t, h in history.items()}
    for t, h in hist.items():
        px.setdefault(t, {})["hist_price"] = h["avg_price"]

    base = next((d for d in decs if d["name"].lower() == dec_mode.lower()), None)
    rows = []
    for it in items:
        res = evaluate(it, base, cfg, px, adj, idx)
        options = [res] if it.get("bpo") else (
            [evaluate(it, None, cfg, px, adj, idx)] + [evaluate(it, d, cfg, px, adj, idx) for d in decs])
        best = max(options, key=lambda r: r["profit_per_attempt"])
        if dec_mode == "best":
            res = best
        h = hist[it["type_id"]]
        m = px.get(it["type_id"], {})
        sell_through = res["units"] / h["typ_vol"] if h["typ_vol"] else float("inf")
        rows.append({
            "item": it, "res": res, "best": best, "hist": h, "mkt": m,
            "daily_isk": h["avg_vol"] * (res["sell_price"] or 0),
            "sell_through_days": sell_through,
            "liquid": sell_through <= cfg["max_sell_through_days"] and not res["no_market"],
            "stock_days": m.get("sell_units", 0) / h["typ_vol"] if h["typ_vol"] else float("inf"),
            # profit/day if you could capture 10% of a typical day's volume, at the
            # lower of today's price and the 30-day average so price spikes don't rank high
            "profit_day_10pct": h["typ_vol"] * 0.10 * min(res["profit_per_unit"], res["profit_per_unit_hist"]
                                                          if res["profit_per_unit_hist"] is not None else res["profit_per_unit"]),
        })
    rows.sort(key=lambda r: (r["liquid"], r["profit_day_10pct"]), reverse=True)

    os.makedirs(OUT_DIR, exist_ok=True)
    write_csv(rows)
    write_crafting_list(items, sde["names"])
    mats = material_overview(items, decs, px, hist, sde["names"])
    market = {**avg_price, **{t: p["sell"] for t, p in px.items() if p.get("sell")}}
    track = tracking.build(cfg.get("_char_raw"), cfg, {
        "names": sde["all_names"], "bp_mfg": sde["bp_mfg"], "bp_inv": sde["bp_inv"], "market": market,
        "est_cpu": {r["item"]["type_id"]: r["res"]["cost_per_unit"] for r in rows},
        "est_inv": {r["item"]["bp_id"]: r["res"]["invention_per_bpc"] for r in rows if not r["res"]["bpo"]},
        "bpc_runs": {bp: runs for inv in sde["bp_inv"].values() for bp, runs in inv["p"].items()},
        "mat_ids": {m["typeID"] for it in items for m in it["mfg_materials"] + it["inv_materials"]} | {d["type_id"] for d in decs},
    })
    if track:
        bad = sum(o["status"] in ("undercut", "outbid") for o in track["orders"])
        print(f"Tracking: {len(track['orders'])} active orders ({bad} undercut/outbid), {track['tx_count']} transactions stored")
    path = write_html(rows, cfg, idx, sde, cats, mats, track)
    for c in cats:
        print(f"\n=== {CATEGORIES[c]}")
        print_top([r for r in rows if r["item"]["cat"] == c])
    print(f"\nReport: {path}")


# --------------------------------------------------------------------------- output

def isk(v, dec=0):
    if v is None or (isinstance(v, float) and (math.isinf(v) or math.isnan(v))):
        return "-"
    for div, suf in ((1e9, "b"), (1e6, "m"), (1e3, "k")):
        if abs(v) >= div:
            return f"{v / div:,.2f}{suf}"
    return f"{v:,.{dec}f}"


def print_top(rows):
    print(f"\n{'Item':42} {'Profit/BPC':>11} {'ROI':>7} {'ROI@avg':>8} {'Vol/day':>9} {'Sell-thru':>9} {'10%/day':>9}")
    for r in rows[:20]:
        res = r["res"]
        flag = "  (no market)" if res["no_market"] else "" if r["liquid"] else "  (illiquid)"
        roi_hist = f"{res['roi_hist']:.1%}" if res["roi_hist"] is not None else "-"
        print(f"{r['item']['name'][:42]:42} {isk(res['profit']):>11} {res['roi']:>7.1%} {roi_hist:>8} "
              f"{r['hist']['typ_vol']:>9,.0f} {r['sell_through_days']:>8.1f}d {isk(r['profit_day_10pct']):>9}{flag}")


def write_csv(rows):
    with open(os.path.join(OUT_DIR, "roi.csv"), "w", newline="", encoding="utf8") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["Item", "Category", "Group", "Decryptor", "Invention chance", "Runs", "Units per BPC",
                    "Invention cost per BPC", "Material cost", "Job cost", "Total cost", "Cost per unit",
                    "Sell price", "Profit per BPC", "Profit per unit", "ROI", "30d avg price", "ROI at 30d avg", "Median daily volume",
                    "Daily ISK volume", "Sell-through days", "Amarr stock days", "Profit/day @10% share",
                    "Best decryptor", "Best decryptor profit/attempt", "Missing prices"])
        for r in rows:
            res = r["res"]
            w.writerow([r["item"]["name"], CATEGORIES[r["item"]["cat"]], r["item"]["group"], res["decryptor"], round(res["chance"], 4),
                        res["runs"], res["units"], round(res["invention_per_bpc"]), round(res["material_cost"]),
                        round(res["mfg_job"]), round(res["cost"]), round(res["cost_per_unit"], 2),
                        res["sell_price"] and round(res["sell_price"], 2), round(res["profit"]),
                        round(res["profit_per_unit"], 2), round(res["roi"], 4),
                        res["hist_price"] and round(res["hist_price"], 2),
                        res["roi_hist"] is not None and round(res["roi_hist"], 4) or "",
                        round(r["hist"]["typ_vol"]),
                        round(r["daily_isk"]), round(r["sell_through_days"], 1), round(r["stock_days"], 1),
                        round(r["profit_day_10pct"]), r["best"]["decryptor"],
                        round(r["best"]["profit_per_attempt"]), ", ".join(res["missing"])])


def write_crafting_list(items, names):
    with open(os.path.join(OUT_DIR, "crafting_list.csv"), "w", newline="", encoding="utf8") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["Item", "Category", "Group", "Units per run", "T1 blueprint (invent from)", "Base chance",
                    "BPC runs", "Invention materials", "Invention skills", "Manufacturing materials per run (ME0)"])
        for it in items:
            w.writerow([it["name"], CATEGORIES[it["cat"]], it["group"], it["units_per_run"], it["t1_bp_name"], it["inv_prob"],
                        it["inv_runs"],
                        ", ".join(f"{m['quantity']}x {names.get(m['typeID'])}" for m in it["inv_materials"]),
                        ", ".join(s["name"] for s in it["inv_skills"]),
                        ", ".join(f"{m['quantity']}x {names.get(m['typeID'])}" for m in it["mfg_materials"])])


def material_overview(items, decs, px, hist, names):
    """Every input (materials, datacores, decryptors) with its Amarr market state
    and which T2 items use it."""
    used = {}
    for it in items:
        for kind, mats in (("Material", it["mfg_materials"]), ("Datacore", it["inv_materials"])):
            for m in mats:
                u = used.setdefault(m["typeID"], {"kind": kind, "by": []})
                u["by"].append({"name": it["name"], "cat": it["cat"], "qty": m["quantity"]})
    for d in decs:
        used.setdefault(d["type_id"], {"kind": "Decryptor", "by": []})
    out = []
    for tid, u in used.items():
        p, h = px.get(tid, {}), hist.get(tid, {})
        out.append({
            "tid": tid, "name": names.get(tid, str(tid)), "kind": u["kind"],
            "sell": p.get("sell"), "buy": p.get("buy"), "units": p.get("sell_units", 0),
            "orders": p.get("sell_orders", 0), "hprice": h.get("avg_price"),
            "vol": h.get("typ_vol", 0), "trend": h.get("trend"),
            "by": sorted(u["by"], key=lambda b: b["name"]),
            "cats": sorted({b["cat"] for b in u["by"]}),
        })
    return out


def write_html(rows, cfg, idx, sde, cats, mats, track=None):
    data = []
    for r in rows:
        res, it = r["res"], r["item"]
        # Tritanium economics: what each Tritanium earns when turned into this item
        trit = sum(m["qty"] for m in res["materials"] if m.get("tid") == TRITANIUM)
        trit_cost = sum(m["total"] for m in res["materials"] if m.get("tid") == TRITANIUM)
        revenue = res["revenue"]
        if res["roi_hist"] is not None:
            revenue = min(revenue, res["cost"] * (1 + res["roi_hist"]))  # conservative, like the ranking
        other = res["cost"] - trit_cost - res["invention_per_bpc"]
        data.append({
            "tid": it["type_id"], "bp": it["bp_id"], "cat": it["cat"],
            "bpo_for_sale": bool(it.get("bpo")) and it["bp_id"] in cfg.get("_region_for_sale", ()),
            "mfg_h": res["mfg_hours"], "lab_h": res["lab_hours"], "te": res["te"],
            "isk_h": res["profit"] / res["mfg_hours"] if res["mfg_hours"] else None,
            "isk_lab_h": res["profit"] / res["lab_hours"] if res["lab_hours"] else None,
            "trit": trit, "trit_share": trit_cost / res["material_cost"] if res["material_cost"] else None,
            "trit_isk": (revenue - other) / trit if trit else None, "name": it["name"], "group": it["group"].replace("Advanced ", ""), "t1": it["t1_bp_name"], "bpo": res["bpo"], "me": res["me"],
            "sub": it.get("subgroup"), "bpoprice": it.get("bpo_price"), "runs_time": it["mfg_time"], "owned": res["owned"],
            "invskills": [{"name": sk["name"], "lvl": cfg["skills"].get("overrides", {}).get(sk["name"])} for sk in it["inv_skills"]],
            "dec": res["decryptor"], "chance": res["chance"], "runs": res["runs"], "units": res["units"],
            "inv": res["invention_per_bpc"], "mat": res["material_cost"], "job": res["mfg_job"],
            "cost": res["cost"], "cpu": res["cost_per_unit"], "price": res["sell_price"],
            "profit": res["profit"], "ppu": res["profit_per_unit"], "roi": res["roi"],
            "hprice": res["hist_price"], "roih": res["roi_hist"],
            "vol": r["hist"]["typ_vol"], "avgvol": r["hist"]["avg_vol"], "vol7": r["hist"]["vol_7d"], "trend": r["hist"]["trend"],
            "isk": r["daily_isk"], "st": r["sell_through_days"] if math.isfinite(r["sell_through_days"]) else None,
            "stock": r["stock_days"] if math.isfinite(r["stock_days"]) else None,
            "orders": r["mkt"].get("sell_orders", 0), "p10": r["profit_day_10pct"], "liquid": r["liquid"],
            "best": r["best"]["decryptor"], "bestppa": r["best"]["profit_per_attempt"],
            "ppa": res["profit_per_attempt"], "attempt": res["attempt_cost"],
            "materials": res["materials"], "missing": res["missing"], "nomarket": res["no_market"],
            "invmats": [f"{m['quantity']}x {sde['names'].get(m['typeID'])}" for m in it["inv_materials"]],
            "invlist": [{"tid": m["typeID"], "name": sde["names"].get(m["typeID"]), "qty": m["quantity"]} for m in it["inv_materials"]],
        })
    meta = {
        "generated": dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
        "sde": sde["sde"].get("buildNumber"), "system": cfg["industry_system"], "idx": idx,
        "matmode": cfg["material_price_mode"], "prodmode": cfg["product_price_mode"],
        "tax": cfg["sales_tax"], "broker": cfg["broker_fee"], "days": cfg["history_days"],
        "maxst": cfg["max_sell_through_days"], "dec": cfg.get("decryptor", "none"),
        "skills": cfg["skills"], "cats": [{"key": c, "title": CATEGORIES[c]} for c in cats],
    }
    with open(os.path.join(HERE, "report_template.html"), encoding="utf8") as f:
        tpl = f.read()
    meta["region"] = cfg.get("market_region_name", "Domain")
    ch = cfg.get("_character")
    meta["character"] = {k: v for k, v in ch.items() if k != "skills"} if ch else None
    meta["bpo_me"], meta["bpo_runs"] = cfg.get("bpo_me", 10), cfg.get("bpo_runs", 10)
    out = tpl.replace("/*TRACK*/null", json.dumps(track)).replace("/*MATS*/null", json.dumps(mats)).replace("/*DATA*/null", json.dumps(data)).replace("/*META*/null", json.dumps(meta))
    path = os.path.join(OUT_DIR, "report.html")
    with open(path, "w", encoding="utf8") as f:
        f.write(out)
    return path


if __name__ == "__main__":
    main()
