# EVE Industry ROI

A personal industry tool for EVE Online. It finds what's worth building, invents and sells at the
**Amarr** hub (configurable), and tracks what you actually earned.

It combines CCP's static data (all blueprints) with live market data from ESI and, optionally, your own
character (skills, standings, blueprints, orders, wallet, jobs, assets) via EVE SSO.

## What's in the report

**Crafting & Blueprints** – one tab per topic, each with T1 (built from NPC-sold BPOs) and T2 (invented) items:
Missiles, Laser Crystals, Mining Crystals, Ammo & Charges, Modules, Rigs, Components, Mining & Industry,
Drones & Fighters, Ships, Mineral Builds, plus Materials & Prices and a Shopping List.

* full cost per batch: materials (priced by walking the Amarr order book), job fees from the system cost
  index, and for T2 the invention cost per successful BPC (datacores + fees ÷ your success chance)
* ROI at today's price **and** at the 30-day average, daily volume, sell-through time, market supply,
  manufacturing and lab time, ISK per slot-hour
* filters: tech level, illiquid, unprofitable, missing skills, BPO not for sale, already selling,
  already have the T2 blueprint, "can build now", numeric limits, two-level sorting
* shopping list: planned builds or automatic restocking of what your jobs consume; copy for Multibuy

**Industry** – your jobs, real production cost per product, material ledger (stock, usage, days left).

**Economy** – your orders (undercut status, relist fee, "will it fill anyway?" with an adjustable
patience slider), trades (timeline with real sales tax and broker fees per sale, real profit) and wallet.

## Setup (Windows)

1. Install Python 3.12+ and run `pip install -r requirements.txt`
2. Double-click **`run.bat`**. On the first run it downloads CCP's static data (~100 MB) and creates
   `config.json` from `config.example.json`. The report opens at `http://localhost:8766/`.
3. Edit `config.json`: your industry system, market hub, structure bonuses, fees (used until you log in).

### Optional: log in with your character

1. Create an application at <https://developers.eveonline.com>
   * Callback URL: `http://localhost:8765/callback`
   * Scopes: `esi-skills.read_skills.v1`, `esi-characters.read_standings.v1`,
     `esi-characters.read_blueprints.v1`, `esi-markets.read_character_orders.v1`,
     `esi-wallet.read_character_wallet.v1`, `esi-universe.read_structures.v1`,
     `esi-industry.read_character_jobs.v1`, `esi-assets.read_assets.v1`
2. Put the **Client ID** into `config.json` (`esi_client_id`). The secret key is not needed — never share it.
3. Run **`login.bat`** (or use "Log in again" in the report).

### Update button

`run.bat` starts a small local server (only reachable from your PC) so the report's **⟳ Update** button can
refresh prices and character data. `python install_link.py` registers an `everoi://` link so the button can
also start the updater when it isn't running (`python install_link.py --remove` to undo).

## Files

| File | |
|---|---|
| `eve_invention.py` | static data, market data, cost / ROI calculation, report generation |
| `tracking.py` | your orders, trades, jobs, wallet, real cost accounting |
| `esi_auth.py` | EVE SSO login (PKCE, no secret) and character data |
| `server.py` | local server for the Update button |
| `report_template.html` | the report page |
| `config.example.json` | settings template |

Private data (`token.json`, `config.json`, `transactions.json`, `journal.json`, `jobs.json`,
`order_events.json`) and caches stay on your PC — see `.gitignore`.
