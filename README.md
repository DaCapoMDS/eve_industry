# EVE Industry ROI

**English** · [Deutsch](README.de.md)

A personal industry tool for EVE Online that runs on your own PC. It works out what is worth building
or inventing and selling at one NPC trade hub (Amarr by default), plans your restocking, and tracks what
you actually earned.

It combines CCP's static game data (SDE: every blueprint, recipe and decryptor) with live market data
from ESI (orders, price history, system cost indices, contracts, LP stores). If you log in with EVE SSO,
it also reads your character: skills, standings, blueprints, market orders, wallet, industry jobs, assets
and planets. Everything ends up in one HTML page, `output/report.html`.

## The report at a glance

The report has four sections, each with its own tabs:

**Crafting & Blueprints**: what to build
- **All** plus **one tab per category** (Missiles, Modules, Ships, Reactions …, see [Categories](#categories)).
  Each tab lists T1 items (built from a BPO) and T2 items (invented), with full cost, profit, ROI at today's
  price and at the 30-day average, volume, sell-through time, market supply and ISK per slot hour.
- **Materials & Prices**: every material, datacore and decryptor with its hub price and which items use it.
- **Decryptors**: for each T2 item, all decryptors compared with inventing without one.
- **🪐 Planetary (PI)**: profit of every P2–P4 schematic, including customs tax.

**Industry**: what you are running *(needs login)*
- **📅 Planner**: what to build next, based on what you sold, your stock and your slots.
- **📘 Blueprints**: every BPO and BPC you own, what to do with each one, and which container it belongs in.
- **Jobs**: running, ready and past industry jobs, job fees, your real invention success rate.
- **Production**: the real cost per unit of what you built, compared with the tool's estimate.
- **Material Ledger**: what you paid on average for each material, stock, usage, days left.
- **🪐 My Planets**: your PI colonies: extractors, factories, exports, taxes.

**Economy**: your money
- **My Orders** *(login)*: undercut/outbid status, relist fee, "will it fill anyway?" advice.
- **Trades** *(login)*: real profit and loss per item, using the sales tax and broker fees you actually paid.
- **🔄 Restock** *(login)*: buy back what your jobs consume, optionally with buy orders.
- **🧪 Craft Prep** *(login)*: everything needed to build the blueprints sitting in one container.
- **🛒 Shopping List**: materials for the batches you picked with ＋, ready to copy into Multibuy.
- **Wallet** *(login)*: balance, money in and out by type, daily cash flow.
- **📈 Growth** *(login)*: wallet, assets and skill points over time, money per month, slot usage.
- **🎯 Campaign**: is handing items in to the campaign Item Trader worth more than selling them? (from `campaign.json`)
- **🏛 LP Store**: every offer of the LP stores listed in `campaign.json`, ranked by ISK/LP.

**🧪 Fun**
- **🧬 Mutaplasmids**: mining mutaplasmids: what each one can roll, hub prices, your abyssal mining modules.

Without logging in, the crafting tabs, PI, Shopping List, Campaign, LP Store and Mutaplasmids still work.
They use the skills and fees from `config.json`.

## Requirements

- **Windows** is the main platform. The tool is plain Python, but `run.bat`, `login.bat` and the
  `everoi://` link (`install_link.py`, which uses the Windows registry) are Windows-only.
- **Python 3.12** (64-bit). `run.bat` and `login.bat` expect the default per-user install from python.org:
  `%LOCALAPPDATA%\Programs\Python\Python312\python.exe`. If yours is somewhere else, see
  [Troubleshooting](#troubleshooting).
- One package: **requests** (listed in `requirements.txt`). Everything else is the Python standard library.
- About 100 MB to download the SDE on the first run, plus an internet connection on every update (ESI).

## Installation

```powershell
cd C:\path\to\eve-invention
python -m pip install -r requirements.txt
# or, using the Python launcher:
py -3.12 -m pip install -r requirements.txt
```

## First-time setup

### 1. Create your config

```powershell
Copy-Item config.example.json config.json
```

(If you skip this, the first run copies it for you.) Open `config.json` in a text editor and set at least
your market hub and industry system (see [Important settings](#important-settings)). Percentages are
fractions: `0.036` = 3.6 %.

### 2. Create an EVE developer application (optional, for character data)

You need your own application so the tool can log in as your character. It is free:

1. Go to <https://developers.eveonline.com>, log in, and create a new application.
2. Choose the type that allows **Authentication & API Access**.
3. **Callback URL:** `http://localhost:8765/callback`
   (it must match `esi_callback` in `config.json` exactly).
4. **Scopes:** add all nine of these. The tool requests all of them at login, and EVE SSO rejects
   scopes that your application doesn't have:
   ```
   esi-skills.read_skills.v1
   esi-characters.read_standings.v1
   esi-characters.read_blueprints.v1
   esi-markets.read_character_orders.v1
   esi-wallet.read_character_wallet.v1
   esi-universe.read_structures.v1
   esi-industry.read_character_jobs.v1
   esi-assets.read_assets.v1
   esi-planets.manage_planets.v1
   ```
   `esi-planets.manage_planets.v1` is only used to **read** your colonies. It is EVE's only scope for PI data.
5. Copy the application's **Client ID** into `config.json`:
   ```json
   "esi_client_id": "your-client-id-here",
   ```
   The login uses PKCE, so the **Secret Key is not needed**. Don't put it anywhere and never share it.

### 3. Log in

Double-click **`login.bat`**, or run:

```powershell
python eve_invention.py --login
```

Your browser opens the EVE login page. Pick your character and accept the permissions. The tool waits on
`localhost:8765` for the redirect, saves the login in `token.json`, and then builds the report.
`login.bat` opens `output\report.html` when it is done.

The login is refreshed automatically after that. To log out, delete `token.json`.

### Important settings

All keys are in `config.example.json`, and most have a `_comment` next to them.

| Setting | What it does |
|---|---|
| `market_region_id`, `market_station_id` | Where you buy and sell. Must be an **NPC station** (player-structure markets aren't read). Default: Domain / Amarr VIII (Oris) – Emperor Family Academy. |
| `market_region_name` | Region name shown in the report (display only). |
| `industry_system` | Solar system name (as spelled in game) where you invent and build. Its cost indices set the job fees. |
| `esi_client_id`, `esi_callback` | Your application from step 2. |
| `skills` | Science/encryption skill levels used **only when not logged in**, with per-skill `overrides`. After login, your real skills are used. |
| `sales_tax`, `broker_fee` | Used only when not logged in. After login they are calculated from Accounting, Broker Relations and your standings toward the station owner. |
| `hub_fee_discount` | Temporary sales tax + broker fee discount at one station (`station_id`, `rate`, `until`). The example contains a dated discount for Amarr, so check it in game and remove or change it when it ends. |
| `facility_tax`, `scc_surcharge` | Job tax at your facility and the SCC surcharge. |
| `structure_me_bonus`, `structure_te_bonus`, `structure_job_cost_bonus` | Bonuses of an Upwell structure with rigs (e.g. `0.02` for a T1 ME rig in hisec). Use `0` for an NPC station. |
| `bpo_me`, `bpo_te`, `bpo_runs` | Assumed research level and runs per job for BPOs you **don't** own. When logged in, the real ME/TE of your own BPOs is used. |
| `decryptor` | `"none"`, `"best"` (most profit per invention attempt, per item), or a decryptor name such as `"Accelerant Decryptor"`. |
| `material_price_mode` | `"sell"` = buy materials from sell orders. `"buy"` = place buy orders. |
| `product_price_mode` | `"sell"` = list sell orders (pays broker fee). `"buy"` = sell instantly into buy orders. |
| `categories` | Which category tabs to build (see below). Fewer categories make the update faster. |
| `history_days`, `max_sell_through_days` | Days of price history for averages. Items that take longer than `max_sell_through_days` to sell one batch are flagged as illiquid. |
| `relist_patience_days` | My Orders: wait instead of paying the relist fee if the order fills within this many days anyway. |
| `reaction_system`, `reaction_me_bonus`, `reaction_tax` | Reactions tab: refinery system (empty = a typical reaction cost index), its rig material bonus and owner tax. |
| `use_buy_orders` | Restock tab: also plan buy orders, not only buying from sell orders. |
| `use_lp_stores`, `lp_isk_value` | Faction & BPC-only tab: also price blueprint copies from NPC LP stores, valuing 1 LP at `lp_isk_value` ISK. |
| `pi_tax_rate` | Customs office tax for PI (can also be changed in the PI tab). |
| `bp_containers` | Optional names of your blueprint containers, by role (Blueprints and Craft Prep tabs). See [Blueprints](#blueprints). |

Optional keys that are read by the code but missing from the example: `faction_bpc_me` / `faction_bpc_te`
(research level assumed for bought faction copies, default 0) and `eve_client_dir` (game client folder used
to copy newer icons, default `C:\CCP\EVE`).

> The page header and some labels always say "Amarr", even if you set a different hub. The numbers
> still come from the station you configured.

#### Categories

Valid keys for `categories` (leave the list empty to get all of them):

| Key | Tab | Key | Tab |
|---|---|---|---|
| `missiles` | Missiles, Rockets & Torpedoes | `deployables` | Deployables |
| `laser_crystals` | Laser Frequency Crystals | `structures` | Structures & Sov |
| `mining_crystals` | Mining Crystals | `structure_mods` | Structure Modules & Rigs |
| `other_ammo` | Ammo & Charges | `fuel` | Fuel Blocks |
| `modules` | Modules | `faction` | Faction & BPC-only |
| `rigs` | Rigs | `boosters` | Boosters |
| `components` | Components | `t3` | T3 (Reverse Engineering) |
| `mining` | Mining & Industry | `reactions` | Reactions (low/null-sec) |
| `drones` | Drones & Fighters | `glorified` | Glorified Mutaplasmids |
| `ships` | Ships | `mineral_builds` | Mineral Builds (other T1 items that need only minerals) |

## Running it

### The normal way: `run.bat`

Double-click **`run.bat`**. It starts a small local web server (`server.py`, which listens only on
`127.0.0.1`). The server runs an update right away and opens **<http://localhost:8766/>** in your browser.
While the first report is being built, the page says so and reloads by itself. Keep the window open while
you use the report, and close it to stop the server.

Without the batch file:

```powershell
python server.py               # same as run.bat
python server.py --no-browser  # don't open a browser tab
```

### The ⟳ Update button

The top right of the report shows when prices and character data were last fetched, and an **⟳ Update**
button. Clicking it runs a full update in the background, shows its progress, and reloads the page on the
same tab when it's done. If the login has expired, a **Log in again** button appears next to it.

The button works while `run.bat` / `server.py` is running. This also applies when you opened
`output\report.html` directly from disk. If you want the button to start the server when it isn't running,
register the `everoi://` link once:

```powershell
python install_link.py           # install (run it again after moving the folder)
python install_link.py --remove  # undo
```

This only writes to `HKEY_CURRENT_USER\Software\Classes\everoi`, so no admin rights are needed. Your browser
will ask once whether it may open "EVE Industry ROI". A server started this way runs without a window and
stops itself after 3 hours without the page talking to it.

### Command line only

You can also build the report without the server:

```powershell
python eve_invention.py                 # update with config.json
python eve_invention.py --login         # log in first (opens the browser)
python eve_invention.py --update-sde    # re-download CCP's static data first (after a game patch)
python eve_invention.py --config other.json   # use a different config file
```

Then open `output\report.html`. The console also prints the top 20 items per category.

### Output files

| File | Content |
|---|---|
| `output/report.html` | The report (one self-contained page). |
| `output/roi.csv` | All evaluated items with cost, profit, ROI, volume (semicolon-separated). |
| `output/crafting_list.csv` | Recipes: invention inputs, skills, manufacturing materials (semicolon-separated). |
| `output/icons/` | Newer icons copied from your game client, if it is installed. |

### What refreshes when

| Data | Refreshed |
|---|---|
| Market orders at your station, adjusted prices, system cost indices | every update |
| Character data (skills, orders, wallet, jobs, assets, planets …) | every update, if logged in |
| Price history (`history_cache.json`) | once per day per item (ESI only updates it daily) |
| Blueprint contracts (`contracts_cache.json`) | new contracts are read on every update. Their contents are cached, and finished contracts are dropped. |
| All NPC LP stores for the Faction tab (`lp_cache.json`) | every 7 days, only with `use_lp_stores: true` |
| Campaign LP stores (`campaign_lp_cache.json`) | once per day |
| Campaign snapshots (`campaign_history.json`) | one per day, last 120 kept |
| Static data (`sde/`, `sde_cache.json`) | downloaded on the first run. After that only with `--update-sde`, or when a new tool version needs a rebuild of the cache. |

To force a refresh, delete the cache file. It is rebuilt on the next update.

Your wallet transactions, journal, jobs and orders are only available from ESI for a limited time
(transactions and journal about 30 days, jobs 90 days). The tool merges them into local files, so your
history keeps growing **as long as you run an update at least every few weeks**.

Filters, sorting, limits, the shopping list and tab choices are saved in your browser (localStorage), not in
files.

## Using the tabs

### Crafting ROI and invention (category tabs)

- The **cards** at the top show the best picks for the current tab. The table below has one row per item.
  Click a row for the cost breakdown: materials, invention, job fees, decryptor options, and a make-or-buy
  chain (⛓) for parts you could build yourself.
- **Profit/batch** = one successful invention turned into a full BPC, manufactured and sold (T2), or one
  BPO job (T1). Failures, materials, job fees, sales tax and broker fee are all included.
  **ROI @avg** (at the 30-day average price) is the conservative number to trust.
- Materials are priced by walking the order book for the quantity you need, not just the top order.
- **Hide** filters: illiquid, unprofitable, BPO not for sale, and (with login) missing skills, already
  selling, have T2 blueprint, in production, mats short, in stock.
  **Show only** (with login): can build now, can invent now, missing (gaps in your line-up), things I use.
- **Limits**: min ROI, max sell-through days, max market supply, min volume, min ISK/h, min slot hours,
  plus a two-level sort. "🔨 my builds as material cost" re-costs parts you can build yourself at your
  own cost.
- "What do the columns mean?" below the filters explains every column.
- **Decryptors** shows, per T2 item, whether a decryptor beats inventing without one.
  **Materials & Prices** lists every input. Click one to see what uses it.

### Restock, Planner, Craft Prep, Shopping List

- **🛒 Shopping List**: click **＋** in front of any item in the crafting tabs to add a batch (click again
  for more). The list totals materials, datacores and the stock you already have, and gives a list you can
  paste into the in-game **Multibuy**.
- **🔄 Restock**: built from what your manufacturing and invention jobs consumed over up to the last 30
  days. Choose "Keep stock for N days" or "Replace what I used". With `use_buy_orders: true` it also shows
  what a Buy Order one tick above the best bid would save and whether it would fill. A red dot on the tab
  means inputs are running out.
- **📅 Planner**: for everything you sold in the last 30 days, it works out how long your stock (hangar +
  listed + in jobs) lasts and when you need to start the next batch, taking invention, build time and
  hauling into account. Items have to pass your minimum ROI, ISK per slot hour and profit per day. Choose
  between "What I sell" and "All my blueprints".
- **🧪 Craft Prep**: takes the idle blueprints in your `bpc_missiles` container and lists what to craft
  first, which reactions to run, what to haul to the refinery, which raw materials to buy (Multibuy lists)
  and the science stock you need.

### Tracking and P&L

All of these need login.

- **My Orders**: every Sell Order and Buy Order with its status (undercut/outbid), units ahead of you, how
  long it would take to fill anyway, and the relist fee. The patience slider (default
  `relist_patience_days`) decides when the advice is "wait" instead of "update". A red dot on the tab
  means an order is undercut.
- **Trades**: real profit per item and per sale. It uses the actual sales tax and broker fees from your
  wallet journal, and your real production cost. It also shows profit/day, your market share, how long
  your stock lasts, and how often you were sold out.
- **Production / Material Ledger / Jobs**: real cost per unit from your finished jobs (materials at your
  average purchase price + real job fee + real invention cost per BPC), compared with the report's
  estimate. Plus stock and usage per material, and your job list. A green dot means jobs are ready to
  deliver.
- **Wallet / Growth**: cash flow by transaction type, wallet history, assets, SP over time, and how many
  manufacturing/science/reaction slots you use compared with what your skills allow.
- **My Planets**: per colony: extraction per day, extractor time left (red dot when one ends within
  24 h), factory output, exports and imports, customs tax.

### Blueprints

The **📘 Blueprints** tab lists every BPO, T2 copy, T1 copy and faction copy you own, with location, ME/TE,
runs and a "what to do" suggestion (in a job, unprofitable, build now, research, wait, free to build).

It also suggests a **container system**: one container per role in your build station's hangar, named in
game with *Set Name*. By default only the number at the start of the name matters:

| No. | Role (`bp_containers` key) | Contents |
|---|---|---|
| 1 | `build` | idle blueprints whose item is running out: today's build list |
| 2 | `invent` | T1 copies for inventing missiles, ammo and crystals |
| 3 | `bpc_missiles` | invented copies: missiles, ammo, crystals (also used by Craft Prep) |
| 4 | `bpc_other` | every other copy worth building |
| 5 | `bpo_ammo` | BPOs: ammo, missiles, laser and mining crystals |
| 6 | `bpo_modules` | T1 gear: modules, rigs, ships, drones, mining equipment |
| 7 | `bpo_misc` | parts and inputs: components, R.A.M., fuel blocks … |
| 9 | `dead` | copies that lose money or have no market |

To use your own names, add them to `config.json`, for example:

```json
"bp_containers": {"build": "1 BUILD NOW", "invent": "2 INVENT", "bpc_missiles": "3 BPC MISSILES",
                  "bpc_other": "4 BPC OTHER", "bpo_ammo": "5 BPO AMMO", "bpo_modules": "6 BPO GEAR",
                  "bpo_misc": "7 BPO PARTS", "dead": "9 DEAD"}
```

After the next update, the tab lists which blueprint to move where.

### Campaign and LP Store (`campaign.json`)

`campaign.json` describes an in-game campaign with an Item Trader. The included file is for
"Into the Light (Amarr)". The **🎯 Campaign** tab compares, for each objective item, **handing it in**
(ISK + LP + a share of a trader token) with **selling it**. It also shows what a turn-in gives, the best
LP offers to spend the LP on, token rewards, and price charts. The **🏛 LP Store** tab lists every offer of
the stores in `lp_stores` with ISK/LP via Sell Order and instant, offers sold per day, and required items.
Blueprint copies are valued as the item you would build from them at ME0. You can filter by name, minimum
volume, minimum ISK/LP or "no required items", and click an offer for its cost breakdown.

Both tabs only use market data, so they work without login. Edit `campaign.json` with numbers from the
in-game objective window:

| Field | Meaning |
|---|---|
| `campaign`, `ends`, `trader` | Name, end date (`YYYY-MM-DD`) and trader location (display). |
| `turnin_tax`, `turnin_tax_lp` | Tax taken from the objective's ISK and LP payout (`0.05` = 5 %). `turnin_tax_lp: true` = same as the ISK tax. |
| `lp_value` | ISK per LP for hand-ins. The market's ISK/LP is used if it is lower. |
| `token` | `type_id` and `name` of the trader token. |
| `lp_stores` | Corporation IDs of the LP stores shown in the LP Store tab. |
| `objectives[]` | `type_id`, `name`, `isk` and `lp` **per contribution** (1 contribution = 1 item delivered), `lp_corp` (whose LP you get), `per_token` (items per token, `null` if unknown), `how` (note). Set `isk`/`lp` to `null` while you don't know them. |
| `rewards[]` | Trader rewards: `type_id`, `name`, `tokens` (cost in tokens). |
| `also_watch[]` | Other items whose prices you want to see: `type_id`, `name`, `note`. |

Items are given by type ID because a new campaign's items may not be in the local SDE yet. If you only want
the LP Store tab, keep `token` and set `objectives` to `[]`. Delete or rename `campaign.json` to hide both
tabs.

## Privacy: files you must never share

These files contain your login or your personal game data. **Never commit, upload, or send them.** They are
all listed in `.gitignore`.

| File | Contains |
|---|---|
| `token.json` | your EVE login (refresh token): anyone with it can read your character's data |
| `config.json` | your settings and application Client ID |
| `transactions.json` | your wallet transactions |
| `journal.json` | your wallet journal |
| `jobs.json` | your industry jobs |
| `order_events.json` | every state of your market orders |
| `sell_orders.json` | your sell order history |
| `growth.json` | daily snapshots of your wallet, assets and skill points |

Also be careful with **`output/report.html`**. When you are logged in, it contains your character name,
orders, wallet and assets, so don't post it publicly.

Everything else that isn't in git is generated and can be deleted at any time: `sde/`, `sde_cache.json`,
`history_cache.json`, `contracts_cache.json`, `lp_cache.json`, `campaign_lp_cache.json`,
`campaign_history.json`, `output/`, `__pycache__/`. Deleting the private tracking files above loses
history that ESI no longer has.

To revoke the tool's access completely, delete `token.json` and remove the application from your EVE
account's list of authorized third-party applications.

## Troubleshooting

**"EVE login expired - run login.bat again"**
The saved login couldn't be refreshed. Run `login.bat` (or click **Log in again** in the report). Until
then, the skills and fees from `config.json` are used and the tracking tabs stay empty.

**"Some tracking needs new permissions (…) - run login.bat once more"** / a tab says *Not available yet*
Your login is missing scopes. Make sure your application has all nine scopes listed above, then log in again.

**"Put your application's Client ID into config.json (esi_client_id) first."**
`--login` was used without a Client ID. Do step 2 of the setup.

**The login page shows an error, or the tool keeps waiting after login**
The callback URL of your application must be exactly `http://localhost:8765/callback`, the same as
`esi_callback`, and port 8765 must be free.

**`run.bat` / `login.bat` say Python can't be found**
They call `%LOCALAPPDATA%\Programs\Python\Python312\python.exe`. Either install Python 3.12 there (the
python.org default "for me only") or skip the batch files and run `python server.py` / `python eve_invention.py --login`.

**The Update button says "Could not start the updater"**
The server isn't running and the `everoi://` link isn't installed or wasn't allowed. Start `run.bat`, or run
`python install_link.py` and allow the browser prompt.

**Crash on the first run: `FileNotFoundError` for `sde\icons.jsonl`**
This happens when the EVE client is installed at `C:\CCP\EVE` (or `eve_client_dir`). The tool then tries to
copy newer icons, but the automatic SDE download doesn't extract `icons.jsonl`. Either extract `icons.jsonl`
from the SDE zip (<https://developers.eveonline.com/static-data/eve-online-static-data-latest-jsonl.zip>)
into the `sde\` folder, or set `"eve_client_dir"` in `config.json` to a folder without the game client.

**New items from the latest patch are missing**
Run `python eve_invention.py --update-sde` to download the newest SDE (about 100 MB, then about 30 s of parsing).

**"Unknown solar system '…'"**, **"Unknown category …"**, **"Unknown decryptor '…'"**
Check the spelling in `config.json`. The error message lists the valid options for categories and decryptors.

**"ESI kept refusing … - try the update again in a minute"**
ESI was rate-limiting or down. The tool already retries and backs off, so just try again later.
If contracts, LP stores, mutaplasmids or the campaign fail, the update continues without them.

**The Industry / Economy tabs show "–"**
You aren't logged in, or the login lacks the wallet/industry scopes.

**My filters or shopping list are gone**
They are stored per browser and per address. `http://localhost:8766/` and the file `output\report.html`
keep separate settings.

## Files

| File | Purpose |
|---|---|
| `eve_invention.py` | main script: static data, market data, cost and ROI calculation, report generation |
| `tracking.py` | your orders, trades, jobs, wallet, planets, real cost accounting |
| `campaign.py` / `campaign.json` | Campaign and LP Store tabs |
| `chains.py` | make-or-buy chains (building parts yourself vs buying them) |
| `esi_auth.py` | EVE SSO login (PKCE, no secret) and character data |
| `server.py` | local server for the report and the Update button |
| `install_link.py` | registers the `everoi://` link for the Update button |
| `report_template.html` | the report page |
| `config.example.json` | settings template |
| `run.bat`, `login.bat` | Windows shortcuts |
