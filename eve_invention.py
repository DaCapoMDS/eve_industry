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
import shutil
import sys
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor

import requests

import esi_auth
import tracking
from chains import Chains

HERE = os.path.dirname(os.path.abspath(__file__))
SDE_DIR = os.path.join(HERE, "sde")
SDE_URL = "https://developers.eveonline.com/static-data/eve-online-static-data-latest-jsonl.zip"
SDE_CACHE = os.path.join(HERE, "sde_cache.json")
SDE_CACHE_VERSION = 25  # bump when build_sde_cache() extracts new things
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
    "deployables": "Deployables",
    "structures": "Structures & Sov",
    "faction": "Faction & BPC-only",
    "boosters": "Boosters",
    "t3": "T3 (Reverse Engineering)",
    "structure_mods": "Structure Modules & Rigs",
    "reactions": "Reactions (low/null-sec)",
}
STRUCTURE_T1_META, STRUCTURE_T2_META = 54, 53  # CCP's own "Structure Tech I / II" meta levels
# items only built from blueprint copies (LP stores, drops): which kinds of things we list
FACTION_CATEGORIES = {"Module", "Charge", "Ship", "Drone", "Fighter", "Implant", "Deployable"}
FACTION_SKIP_META = {2, 14, 15, 52, 54}  # T2, T3, mutaplasmids, structure variants


def topic(name, group, category_id, category):
    """Which tab an item belongs to by what it is, or None."""
    if name.startswith("Standup ") or category == "Structure Module":
        return "structure_mods"  # Upwell modules, rigs, fighters and charges
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
    if category == "Deployable":
        return "deployables"
    if category in ("Structure", "Orbitals", "Sovereignty Structures", "Infrastructure Upgrades"):
        return "structures"
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
    wanted = {"types.jsonl", "groups.jsonl", "categories.jsonl", "planetSchematics.jsonl", "blueprints.jsonl", "typeDogma.jsonl",
              "mapSolarSystems.jsonl", "_sde.jsonl", "npcCorporations.jsonl"}
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
    relic_from = {}
    for bp_id, b in bps.items():
        inv = b["activities"].get("invention")
        rg = groups.get(types.get(bp_id, {}).get("group"), {})
        if not inv or "Relic" not in (en(rg.get("name")) or ""):
            continue
        for p in inv.get("products", []):
            relic_from.setdefault(p["typeID"], []).append({
                "t1_bp_id": bp_id, "t1_bp_name": types[bp_id]["name"], "relic": True,
                "inv_prob": p.get("probability", 0), "inv_runs": p["quantity"], "inv_time": inv.get("time", 0),
                "inv_materials": inv.get("materials", []), "copy_time": 0, "t1_mfg_materials": [],
                "inv_skills": [{"name": types[x["typeID"]]["name"], "level": x["level"]} for x in inv.get("skills", [])]})
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
                "mfg_materials": m["materials"], "mfg_time": m.get("time", 0),
                "mfg_skills": [{"name": types[x["typeID"]]["name"], "level": x["level"]} for x in m.get("skills", []) if x["typeID"] in types]}

        if bp_id in relic_from and cat_names.get(g["categoryID"]) in ("Ship", "Subsystem"):
            # T3: reverse-engineered from a Sleeper relic; the best relic quality is picked at costing time
            variants = sorted(relic_from[bp_id], key=lambda v: -v["inv_runs"])
            items.append({**base, "cat_hint": "t3", **variants[0], "relics": variants,
                          "t1_bp_seeded": True, "t1_product_id": variants[0]["t1_bp_id"]})
            continue

        if bp_id in invented_from and pt["meta"] in (TECH2_META_GROUP, STRUCTURE_T2_META):
            # T2: invented from a T1 blueprint copy
            if not tab:
                continue
            inv = invented_from[bp_id]
            t1_bp = bps[inv["t1_bp"]]
            if not (t1_bp["activities"].get("manufacturing") or {}).get("products"):
                continue  # T1 blueprint without a buildable product (unreleased leftovers)
            items.append({**base, "cat_hint": tab,
                "t1_bp_id": inv["t1_bp"], "t1_bp_name": types[inv["t1_bp"]]["name"],
                "t1_bp_seeded": bool(types[inv["t1_bp"]]["market"]),
                "t1_product_id": t1_bp["activities"]["manufacturing"]["products"][0]["typeID"],
                "t1_mfg_materials": t1_bp["activities"]["manufacturing"]["materials"],
                "inv_prob": inv["prob"], "inv_runs": inv["runs"], "inv_time": inv["time"],
                "copy_time": t1_bp["activities"].get("copying", {}).get("time", 0),
                "inv_materials": inv["materials"],
                "inv_skills": [{"name": types[s["typeID"]]["name"], "level": s["level"]} for s in inv["skills"]]})
            continue

        # faction / BPC-only: the blueprint is never sold by NPCs, copies come from LP stores or drops
        if not bt["market"] and bp_id not in invented_from and pt["meta"] not in FACTION_SKIP_META \
                and cat_names.get(g["categoryID"]) in FACTION_CATEGORIES:
            items.append({**base, "cat_hint": "boosters" if gname == "Booster" else "faction", "bpo": True, "bpc_only": True, "bpo_price": 0,
                "t1_bp_id": bp_id, "t1_bp_name": bt["name"], "t1_product_id": prod["typeID"],
                "t1_mfg_materials": m["materials"], "inv_prob": 1.0, "inv_runs": 1, "inv_time": 0,
                "inv_materials": [], "inv_skills": []})
            continue

        # T1: built from a BPO you can buy from NPCs
        if not bt["published"] or not bt["market"] or pt["meta"] not in (None, 1, STRUCTURE_T1_META):
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

    for bp_id, b in bps.items():
        r = b["activities"].get("reaction")
        if not r or not r.get("products") or not r.get("materials"):
            continue
        prod = r["products"][0]
        pt, bt = types.get(prod["typeID"]), types[bp_id]
        g = groups.get(pt["group"]) if pt else None
        if not pt or not pt["published"] or not pt["market"] or not g or not bt["market"]:
            continue
        items.append({"type_id": prod["typeID"], "name": pt["name"], "group": en(g["name"]), "units_per_run": prod["quantity"],
                      "bp_id": bp_id, "mfg_materials": r["materials"], "mfg_time": r.get("time", 0),
                      "mfg_skills": [{"name": types[x["typeID"]]["name"], "level": x["level"]} for x in r.get("skills", []) if x["typeID"] in types],
                      "cat_hint": "reactions", "bpo": True, "reaction": True, "bpo_price": bt["base_price"],
                      "t1_bp_id": bp_id, "t1_bp_name": bt["name"], "t1_product_id": prod["typeID"],
                      "t1_mfg_materials": r["materials"], "inv_prob": 1.0, "inv_runs": 1, "inv_time": 0,
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
        needed |= {v["t1_bp_id"] for v in it.get("relics", [])}
    names = {k: types[k]["name"] for k in needed | set(decryptors)}
    systems = {en(s["name"]).lower(): s["_key"] for s in read_jsonl("mapSolarSystems.jsonl")}
    sde_ver = next(read_jsonl("_sde.jsonl"), {})
    corps = {c["_key"]: en(c["name"]) for c in read_jsonl("npcCorporations.jsonl")}

    # every blueprint's manufacturing / invention recipe, for costing your real jobs
    bp_mfg, bp_inv = {}, {}
    for bp_id, b in bps.items():
        m = b["activities"].get("manufacturing") or b["activities"].get("reaction")
        if m and m.get("products"):
            bp_mfg[bp_id] = {"p": m["products"][0]["typeID"], "q": m["products"][0]["quantity"],
                             "m": [[x["typeID"], x["quantity"]] for x in m.get("materials", [])],
                             **({"r": True} if "reaction" in b["activities"] else {}),
                             # blueprint sold by NPCs (you can buy it and build this yourself)
                             **({"s": True} if types.get(bp_id, {}).get("market") and types.get(bp_id, {}).get("published") else {})}
        inv = b["activities"].get("invention")
        if inv and inv.get("products"):
            bp_inv[bp_id] = {"p": {p["typeID"]: p["quantity"] for p in inv["products"]},
                             "m": [[x["typeID"], x["quantity"]] for x in inv.get("materials", [])]}
    all_names = {k: t["name"] for k, t in types.items() if t["published"] or k in bps}

    # planetary interaction: every schematic (P0->P1 ... P3->P4), tier from the product's group
    def pi_tier(tid):
        g = en(groups.get(types.get(tid, {}).get("group"), {}).get("name", "")) or ""
        return 1 if "Tier 1" in g else 2 if "Tier 2" in g else 3 if "Tier 3" in g else 4 if "Tier 4" in g else 0
    pi = []
    for sch in read_jsonl("planetSchematics.jsonl"):
        out = next(t for t in sch["types"] if not t["isInput"])
        ins = [t for t in sch["types"] if t["isInput"]]
        pi.append({"id": sch["_key"], "out": out["_key"], "q": out["quantity"], "cycle": sch["cycleTime"], "tier": pi_tier(out["_key"]),
                   "in": [[t["_key"], t["quantity"], pi_tier(t["_key"])] for t in ins]})
    skill_names = {k: t["name"] for k, t in types.items()
                   if t["published"] and groups.get(t["group"], {}).get("categoryID") == SKILL_CATEGORY}
    cache = {"version": SDE_CACHE_VERSION, "skill_names": skill_names, "pi": pi, "bp_mfg": bp_mfg, "bp_inv": bp_inv,
             "all_names": all_names, "sde": sde_ver, "items": items, "decryptors": decryptors, "names": names, "systems": systems, "corps": corps}
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
    c["corps"] = {int(k): v for k, v in c.get("corps", {}).items()}
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
LP_CACHE = os.path.join(HERE, "lp_cache.json")


def fetch_lp_offers(corp_ids):
    """Every NPC LP store's offers, cached for a week (they rarely change)."""
    if os.path.exists(LP_CACHE) and time.time() - os.path.getmtime(LP_CACHE) < 7 * 86400:
        with open(LP_CACHE, encoding="utf8") as f:
            return {int(k): v for k, v in json.load(f).items()}
    print(f"  LP stores: reading {len(corp_ids)} corporations (cached for a week)")

    def one(c):
        try:
            return c, esi_get(f"/loyalty/stores/{c}/offers/") or []
        except Exception:
            return c, []
    with ThreadPoolExecutor(max_workers=8) as ex:
        out = {c: o for c, o in ex.map(one, sorted(corp_ids)) if o}
    with open(LP_CACHE, "w", encoding="utf8") as f:
        json.dump(out, f)
    return out


def faction_bpc_sources(bp_ids, lp_offers, contract_prices, px, cfg, corps):
    """Cheapest way per blueprint to get copies: a contract, or an LP store offer
    (ISK + required items at market + LP valued at cfg lp_isk_value)."""
    lp_value = cfg.get("lp_isk_value", 700)
    out = {}
    for bp in bp_ids:
        cands = []
        c = contract_prices.get(bp)
        if c:
            cands.append({"src": "contract", "per_run": c["per_run"], "offers": c["offers"],
                          "runs": max(1, round(c["runs_offered"] / max(c["offers"], 1)))})
        for corp, offers in lp_offers.items():
            for o in offers:
                if o["type_id"] != bp or not o.get("quantity"):
                    continue
                isk, ok = o.get("isk_cost", 0), True
                for r in o.get("required_items", []):
                    p = px.get(r["type_id"], {})
                    unit = fill_price(p.get("sells"), r["quantity"]) or p.get("hist_price")
                    if not unit:
                        ok = False  # a required tag/item nobody sells: can't price this offer
                        break
                    isk += unit * r["quantity"]
                if not ok:
                    continue
                q = o["quantity"]
                cands.append({"src": "lp", "per_run": (isk + o["lp_cost"] * lp_value) / q, "runs": q,
                              "corp": corps.get(corp, str(corp)), "lp_run": o["lp_cost"] / q, "isk_run": isk / q,
                              "req": bool(o.get("required_items"))})
        if cands:
            best = min(cands, key=lambda x: x["per_run"])
            best["alts"] = len(cands) - 1
            out[bp] = best
    return out
CONTRACT_CACHE = os.path.join(HERE, "contracts_cache.json")


def fetch_bpc_prices(region, wanted):
    """Cheapest price per run for blueprint copies on public item-exchange contracts in the region.
    Only small-volume contracts (blueprints) are opened; their contents are cached because they never change."""
    first, data = esi_get(f"/contracts/public/{region}/", raw=True, page=1)
    pages = int(first.headers.get("X-Pages", 1))
    with ThreadPoolExecutor(max_workers=4) as ex:
        rest = list(ex.map(lambda p: esi_get(f"/contracts/public/{region}/", page=p), range(2, pages + 1)))
    live = [c for page in [data] + rest for c in page
            if c["type"] == "item_exchange" and c.get("price", 0) > 0 and c.get("volume", 1) <= 1]
    cache = {}
    if os.path.exists(CONTRACT_CACHE):
        with open(CONTRACT_CACHE, encoding="utf8") as f:
            cache = json.load(f)
    todo = [c for c in live if str(c["contract_id"]) not in cache]
    if todo:
        print(f"  contracts: reading {len(todo)} new blueprint contracts (cached afterwards)")
        with ThreadPoolExecutor(max_workers=6) as ex:
            for c, items in zip(todo, ex.map(lambda c: esi_get(f"/contracts/public/items/{c['contract_id']}/"), todo)):
                cache[str(c["contract_id"])] = [[i["type_id"], i.get("runs", 0), i.get("quantity", 1), bool(i.get("is_blueprint_copy"))]
                                                for i in (items or []) if i.get("is_included", True)]
    live_ids = {str(c["contract_id"]): c for c in live}
    cache = {k: v for k, v in cache.items() if k in live_ids}  # drop finished / expired contracts
    with open(CONTRACT_CACHE, "w", encoding="utf8") as f:
        json.dump(cache, f)
    offers = {}
    for cid, items in cache.items():
        # only contracts that are nothing but copies of one blueprint
        if not items or len({i[0] for i in items}) != 1 or not all(i[3] for i in items):
            continue
        tid = items[0][0]
        runs = sum(max(i[1], 1) * max(i[2], 1) for i in items)
        if tid in wanted and runs:
            offers.setdefault(tid, []).append((live_ids[cid]["price"] / runs, runs))
    prices = {}
    for tid, o in offers.items():
        o.sort()
        cheapest = o[:3]  # the few cheapest offers, so one odd contract doesn't decide alone
        prices[tid] = {"per_run": sum(p for p, _ in cheapest) / len(cheapest), "offers": len(o),
                       "runs_offered": sum(r for _, r in o)}
    return prices



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
        try:
            rows = esi_get(f"/markets/{region}/history/", type_id=t)
        except requests.HTTPError as e:
            if e.response is not None and e.response.status_code in (400, 404):
                return t, []  # not tradable on the market (e.g. some LP store requirements)
            raise
        return t, [h for h in rows if h["date"] >= cutoff]

    if todo:
        print(f"  price history: {len(todo)} items to refresh (cached for the rest of the day)")
        with ThreadPoolExecutor(max_workers=6) as ex:
            for t, rows in ex.map(one, todo):
                cache[key(t)] = {"fetched": today, "rows": rows}
        with open(HISTORY_CACHE, "w", encoding="utf8") as f:
            json.dump(cache, f)
    return {t: cache.get(key(t), {}).get("rows", []) for t in type_ids}


MUTA_WORDS = ("Miner", "Mining", "Harvester", "Gas Cloud", "Excavator")  # the mining side of mutaplasmids


def mutaplasmid_overview(station_orders, region, char):
    """Mining mutaplasmids: what each roll can do to which module, prices at the hub,
    and the abyssal modules you own with their real rolls."""
    types = {t["_key"]: t for t in read_jsonl("types.jsonl")}
    name = lambda k: en(types[k]["name"]) if k in types else str(k)
    attrs = {a["_key"]: a for a in read_jsonl("dogmaAttributes.jsonl")}
    mutas, bases, results = [], set(), {}
    for d in read_jsonl("dynamicItemAttributes.jsonl"):
        n = name(d["_key"])
        out = [m for m in d.get("inputOutputMapping", []) if m.get("resultingType")]
        if not out or not any(w in name(out[0]["resultingType"]) for w in MUTA_WORDS):
            continue
        if not types.get(d["_key"], {}).get("published"):
            continue
        app = [t for m in out for t in m.get("applicableTypes", []) if types.get(t, {}).get("published")]
        res = out[0]["resultingType"]
        results[res] = name(res)
        bases |= set(app)
        quality = n.split(" ")[0]
        mutas.append({"tid": d["_key"], "name": n, "quality": quality, "result": res, "result_name": name(res), "bases": app,
                      "attrs": [{"id": a["_key"], "min": a["min"], "max": a["max"],
                                 "good": a["highIsGood"] if a.get("highIsGood") is not None else attrs.get(a["_key"], {}).get("highIsGood", True)}
                                for a in d["attributeIDs"]]})
    used_attrs = {a["id"] for m in mutas for a in m["attrs"]}
    stats = {}
    for td in read_jsonl("typeDogma.jsonl"):
        if td["_key"] in bases:
            stats[td["_key"]] = {x["attributeID"]: x["value"] for x in td["dogmaAttributes"] if x["attributeID"] in used_attrs}
    ids = {m["tid"] for m in mutas} | bases
    hist = fetch_histories(region, ids)
    price = {}
    for t in ids:
        mk = summarize_market(station_orders.get(t, []))
        h = summarize_history(hist.get(t, []), 30)
        price[t] = {"sell": mk.get("sell"), "buy": mk.get("buy"), "avg": h["avg_price"], "vol": h["typ_vol"], "units": mk.get("sell_units", 0)}
    for m in mutas:
        m.update(price[m["tid"]])
    base_list = {t: {"tid": t, "name": name(t), "stats": {str(k): v for k, v in stats.get(t, {}).items()},
                     "defaults": {}, **price[t]} for t in bases}
    for t, b in base_list.items():  # attributes a module doesn't list use the attribute's default value
        for a in used_attrs:
            if str(a) not in b["stats"]:
                b["stats"][str(a)] = attrs.get(a, {}).get("defaultValue", 0.0)
    # your own abyssal mining modules, with what they really rolled (public ESI dogma)
    mine = []
    for a in (char or {}).get("assets") or []:
        if a["type_id"] in results:
            try:
                dyn = esi_get(f"/dogma/dynamic/items/{a['type_id']}/{a['item_id']}/")
            except Exception:
                continue
            if dyn:
                mine.append({"item_id": a["item_id"], "type": a["type_id"], "name": results[a["type_id"]],
                             "source": dyn.get("source_type_id"), "mutator": dyn.get("mutator_type_id"),
                             "stats": {str(x["attribute_id"]): x["value"] for x in dyn.get("dogma_attributes", []) if x["attribute_id"] in used_attrs}})
    names = {str(k): name(k) for k in ids | {x["mutator"] for x in mine if x.get("mutator")}}
    return {"mutas": sorted(mutas, key=lambda m: (m["result_name"], m["name"])), "bases": {str(k): v for k, v in base_list.items()},
            "attrs": {str(a): {"name": en(attrs[a].get("displayName")) or attrs[a]["name"], "unit": attrs[a].get("unitID")} for a in used_attrs if a in attrs},
            "mine": mine, "names": names}


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
        return {"avg_vol": 0.0, "typ_vol": 0.0, "avg_price": None, "vol_7d": 0.0, "trend": None, "buy_flow": 0.0, "buy_share": None}
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
    # how much was sold INTO buy orders: a day's average sits between the buy price (~ the day's low)
    # and the sell price (~ the day's high), so share at buy orders = (high - avg) / (high - low)
    into_buys = 0.0
    for h in recent:
        if h["highest"] > h["lowest"]:
            into_buys += h["volume"] * min(1.0, max(0.0, (h["highest"] - h["average"]) / (h["highest"] - h["lowest"])))
    known = sum(h["volume"] for h in recent if h["highest"] > h["lowest"])
    buy_share = into_buys / known if known else None
    return {"avg_vol": avg_vol, "typ_vol": typ_vol, "avg_price": avg_price, "vol_7d": vol_7d, "trend": trend,
            "buy_flow": typ_vol * buy_share if buy_share is not None else 0.0, "buy_share": buy_share}


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
    fbpc = cfg.get("_fbpc", {}).get(item["bp_id"]) if item.get("bpc_only") else None
    if fbpc:  # faction / BPC-only: one bought copy, unresearched
        dec = None
        chance, runs = 1.0, fbpc["runs"]
        me = cfg.get("faction_bpc_me", 0)
    elif item.get("bpc_only"):  # copy-only blueprint with no copy on offer: cost it as one unresearched copy
        dec = None
        chance, runs = 1.0, cfg.get("bpo_runs", 10)
        me = cfg.get("faction_bpc_me", 0)
    elif item.get("reaction"):  # reaction formula: no research possible; a batch = about a day of runs
        dec = None
        chance, me = 1.0, 0
        runs = max(1, round(86400 / max(item["mfg_time"], 1)))
    elif bpo:  # built from your own BPO: no invention, researched ME
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
    dec_price = 0.0
    if dec:
        p = price(dec["type_id"])
        if p is None:
            missing.append(dec["name"])
            p = 0
        attempt += p
        dec_price = p
    t1_eiv = sum(adj.get(m["typeID"], 0) * m["quantity"] for m in item["t1_mfg_materials"])
    t2_eiv = sum(adj.get(m["typeID"], 0) * m["quantity"] for m in item["mfg_materials"])
    bpc_run = None
    if not bpo:
        if item.get("relic"):
            rp = price(item["t1_bp_id"])
            if rp is None:
                missing.append(item["t1_bp_name"])
                rp = 0
            attempt += rp  # the relic is consumed by each reverse-engineering attempt
        elif item.get("t1_bp_seeded", True):
            attempt += t1_eiv * 0.02 * (idx["copying"] + job_rate)  # copy your own T1 BPO
        else:
            offer = cfg.get("_bpc_prices", {}).get(item["t1_bp_id"])
            bpc_run = offer["per_run"] if offer else None
            attempt += bpc_run or 0.0  # one bought T1 BPC run per attempt
        attempt += t2_eiv * 0.02 * (idx["invention"] + job_rate)
    if fbpc:
        bpc_run = fbpc["per_run"]
        attempt += bpc_run * runs  # the copy itself
    invention_per_bpc = attempt / chance if chance else float("inf")

    # manufacturing the whole BPC
    materials, mat_cost = [], 0.0
    for m in item["mfg_materials"]:
        q = mat_qty(m["quantity"], runs, me, cfg.get("reaction_me_bonus", 0.0) if item.get("reaction") else cfg["structure_me_bonus"])
        p = price(m["typeID"], q)
        if p is None:
            missing.append(names.get(m["typeID"], m["typeID"]))
            p = 0
        mat_cost += q * p
        materials.append({"tid": m["typeID"], "name": names.get(m["typeID"], str(m["typeID"])), "qty": q, "unit": p, "total": q * p,
                          "avail": px.get(m["typeID"], {}).get("sell_units", 0)})  # units for sale at the hub right now
    if item.get("reaction"):
        mfg_job = t2_eiv * runs * (idx.get("reaction", 0.0) + job_rate)
    else:
        mfg_job = t2_eiv * runs * (idx["manufacturing"] * (1 - cfg["structure_job_cost_bonus"]) + job_rate)

    # job times (hours): Industry -4%/lvl on manufacturing, Advanced Industry -3%/lvl on all jobs,
    # Science -5%/lvl on copying; blueprint TE and structure TE bonus on manufacturing
    def lvl(name, default=4):
        v = cfg["skills"].get("overrides", {}).get(name)
        return v if v is not None else (0 if cfg.get("_character") else default)
    ind, adv, sci = lvl("Industry", 5), lvl("Advanced Industry"), lvl("Science", 5)
    if item.get("reaction") or (item.get("bpc_only") and not fbpc):
        te = 0
    elif fbpc:
        te = cfg.get("faction_bpc_te", 0)
    elif bpo:
        te = cfg.get("_owned_bpo_te", {}).get(item["bp_id"], cfg.get("bpo_te", 20))
    else:
        te = 4 + (dec["te"] if dec else 0)
    if item.get("reaction"):  # Reactions skill: -4% time per level
        mfg_hours = item["mfg_time"] * runs * (1 - 0.04 * lvl("Reactions", 0)) / 3600
    else:
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
        "mfg_hours": mfg_hours, "lab_hours": lab_hours, "te": te, "dec_price": dec_price,
        "bpc_run": bpc_run, "bpc_needed": not bpo and not item.get("t1_bp_seeded", True), "fbpc": fbpc,
        "nocopy": bool(item.get("bpc_only") and not fbpc),
        "relic": item["t1_bp_name"] if item.get("relic") else None, "relic_id": item["t1_bp_id"] if item.get("relic") else None,
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
    # originals only: -1 = a used/researched BPO, a positive number = a stack of never-used BPOs
    # (-2 = copy); keep the best-researched one of each
    owned, owned_te = {}, {}
    for b in char["blueprints"]:
        if b["quantity"] != -2:
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
        price_ids |= {v["t1_bp_id"] for v in it.get("relics", [])}
        price_ids.add(it["type_id"])
    price_ids |= {d["type_id"] for d in decs}
    pi_ids = {x["out"] for x in sde["pi"]} | {t for x in sde["pi"] for t, _, tier in x["in"] if tier >= 1}
    price_ids |= pi_ids
    product_ids = {it["type_id"] for it in items}
    faction_bps = {it["bp_id"] for it in items if it.get("bpc_only")}
    lp_offers = {}
    if faction_bps and cfg.get("use_lp_stores", False):
        try:
            lp_offers = fetch_lp_offers(set(sde["corps"]))
        except Exception as e:
            print(f"  LP stores not available right now ({e})")
        for offers in lp_offers.values():
            for o in offers:
                if o["type_id"] in faction_bps:
                    price_ids |= {r["type_id"] for r in o.get("required_items", [])}

    print(f"Fetching market orders + history for {len(price_ids)} types...")
    station_orders, pages, region_for_sale = fetch_station_orders(region, station)
    print(f"  {pages} pages of region orders")
    orders = {t: station_orders.get(t, []) for t in price_ids}
    cfg["_region_for_sale"] = region_for_sale
    # T1 blueprints you can't buy from NPCs: price their copies from contracts
    need_bpc = {it["t1_bp_id"] for it in items if not it.get("bpo") and not it.get("t1_bp_seeded", True)} | faction_bps
    cfg["_bpc_prices"] = {}
    if need_bpc:
        try:
            cfg["_bpc_prices"] = fetch_bpc_prices(region, need_bpc)
            print(f"  T1 BPC prices from contracts: {len(cfg['_bpc_prices'])} of {len(need_bpc)} blueprints offered")
        except Exception as e:  # contracts are a nice-to-have; never break the update for them
            print(f"  contracts not available right now ({e})")
    history = fetch_histories(region, price_ids)
    prices = esi_get("/markets/prices/")
    adj = {p["type_id"]: p.get("adjusted_price", 0) for p in prices}
    avg_price = {p["type_id"]: p["average_price"] for p in prices if p.get("average_price")}

    sys_id = sde["systems"].get(cfg["industry_system"].lower())
    if not sys_id:
        sys.exit(f"Unknown solar system '{cfg['industry_system']}'")
    idx = {"manufacturing": 0.0, "invention": 0.0, "copying": 0.0, "reaction": 0.0}
    react_sys = sde["systems"].get((cfg.get("reaction_system") or "").lower())
    react_all = []
    for s in esi_get("/industry/systems/"):
        for c in s["cost_indices"]:
            if c["activity"] == "reaction" and c["cost_index"] > 0:
                react_all.append(c["cost_index"])
                if s["solar_system_id"] == react_sys:
                    idx["reaction"] = c["cost_index"]
        if s["solar_system_id"] == sys_id:
            for c in s["cost_indices"]:
                if c["activity"] in idx and c["activity"] != "reaction":
                    idx[c["activity"]] = c["cost_index"]
    if not react_sys and react_all:  # no refinery system set: a typical (median) reaction index
        idx["reaction"] = sorted(react_all)[len(react_all) // 2]
    print(f"Cost indices in {cfg['industry_system']}: " + ", ".join(f"{k} {v:.2%}" for k, v in idx.items()))

    px = {t: summarize_market(o) for t, o in orders.items()}
    hist = {t: summarize_history(h, cfg["history_days"]) for t, h in history.items()}
    for t, h in hist.items():
        px.setdefault(t, {})["hist_price"] = h["avg_price"]

    cfg["_fbpc"] = faction_bpc_sources(faction_bps, lp_offers, cfg["_bpc_prices"], px, cfg, sde["corps"]) if faction_bps else {}
    if faction_bps:
        print(f"  faction / BPC-only: {len(cfg['_fbpc'])} of {len(faction_bps)} blueprints obtainable" +
              (f" ({sum(v['src'] == 'lp' for v in cfg['_fbpc'].values())} cheapest via LP store at {cfg.get('lp_isk_value', 700)} ISK/LP)"
               if cfg.get("use_lp_stores", False) else " on contracts (LP stores off)"))

    base = next((d for d in decs if d["name"].lower() == dec_mode.lower()), None)
    rows = []
    for it in items:
        if it.get("bpc_only") and it["bp_id"] not in cfg["_fbpc"] and it["cat_hint"] != "boosters":
            continue  # no copy for sale anywhere we look: can't build it
        if it.get("relics"):
            def relic_opts(v):
                x = {**it, **v}
                return [evaluate(x, None, cfg, px, adj, idx)] + [evaluate(x, d, cfg, px, adj, idx) for d in decs]
            per = {v["t1_bp_id"]: relic_opts(v) for v in it["relics"]}
            pick = max(it["relics"], key=lambda v: max(o["profit_per_attempt"] for o in per[v["t1_bp_id"]]))
            it.update(pick)  # show the chosen relic as the source
            it["relic_alts"] = [{"name": v["t1_bp_name"], "runs": v["inv_runs"], "prob": v["inv_prob"],
                                 "ppa": max(o["profit_per_attempt"] for o in per[v["t1_bp_id"]]),
                                 "relic_price": (px.get(v["t1_bp_id"], {}) or {}).get("sell")} for v in it["relics"]]
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
            "item": it, "res": res, "best": best, "hist": h, "mkt": m, "options": [] if it.get("bpo") else options,
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

    # make-or-buy: re-cost every item with the cheapest way to get each of its materials
    # two versions: industry only (building from BPOs) and industry + PI
    chains, chains_pi = Chains(sde, cfg, station_orders, adj, idx), Chains(sde, cfg, station_orders, adj, idx, use_pi=True)
    for r in rows:
        saving, make = chains.for_item(r["res"])
        saving_pi, make_pi = chains_pi.for_item(r["res"])
        r["chain"] = {"saving": saving, "make": make[:8], "saving_pi": saving_pi, "make_pi": make_pi[:8]}

    os.makedirs(OUT_DIR, exist_ok=True)
    write_csv(rows)
    write_crafting_list(items, sde["names"])
    mats = material_overview(items, decs, px, hist, sde["names"])
    cfg["_pi"] = pi_overview(sde, px, hist, pi_ids)
    try:
        cfg["_muta"] = mutaplasmid_overview(station_orders, region, cfg.get("_char_raw"))
        print(f"  mutaplasmids: {len(cfg['_muta']['mutas'])} mining mutaplasmids, {len(cfg['_muta']['mine'])} abyssal mining modules of yours")
    except Exception as e:  # fun stuff never breaks the update
        print(f"  mutaplasmids not available ({e})")
        cfg["_muta"] = None
    market = {**avg_price, **{t: p["sell"] for t, p in px.items() if p.get("sell")}}
    track = tracking.build(cfg.get("_char_raw"), cfg, {
        "names": sde["all_names"], "bp_mfg": sde["bp_mfg"], "bp_inv": sde["bp_inv"], "market": market,
        "est_cpu": {r["item"]["type_id"]: r["res"]["cost_per_unit"] for r in rows},
        "est_inv": {r["item"]["bp_id"]: r["res"]["invention_per_bpc"] for r in rows if not r["res"]["bpo"]},
        "bpc_runs": {bp: runs for inv in sde["bp_inv"].values() for bp, runs in inv["p"].items()},
        "pi": sde["pi"],
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


def pi_overview(sde, px, hist, pi_ids):
    """Planetary products: schematics + Amarr market state of every P1-P4 commodity (the page does the maths)."""
    names = sde["all_names"]
    goods = {}
    for t in pi_ids:
        p, h = px.get(t, {}), hist.get(t, {})
        goods[str(t)] = {"name": names.get(t, str(t)), "sell": p.get("sell"), "buy": p.get("buy"),
                         "units": p.get("sell_units", 0), "vol": h.get("typ_vol", 0), "avg": h.get("avg_price")}
    tiers = {}
    for x in sde["pi"]:
        tiers[str(x["out"])] = x["tier"]
        for t, _, tier in x["in"]:
            tiers[str(t)] = tier
    return {"schematics": {str(x["out"]): {"q": x["q"], "cycle": x["cycle"], "tier": x["tier"], "in": [[t, q] for t, q, _ in x["in"]]}
                           for x in sde["pi"] if x["tier"] >= 2},
            "goods": goods, "tiers": tiers}


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
            "buy_flow": h.get("buy_flow", 0), "buy_share": h.get("buy_share"),
            "by": sorted(u["by"], key=lambda b: b["name"]),
            "cats": sorted({b["cat"] for b in u["by"]}),
        })
    return out


CLIENT_ICON_DIRS = ("/icons/boosters/",)  # icon sets CCP's image server still serves in their old look


def client_icons(cfg, icon_of):
    """Copy icons the game client has newer than CCP's image server (e.g. boosters) next to the report.
    Returns {type_id: relative path}; empty if no client is installed."""
    root = cfg.get("eve_client_dir") or r"C:\CCP\EVE"
    index = os.path.join(root, "tq", "resfileindex.txt")
    if not os.path.exists(index):
        return {}
    want_icons = {}
    for i in read_jsonl("icons.jsonl"):
        f = (i.get("iconFile") or "").lower()
        if any(d in f for d in CLIENT_ICON_DIRS):
            want_icons[i["_key"]] = f
    files = {}
    wanted_paths = set(want_icons.values())
    with open(index, encoding="utf8", errors="ignore") as fh:
        for line in fh:
            path, _, rest = line.partition(",")
            if path.lower() in wanted_paths:
                files[path.lower()] = rest.split(",")[0]
    out_dir = os.path.join(HERE, "output", "icons")
    os.makedirs(out_dir, exist_ok=True)
    result = {}
    for tid, icon in icon_of.items():
        f = files.get(want_icons.get(icon, ""))
        if not f:
            continue
        src = os.path.join(root, "ResFiles", f.replace("/", os.sep))
        dst = os.path.join(out_dir, f"{tid}.png")
        if os.path.exists(src):
            if not os.path.exists(dst) or os.path.getsize(dst) != os.path.getsize(src):
                shutil.copyfile(src, dst)
            result[str(tid)] = f"icons/{tid}.png"
    if result:
        print(f"  icons: {len(result)} newer icons taken from your game client")
    return result


def write_html(rows, cfg, idx, sde, cats, mats, track=None):
    react = [r for r in rows if r["item"].get("reaction")]
    # item volumes for every product and material (haul planning: reactions run in low-sec)
    need = {m["tid"] for r in rows for m in r["res"]["materials"]} | {r["item"]["type_id"] for r in rows}
    vol, icon_of = {}, {}
    data_ids = need | {r["item"]["type_id"] for r in rows}
    for t in read_jsonl("types.jsonl"):
        if t["_key"] in data_ids:
            vol[t["_key"]] = t.get("volume") or 0
            if t.get("iconID"):
                icon_of[t["_key"]] = t["iconID"]
    local_icons = client_icons(cfg, icon_of)
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
            **{f"chain_saving{sfx}": round(r["chain"]["saving" + sfx]) for sfx in ("", "_pi")},
            **{f"chain_make{sfx}": [
                {"tid": m["tid"], "name": m["name"], "qty": round(m["qty"]), "how": m["how"], "buy": round(m["buy"], 2),
                 "cost": round(m["cost"], 2), "saving": round(m["saving"]), "sub": m["sub"][:4]} for m in r["chain"]["make" + sfx]]
               for sfx in ("", "_pi")},
            "bpo_for_sale": bool(it.get("bpo")) and it["bp_id"] in cfg.get("_region_for_sale", ()),
            "bpc_needed": res["bpc_needed"], "bpc_run": res["bpc_run"],
            **({"relic": res["relic"], "relic_id": res["relic_id"], "relic_alts": it.get("relic_alts", [])} if res.get("relic") else {}),
            **({"fbpc": res["fbpc"], "isk_lp": (res["profit"] + res["fbpc"]["lp_run"] * res["runs"] * cfg.get("lp_isk_value", 700))
                / (res["fbpc"]["lp_run"] * res["runs"]) if res["fbpc"]["src"] == "lp" and res["fbpc"]["lp_run"] else None}
               if res.get("fbpc") else {}),
            "bpc_offers": (cfg.get("_bpc_prices", {}).get(it["t1_bp_id"]) or {}).get("offers", 0) if res["bpc_needed"] else 0,
            "mfg_h": res["mfg_hours"], "lab_h": res["lab_hours"], "te": res["te"],
            "isk_h": res["profit"] / res["mfg_hours"] if res["mfg_hours"] else None,
            "isk_lab_h": res["profit"] / res["lab_hours"] if res["lab_hours"] else None,
            "trit": trit, "trit_share": trit_cost / res["material_cost"] if res["material_cost"] else None,
            "trit_isk": (revenue - other) / trit if trit else None, "name": it["name"], "group": it["group"].replace("Advanced ", ""), "t1": it["t1_bp_name"], "t1bp": it["t1_bp_id"], "bpo": res["bpo"], "reaction": bool(it.get("reaction")),
            **({"vin": sum(m["qty"] * vol.get(m["tid"], 0) for m in res["materials"]), "vout": res["units"] * vol.get(it["type_id"], 0)}
               if it.get("reaction") else {}), "me": res["me"],
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
            # every decryptor option (first = none): name, price, chance, runs, ME, TE, cost/unit, profit/BPC,
            # profit/attempt, lab h/BPC, mfg h/BPC, ROI, ROI @avg
            "decs": [[o["decryptor"], round(o["dec_price"]), round(o["chance"], 4), o["runs"], o["me"], o["te"],
                      round(o["cost_per_unit"], 2), round(o["profit"]), round(o["profit_per_attempt"]),
                      round(o["lab_hours"], 2), round(o["mfg_hours"], 2), round(o["roi"], 4),
                      None if o["roi_hist"] is None else round(o["roi_hist"], 4)] for o in r["options"]],
            "ppa": res["profit_per_attempt"], "attempt": res["attempt_cost"],
            "materials": res["materials"], "missing": res["missing"], "nomarket": res["no_market"],
            "incomplete": bool(res["missing"]), "nocopy": res["nocopy"],  # a material has no price anywhere: cost (and ROI) can't be trusted
            "mskills": [{"name": sk["name"], "level": sk["level"], "have": cfg["skills"].get("overrides", {}).get(sk["name"])} for sk in it.get("mfg_skills", [])],
            "invmats": [f"{m['quantity']}x {sde['names'].get(m['typeID'])}" for m in it["inv_materials"]],
            "invlist": [{"tid": m["typeID"], "name": sde["names"].get(m["typeID"]), "qty": m["quantity"]} for m in it["inv_materials"]],
        })
    meta = {
        "generated": dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
        "sde": sde["sde"].get("buildNumber"), "system": cfg["industry_system"], "idx": idx,
        "matmode": cfg["material_price_mode"], "prodmode": cfg["product_price_mode"],
        "tax": cfg["sales_tax"], "broker": cfg["broker_fee"], "days": cfg["history_days"],
        "maxst": cfg["max_sell_through_days"], "local_icons": local_icons, "vol": {str(k): v for k, v in vol.items() if v}, "lp_value": cfg.get("lp_isk_value", 700), "bp_containers": cfg.get("bp_containers"), "use_buy_orders": cfg.get("use_buy_orders", False), "dec": cfg.get("decryptor", "none"),
        "skills": cfg["skills"], "cats": [{"key": c, "title": CATEGORIES[c]} for c in cats],
    }
    with open(os.path.join(HERE, "report_template.html"), encoding="utf8") as f:
        tpl = f.read()
    meta["region"] = cfg.get("market_region_name", "Domain")
    meta["pi_tax"] = cfg.get("pi_tax_rate", 0.10)
    ch = cfg.get("_character")
    meta["character"] = {k: v for k, v in ch.items() if k != "skills"} if ch else None
    meta["bpo_me"], meta["bpo_runs"] = cfg.get("bpo_me", 10), cfg.get("bpo_runs", 10)
    out = tpl.replace("/*MUTA*/null", json.dumps(cfg.get("_muta"))).replace("/*PI*/null", json.dumps(cfg.get("_pi"))).replace("/*TRACK*/null", json.dumps(track)).replace("/*MATS*/null", json.dumps(mats)).replace("/*DATA*/null", json.dumps(data)).replace("/*META*/null", json.dumps(meta))
    path = os.path.join(OUT_DIR, "report.html")
    with open(path, "w", encoding="utf8") as f:
        f.write(out)
    return path


if __name__ == "__main__":
    main()
