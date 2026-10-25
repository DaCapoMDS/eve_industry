"""
Make-or-buy chains: for every material of every product, is it cheaper to make it yourself
(from an NPC-sold BPO; optionally also via PI from bought P1) than to buy it at the market hub?
Goes a few levels deep and takes the cheaper option at every step.

Reactions are left out unless "chain_reactions" is true in config.json (they need
low/null-sec refineries).
"""
import math

MAX_DEPTH = 4
PI_BASE = {0: 5, 1: 400, 2: 7200, 3: 60000, 4: 1200000}


def _mat_qty(base, runs, me, struct_bonus):
    return max(runs, math.ceil(round(runs * base * (1 - me / 100) * (1 - struct_bonus), 2)))


class Chains:
    def __init__(self, sde, cfg, station_orders, adj, idx, use_pi=False):
        self.use_pi = use_pi
        self.names = sde["all_names"]
        self.recipes = sde["bp_mfg"]
        self.makers = {}  # product -> blueprint you can buy from NPCs
        for bp, r in sde["bp_mfg"].items():
            if r.get("s") and (not r.get("r") or cfg.get("chain_reactions")):
                self.makers.setdefault(r["p"], bp)
        self.pi = {x["out"]: x for x in sde["pi"] if x["tier"] >= 2}
        self.pi_tier = {}
        for x in sde["pi"]:
            self.pi_tier[x["out"]] = x["tier"]
            for t, _, tier in x["in"]:
                self.pi_tier[t] = tier
        self.cfg, self.adj, self.idx = cfg, adj, idx
        self.mode = cfg.get("material_price_mode", "sell")
        self.prices = {}
        for t, orders in station_orders.items():
            sells = [o["price"] for o in orders if not o["is_buy_order"]]
            buys = [o["price"] for o in orders if o["is_buy_order"]]
            self.prices[t] = (min(sells) if sells else None, max(buys) if buys else None)
        self.owned_me = cfg.get("_owned_bpo", {})
        self.memo = {}

    def buy_price(self, tid):
        s, b = self.prices.get(tid, (None, None))
        return (b or s) if self.mode == "buy" else (s or b)

    def best(self, tid, depth=0, path=()):
        """Cheapest way to get one unit: {"cost", "how": buy/build/pi, "parts": [...]}"""
        if tid in self.memo:
            return self.memo[tid]
        buy = self.buy_price(tid)
        best = {"cost": buy, "how": "buy", "parts": []} if buy is not None else None
        if depth < MAX_DEPTH and tid not in path:
            made = self._build(tid, depth, path + (tid,)) or self._pi(tid, depth, path + (tid,))
            if made and made["cost"] is not None and (best is None or made["cost"] < best["cost"] * 0.995):
                best = made
        self.memo[tid] = best
        return best

    def _build(self, tid, depth, path):
        bp = self.makers.get(tid)
        if not bp:
            return None
        r = self.recipes[bp]
        runs = 10
        me = 0 if r.get("r") else self.owned_me.get(bp, self.cfg.get("bpo_me", 10))
        cost, parts = 0.0, []
        for m, q in r["m"]:
            sub = self.best(m, depth + 1, path)
            if not sub or sub["cost"] is None:
                return None
            per = _mat_qty(q, runs, me, self.cfg.get("structure_me_bonus", 0.0)) / (runs * r["q"])
            cost += per * sub["cost"]
            if sub["how"] != "buy":
                parts.append({"tid": m, "qty": per, "how": sub["how"], "cost": sub["cost"], "parts": sub["parts"]})
        eiv = sum(self.adj.get(m, 0) * q for m, q in r["m"])
        job_rate = self.idx.get("manufacturing", 0) + self.cfg.get("facility_tax", 0) + self.cfg.get("scc_surcharge", 0)
        cost += eiv * job_rate / r["q"]
        return {"cost": cost, "how": "build", "parts": parts, "bp": bp}

    def _pi(self, tid, depth, path):
        x = self.pi.get(tid) if self.use_pi else None
        if not x:
            return None
        rate = self.cfg.get("pi_tax_rate", 0.10)
        cost, parts = PI_BASE[x["tier"]] * rate, []  # export the product
        for t, q, tier in x["in"]:
            if tier >= 2:
                sub = self.best(t, depth + 1, path)
            else:
                p = self.buy_price(t)
                sub = {"cost": p + PI_BASE[tier] * rate * 0.5, "how": "buy", "parts": []} if p is not None else None
            if not sub or sub["cost"] is None:
                return None
            cost += sub["cost"] * q / x["q"]
            if sub["how"] != "buy":
                parts.append({"tid": t, "qty": q / x["q"], "how": sub["how"], "cost": sub["cost"], "parts": sub["parts"]})
        return {"cost": cost, "how": "pi", "parts": parts}

    def for_item(self, res):
        """Re-cost a report item's batch with the best chain for each of its materials."""
        saving, make = 0.0, []
        for m in res["materials"]:
            b = self.best(m["tid"])
            if not b or b["how"] == "buy" or b["cost"] is None:
                continue
            s = (m["unit"] - b["cost"]) * m["qty"]
            if s <= 0:
                continue
            saving += s
            make.append({"tid": m["tid"], "name": self.names.get(m["tid"], str(m["tid"])), "qty": m["qty"],
                         "how": b["how"], "buy": m["unit"], "cost": b["cost"], "saving": s,
                         "sub": [self.names.get(p["tid"], str(p["tid"])) for p in b["parts"]]})
        make.sort(key=lambda x: -x["saving"])
        return saving, make
