# EVE Industry ROI

[English](README.md) · **Deutsch**

Ein persönliches Industrie-Tool für EVE Online, das lokal auf deinem PC läuft. Es rechnet aus, was sich zu
bauen oder per Invention herzustellen und an einem NPC-Handelshub (standardmäßig Amarr) zu verkaufen lohnt,
plant deinen Nachschub und zeigt dir, was du tatsächlich verdient hast.

Dafür kombiniert es CCPs statische Spieldaten (SDE: alle Blueprints, Rezepte und Decryptoren) mit
Live-Marktdaten aus ESI (Orders, Preishistorie, System-Cost-Index, Contracts, LP Stores). Wenn du dich per
EVE SSO einloggst, liest es zusätzlich deinen Charakter aus: Skills, Standings, Blueprints, Market Orders,
Wallet, Industry Jobs, Assets und Planeten. Alles landet in einer HTML-Seite: `output/report.html`.

## Der Report im Überblick

Der Report hat vier Bereiche mit jeweils eigenen Tabs:

**Crafting & Blueprints**: was sich zu bauen lohnt
- **All** plus **ein Tab pro Kategorie** (Missiles, Modules, Ships, Reactions …, siehe [Kategorien](#kategorien)).
  Jeder Tab zeigt T1-Items (aus einer BPO gebaut) und T2-Items (per Invention), jeweils mit Gesamtkosten,
  Profit, ROI zum aktuellen Preis und zum 30-Tage-Schnitt, Volumen, Abverkaufszeit, Marktangebot und ISK
  pro Slot-Stunde.
- **Materials & Prices**: jedes Material, jeder Datacore und jeder Decryptor mit Hub-Preis und den Items,
  die es brauchen.
- **Decryptors**: pro T2-Item alle Decryptoren im Vergleich zur Invention ohne Decryptor.
- **🪐 Planetary (PI)**: Profit jedes P2–P4-Schemas, inklusive Customs-Steuer.

**Industry**: was bei dir läuft *(Login nötig)*
- **📅 Planner**: was du als Nächstes bauen solltest, basierend auf Verkäufen, Bestand und freien Slots.
- **📘 Blueprints**: alle deine BPOs und BPCs, was mit jeder zu tun ist und in welchen Container sie gehört.
- **Jobs**: laufende, fertige und vergangene Industry Jobs, Job-Gebühren, deine echte Invention-Erfolgsquote.
- **Production**: die echten Stückkosten deiner Produktion im Vergleich zur Schätzung des Tools.
- **Material Ledger**: was du pro Material im Schnitt bezahlt hast, Bestand, Verbrauch, Reichweite in Tagen.
- **🪐 My Planets**: deine PI-Kolonien mit Extraktoren, Fabriken, Export und Steuern.

**Economy**: dein Geld
- **My Orders** *(Login)*: ob du undercut/outbid bist, die Relist-Gebühr und ob sich die Order „ohnehin füllt“.
- **Trades** *(Login)*: echter Gewinn/Verlust pro Item, mit der tatsächlich gezahlten Sales Tax und Broker Fee.
- **🔄 Restock** *(Login)*: nachkaufen, was deine Jobs verbrauchen, auf Wunsch auch über Buy Orders.
- **🧪 Craft Prep** *(Login)*: alles, was du für die Blueprints in einem bestimmten Container brauchst.
- **🛒 Shopping List**: Materialien für die mit ＋ ausgewählten Batches, fertig zum Einfügen in Multibuy.
- **Wallet** *(Login)*: Kontostand, Einnahmen und Ausgaben nach Art, täglicher Cashflow.
- **📈 Growth** *(Login)*: Wallet, Assets und Skillpunkte im Zeitverlauf, Geld pro Monat, Slot-Auslastung.
- **🎯 Campaign**: Lohnt sich die Abgabe beim Campaign Item Trader mehr als der Verkauf? (aus `campaign.json`)
- **🏛 LP Store**: alle Angebote der in `campaign.json` eingetragenen LP Stores, sortiert nach ISK/LP.

**🧪 Fun**
- **🧬 Mutaplasmids**: Mining-Mutaplasmide: was jedes rollen kann, Hub-Preise, deine abyssalen Mining-Module.

Ohne Login funktionieren weiterhin die Crafting-Tabs, PI, Shopping List, Campaign, LP Store und
Mutaplasmids. Dabei werden die Skills und Gebühren aus `config.json` verwendet.

## Voraussetzungen

- **Windows** ist die Hauptplattform. Das Tool ist normales Python, aber `run.bat`, `login.bat` und der
  `everoi://`-Link (`install_link.py`, nutzt die Windows-Registry) gibt es nur unter Windows.
- **Python 3.12** (64 Bit). `run.bat` und `login.bat` erwarten die Standard-Installation von python.org
  „nur für mich“: `%LOCALAPPDATA%\Programs\Python\Python312\python.exe`. Liegt dein Python woanders, siehe
  [Fehlerbehebung](#fehlerbehebung).
- Ein Paket: **requests** (steht in `requirements.txt`). Alles andere ist Python-Standardbibliothek.
- Beim ersten Start etwa 100 MB Download für die SDE, danach bei jedem Update Internet für ESI.

## Installation

```powershell
cd C:\pfad\zu\eve-invention
python -m pip install -r requirements.txt
# oder mit dem Python-Launcher:
py -3.12 -m pip install -r requirements.txt
```

## Ersteinrichtung

### 1. Config anlegen

```powershell
Copy-Item config.example.json config.json
```

(Wenn du das überspringst, legt der erste Start die Datei automatisch an.) Öffne `config.json` in einem
Texteditor und trage mindestens deinen Markt-Hub und dein Industriesystem ein (siehe
[Wichtige Einstellungen](#wichtige-einstellungen)). Prozentwerte sind Dezimalzahlen: `0.036` = 3,6 %.

### 2. EVE-Developer-Anwendung erstellen (optional, für Charakterdaten)

Damit sich das Tool als dein Charakter einloggen kann, brauchst du eine eigene Anwendung. Das ist kostenlos:

1. Auf <https://developers.eveonline.com> einloggen und eine neue Anwendung anlegen.
2. Den Typ mit **Authentication & API Access** wählen.
3. **Callback URL:** `http://localhost:8765/callback`
   (muss exakt mit `esi_callback` in `config.json` übereinstimmen).
4. **Scopes:** alle neun eintragen. Das Tool fordert beim Login alle an, und EVE SSO lehnt Scopes ab, die
   deine Anwendung nicht hat:
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
   `esi-planets.manage_planets.v1` wird nur zum **Lesen** deiner Kolonien verwendet. EVE hat für PI-Daten
   keinen anderen Scope.
5. Die **Client ID** der Anwendung in `config.json` eintragen:
   ```json
   "esi_client_id": "deine-client-id",
   ```
   Der Login nutzt PKCE, deshalb wird der **Secret Key nicht gebraucht**. Trag ihn nirgends ein und gib ihn
   niemals weiter.

### 3. Einloggen

**`login.bat`** doppelklicken oder ausführen:

```powershell
python eve_invention.py --login
```

Dein Browser öffnet die EVE-Loginseite. Charakter wählen und die Berechtigungen bestätigen. Das Tool wartet
auf `localhost:8765` auf die Weiterleitung, speichert den Login in `token.json` und baut dann den Report.
`login.bat` öffnet danach `output\report.html`.

Der Login wird ab dann automatisch erneuert. Zum Ausloggen `token.json` löschen.

### Wichtige Einstellungen

Alle Schlüssel stehen in `config.example.json`, die meisten mit einem `_comment` daneben.

| Einstellung | Bedeutung |
|---|---|
| `market_region_id`, `market_station_id` | Wo du kaufst und verkaufst. Muss eine **NPC-Station** sein, Märkte in Spielerstrukturen werden nicht gelesen. Standard: Domain / Amarr VIII (Oris) – Emperor Family Academy. |
| `market_region_name` | Regionsname, der im Report angezeigt wird (nur Anzeige). |
| `industry_system` | Name des Sonnensystems (wie im Spiel geschrieben), in dem du inventest und baust. Dessen Cost Index bestimmt die Job-Gebühren. |
| `esi_client_id`, `esi_callback` | Deine Anwendung aus Schritt 2. |
| `skills` | Science- und Encryption-Skilllevel, die **nur ohne Login** gelten, mit `overrides` pro Skill. Mit Login werden deine echten Skills verwendet. |
| `sales_tax`, `broker_fee` | Gelten nur ohne Login. Mit Login werden sie aus Accounting, Broker Relations und deinen Standings zum Stationsbesitzer berechnet. |
| `hub_fee_discount` | Befristeter Rabatt auf Sales Tax und Broker Fee an einer Station (`station_id`, `rate`, `until`). Das Beispiel enthält einen datierten Rabatt für Amarr. Im Spiel prüfen und entfernen oder anpassen, wenn er ausläuft. |
| `facility_tax`, `scc_surcharge` | Job-Steuer der Anlage und SCC-Zuschlag. |
| `structure_me_bonus`, `structure_te_bonus`, `structure_job_cost_bonus` | Boni einer Upwell-Struktur mit Rigs (z. B. `0.02` für ein T1-ME-Rig im Hisec). Für eine NPC-Station `0`. |
| `bpo_me`, `bpo_te`, `bpo_runs` | Angenommene Forschung und Runs pro Job für BPOs, die du **nicht** besitzt. Mit Login gelten bei eigenen BPOs deren echte ME/TE. |
| `decryptor` | `"none"`, `"best"` (höchster Profit pro Invention-Versuch, pro Item) oder ein Name wie `"Accelerant Decryptor"`. |
| `material_price_mode` | `"sell"` = Material aus Sell Orders kaufen. `"buy"` = eigene Buy Orders stellen. |
| `product_price_mode` | `"sell"` = eigene Sell Orders (kostet Broker Fee). `"buy"` = sofort in Buy Orders verkaufen. |
| `categories` | Welche Kategorie-Tabs gebaut werden (siehe unten). Weniger Kategorien machen das Update schneller. |
| `history_days`, `max_sell_through_days` | Tage Preishistorie für Durchschnitte. Items, deren Batch länger als `max_sell_through_days` zum Abverkauf braucht, gelten als illiquide. |
| `relist_patience_days` | My Orders: lieber warten statt Relist-Gebühr zahlen, wenn die Order sich innerhalb so vieler Tage ohnehin füllt. |
| `reaction_system`, `reaction_me_bonus`, `reaction_tax` | Reactions-Tab: System der Raffinerie (leer = typischer Reaction-Cost-Index), deren Rig-Materialbonus und Besitzersteuer. |
| `use_buy_orders` | Restock-Tab: zusätzlich Buy Orders einplanen, nicht nur Sofortkauf aus Sell Orders. |
| `use_lp_stores`, `lp_isk_value` | Faction-&-BPC-only-Tab: Blueprint-Kopien auch aus NPC-LP-Stores bepreisen, 1 LP = `lp_isk_value` ISK. |
| `pi_tax_rate` | Customs-Office-Steuer für PI (auch im PI-Tab änderbar). |
| `bp_containers` | Optionale Namen deiner Blueprint-Container pro Rolle (Blueprints- und Craft-Prep-Tab). Siehe [Blueprints](#blueprints). |

Optionale Schlüssel, die der Code liest, die aber nicht im Beispiel stehen: `faction_bpc_me` / `faction_bpc_te`
(angenommene Forschung gekaufter Faction-Kopien, Standard 0) und `eve_client_dir` (Ordner des Spielclients,
aus dem neuere Icons kopiert werden, Standard `C:\CCP\EVE`).

> Die Kopfzeile und einige Beschriftungen zeigen immer „Amarr“, auch wenn du einen anderen Hub
> einstellst. Die Zahlen stammen trotzdem von der eingestellten Station.

#### Kategorien

Gültige Schlüssel für `categories` (leere Liste = alle):

| Schlüssel | Tab | Schlüssel | Tab |
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
| `ships` | Ships | `mineral_builds` | Mineral Builds (sonstige T1-Items nur aus Mineralien) |

## Benutzung

### Der normale Weg: `run.bat`

**`run.bat`** doppelklicken. Das startet einen kleinen lokalen Webserver (`server.py`, hört nur auf
`127.0.0.1`). Der Server führt sofort ein Update aus und öffnet **<http://localhost:8766/>** im Browser.
Solange der erste Report gebaut wird, zeigt die Seite einen Hinweis und lädt sich selbst neu. Lass das
Fenster offen, solange du den Report benutzt. Schließen beendet den Server.

Ohne Batch-Datei:

```powershell
python server.py               # wie run.bat
python server.py --no-browser  # ohne Browser-Tab zu öffnen
```

### Der ⟳-Update-Button

Oben rechts im Report steht, wann Preise und Charakterdaten zuletzt geholt wurden, daneben der Button
**⟳ Update**. Ein Klick startet ein komplettes Update im Hintergrund, zeigt den Fortschritt und lädt die
Seite danach auf demselben Tab neu. Ist der Login abgelaufen, erscheint zusätzlich **Log in again**.

Der Button funktioniert, solange `run.bat` / `server.py` läuft, auch wenn du `output\report.html` direkt
von der Festplatte geöffnet hast. Soll der Button den Server selbst starten können, wenn er nicht läuft,
registrierst du einmalig den `everoi://`-Link:

```powershell
python install_link.py           # installieren (nach Verschieben des Ordners erneut ausführen)
python install_link.py --remove  # rückgängig machen
```

Das schreibt nur nach `HKEY_CURRENT_USER\Software\Classes\everoi`, Adminrechte sind also nicht nötig. Der
Browser fragt einmal, ob er „EVE Industry ROI“ öffnen darf. Ein so gestarteter Server läuft ohne Fenster und
beendet sich nach 3 Stunden ohne Kontakt zur Seite von selbst.

### Nur Kommandozeile

Den Report kannst du auch ohne Server bauen:

```powershell
python eve_invention.py                 # Update mit config.json
python eve_invention.py --login         # vorher einloggen (öffnet den Browser)
python eve_invention.py --update-sde    # CCPs statische Daten neu laden (nach einem Patch)
python eve_invention.py --config andere.json   # andere Config-Datei verwenden
```

Danach `output\report.html` öffnen. Die Konsole zeigt außerdem die Top 20 pro Kategorie.

### Ausgabedateien

| Datei | Inhalt |
|---|---|
| `output/report.html` | Der Report (eine eigenständige Seite). |
| `output/roi.csv` | Alle bewerteten Items mit Kosten, Profit, ROI, Volumen (durch Semikolon getrennt). |
| `output/crafting_list.csv` | Rezepte: Invention-Inputs, Skills, Baumaterialien (durch Semikolon getrennt). |
| `output/icons/` | Neuere Icons aus deinem Spielclient, falls installiert. |

### Was wann aktualisiert wird

| Daten | Aktualisierung |
|---|---|
| Market Orders an deiner Station, Adjusted Prices, System-Cost-Index | bei jedem Update |
| Charakterdaten (Skills, Orders, Wallet, Jobs, Assets, Planeten …) | bei jedem Update, falls eingeloggt |
| Preishistorie (`history_cache.json`) | einmal pro Tag und Item (ESI aktualisiert sie nur täglich) |
| Blueprint-Contracts (`contracts_cache.json`) | neue Contracts bei jedem Update. Ihr Inhalt wird gecacht, beendete Contracts fliegen raus. |
| Alle NPC-LP-Stores für den Faction-Tab (`lp_cache.json`) | alle 7 Tage, nur mit `use_lp_stores: true` |
| Campaign-LP-Stores (`campaign_lp_cache.json`) | einmal pro Tag |
| Campaign-Snapshots (`campaign_history.json`) | einer pro Tag, die letzten 120 bleiben |
| Statische Daten (`sde/`, `sde_cache.json`) | beim ersten Start. Danach nur mit `--update-sde` oder wenn eine neue Tool-Version den Cache neu aufbauen muss. |

Um eine Aktualisierung zu erzwingen, die Cache-Datei löschen. Sie wird beim nächsten Update neu gebaut.

Wallet-Transaktionen, Journal, Jobs und Orders liefert ESI nur für begrenzte Zeit (Transaktionen und Journal
etwa 30 Tage, Jobs 90 Tage). Das Tool sammelt sie in lokalen Dateien, sodass deine Historie weiter wächst,
**solange du mindestens alle paar Wochen ein Update laufen lässt**.

Filter, Sortierung, Limits, Shopping List und Tab-Auswahl speichert der Browser (localStorage), nicht das Tool.

## Die Tabs benutzen

### Crafting-ROI und Invention (Kategorie-Tabs)

- Die **Karten** oben zeigen die besten Kandidaten des aktuellen Tabs, darunter steht eine Zeile pro Item.
  Ein Klick auf die Zeile öffnet die Kostenaufstellung: Materialien, Invention, Job-Gebühren,
  Decryptor-Optionen und eine Make-or-Buy-Kette (⛓) für Teile, die du selbst bauen könntest.
- **Profit/batch** = eine erfolgreiche Invention, als komplette BPC gebaut und verkauft (T2), bzw. ein
  BPO-Job (T1). Fehlschläge, Material, Job-Gebühren, Sales Tax und Broker Fee sind eingerechnet.
  **ROI @avg** (zum 30-Tage-Durchschnittspreis) ist die vorsichtige Zahl, der du trauen solltest.
- Materialien werden über das Orderbuch für die benötigte Menge bepreist, nicht nur über die oberste Order.
- **Hide**-Filter: illiquid, unprofitable, BPO not for sale und (mit Login) missing skills, already selling,
  have T2 blueprint, in production, mats short, in stock.
  **Show only** (mit Login): can build now, can invent now, missing (Lücken in deinem Sortiment), things I use.
- **Limits**: Mindest-ROI, maximale Abverkaufstage, maximales Marktangebot, Mindestvolumen, Mindest-ISK/h,
  Mindest-Slotstunden, dazu Sortierung auf zwei Ebenen. „🔨 my builds as material cost“ rechnet Teile, die
  du selbst bauen kannst, mit deinen eigenen Kosten.
- „What do the columns mean?“ unter den Filtern erklärt jede Spalte.
- **Decryptors** zeigt pro T2-Item, ob ein Decryptor besser ist als Invention ohne.
  **Materials & Prices** listet alle Inputs. Ein Klick zeigt, wofür sie gebraucht werden.

### Restock, Planner, Craft Prep, Shopping List

- **🛒 Shopping List**: In den Crafting-Tabs auf **＋** vor einem Item klicken, um ein Batch hinzuzufügen
  (mehrfach klicken für mehr). Die Liste summiert Materialien und Datacores, berücksichtigt deinen Bestand und
  erzeugt eine Liste zum Einfügen ins Ingame-**Multibuy**.
- **🔄 Restock**: basiert auf dem, was deine Manufacturing- und Invention-Jobs in bis zu 30 Tagen verbraucht
  haben. Wahlweise „Keep stock for N days“ oder „Replace what I used“. Mit `use_buy_orders: true` siehst du
  zusätzlich, was eine Buy Order einen Tick über dem besten Gebot sparen würde und ob sie sich füllt. Ein roter
  Punkt am Tab heißt, dass Inputs ausgehen.
- **📅 Planner**: Für alles, was du in den letzten 30 Tagen verkauft hast, berechnet er, wie lange dein
  Bestand (Hangar + gelistet + in Jobs) reicht und wann du das nächste Batch starten musst, inklusive
  Invention, Bauzeit und Transport. Items müssen deinen Mindest-ROI, ISK pro Slotstunde und Profit pro Tag
  erfüllen. Umschaltbar zwischen „What I sell“ und „All my blueprints“.
- **🧪 Craft Prep**: nimmt die untätigen Blueprints in deinem `bpc_missiles`-Container und listet, was zuerst
  zu craften ist, welche Reactions laufen müssen, was zur Raffinerie muss, welche Rohstoffe zu kaufen sind
  (Multibuy-Listen) und welchen Science-Vorrat du brauchst.

### Tracking und G&V

Alle diese Tabs brauchen den Login.

- **My Orders**: alle Sell Orders und Buy Orders mit Status (undercut/outbid), Stückzahl vor dir, wie lange
  es ohnehin bis zur Füllung dauert, und Relist-Gebühr. Der Geduld-Regler (Standard `relist_patience_days`)
  entscheidet, wann die Empfehlung „warten“ statt „anpassen“ lautet. Ein roter Punkt am Tab heißt, dass eine
  Order undercut ist.
- **Trades**: echter Gewinn pro Item und pro Verkauf. Verwendet werden die tatsächliche Sales Tax und die
  Broker Fees aus deinem Wallet Journal sowie deine echten Produktionskosten. Außerdem siehst du Profit/Tag,
  deinen Marktanteil, wie lange dein Bestand reicht und wie oft du ausverkauft warst.
- **Production / Material Ledger / Jobs**: echte Stückkosten aus deinen abgeschlossenen Jobs (Material zum
  Durchschnitts-Einkaufspreis + echte Job-Gebühr + echte Invention-Kosten pro BPC) im Vergleich zur Schätzung
  des Reports. Dazu Bestand und Verbrauch pro Material und deine Jobliste. Ein grüner Punkt heißt, dass Jobs
  zur Auslieferung bereit sind.
- **Wallet / Growth**: Cashflow nach Buchungsart, Wallet-Verlauf, Assets, SP im Zeitverlauf und wie viele
  Manufacturing-/Science-/Reaction-Slots du nutzt im Vergleich zu dem, was deine Skills erlauben.
- **My Planets**: pro Kolonie Förderung pro Tag, Restlaufzeit der Extraktoren (roter Punkt, wenn einer in
  24 h ausläuft), Fabrikausstoß, Export/Import und Customs-Steuer.

### Blueprints

Der Tab **📘 Blueprints** listet alle BPOs, T2-Kopien, T1-Kopien und Faction-Kopien, die du besitzt, mit Ort,
ME/TE, Runs und einem Vorschlag, was zu tun ist (im Job, unprofitabel, jetzt bauen, erforschen, warten, frei
zum Bauen).

Dazu schlägt er ein **Container-System** vor: ein Container pro Rolle im Hangar deiner Baustation, im Spiel
per *Set Name* benannt. Standardmäßig zählt nur die Zahl am Anfang des Namens:

| Nr. | Rolle (`bp_containers`-Schlüssel) | Inhalt |
|---|---|---|
| 1 | `build` | untätige Blueprints, deren Item zur Neige geht: die Bauliste für heute |
| 2 | `invent` | T1-Kopien zum Inventen von Missiles, Ammo und Crystals |
| 3 | `bpc_missiles` | invente Kopien: Missiles, Ammo, Crystals (wird auch von Craft Prep genutzt) |
| 4 | `bpc_other` | alle anderen Kopien, die sich zu bauen lohnen |
| 5 | `bpo_ammo` | BPOs: Ammo, Missiles, Laser- und Mining-Crystals |
| 6 | `bpo_modules` | T1-Ausrüstung: Module, Rigs, Schiffe, Drohnen, Mining-Ausrüstung |
| 7 | `bpo_misc` | Teile und Vorprodukte: Components, R.A.M., Fuel Blocks … |
| 9 | `dead` | Kopien, die Verlust machen oder keinen Markt haben |

Eigene Namen trägst du in `config.json` ein, zum Beispiel:

```json
"bp_containers": {"build": "1 BUILD NOW", "invent": "2 INVENT", "bpc_missiles": "3 BPC MISSILES",
                  "bpc_other": "4 BPC OTHER", "bpo_ammo": "5 BPO AMMO", "bpo_modules": "6 BPO GEAR",
                  "bpo_misc": "7 BPO PARTS", "dead": "9 DEAD"}
```

Nach dem nächsten Update zeigt der Tab, welche Blueprint wohin verschoben werden soll.

### Campaign und LP Store (`campaign.json`)

`campaign.json` beschreibt eine Ingame-Kampagne mit Item Trader. Die mitgelieferte Datei gilt für
„Into the Light (Amarr)“. Der Tab **🎯 Campaign** vergleicht pro Objective-Item die **Abgabe** (ISK + LP +
Anteil an einem Trader-Token) mit dem **Verkauf**. Er zeigt außerdem, was eine Abgabe bringt, die besten
LP-Angebote zum Ausgeben der LP, die Token-Belohnungen und Preisverläufe. Der Tab **🏛 LP Store** listet alle
Angebote der Stores aus `lp_stores` mit ISK/LP per Sell Order und sofort, verkauften Angeboten pro Tag und
benötigten Items. Blueprint-Kopien werden als das Item bewertet, das du daraus bei ME0 bauen würdest. Filtern
kannst du nach Name, Mindestvolumen, Mindest-ISK/LP oder „keine benötigten Items“. Ein Klick auf ein
Angebot zeigt dessen Kostenaufstellung.

Beide Tabs brauchen nur Marktdaten und funktionieren deshalb ohne Login. Trag in `campaign.json` die Zahlen
aus dem Ingame-Objective-Fenster ein:

| Feld | Bedeutung |
|---|---|
| `campaign`, `ends`, `trader` | Name, Enddatum (`JJJJ-MM-TT`) und Ort des Traders (nur Anzeige). |
| `turnin_tax`, `turnin_tax_lp` | Steuer auf die ISK- und LP-Auszahlung des Objectives (`0.05` = 5 %). `turnin_tax_lp: true` = gleich wie die ISK-Steuer. |
| `lp_value` | ISK pro LP für Abgaben. Ist der ISK/LP-Wert am Markt niedriger, wird dieser genommen. |
| `token` | `type_id` und `name` des Trader-Tokens. |
| `lp_stores` | Corporation-IDs der LP Stores, die im LP-Store-Tab erscheinen. |
| `objectives[]` | `type_id`, `name`, `isk` und `lp` **pro Contribution** (1 Contribution = 1 abgegebenes Item), `lp_corp` (wessen LP du bekommst), `per_token` (Items pro Token, `null` wenn unbekannt), `how` (Notiz). `isk`/`lp` auf `null` lassen, solange du sie nicht kennst. |
| `rewards[]` | Belohnungen beim Trader: `type_id`, `name`, `tokens` (Preis in Tokens). |
| `also_watch[]` | Weitere Items, deren Preise du sehen willst: `type_id`, `name`, `note`. |

Items werden per Type-ID angegeben, weil die Items einer neuen Kampagne oft noch nicht in der lokalen SDE
stehen. Wenn du nur den LP-Store-Tab willst, `token` behalten und `objectives` auf `[]` setzen.
`campaign.json` löschen oder umbenennen blendet beide Tabs aus.

## Datenschutz: Dateien, die du nie weitergeben darfst

Diese Dateien enthalten deinen Login oder deine persönlichen Spieldaten. **Niemals committen, hochladen oder
verschicken.** Alle stehen in `.gitignore`.

| Datei | Inhalt |
|---|---|
| `token.json` | dein EVE-Login (Refresh Token): Wer ihn hat, kann die Daten deines Charakters lesen |
| `config.json` | deine Einstellungen und die Client ID deiner Anwendung |
| `transactions.json` | deine Wallet-Transaktionen |
| `journal.json` | dein Wallet Journal |
| `jobs.json` | deine Industry Jobs |
| `order_events.json` | jeder Zustand deiner Market Orders |
| `sell_orders.json` | Historie deiner Sell Orders |
| `growth.json` | tägliche Snapshots von Wallet, Assets und Skillpunkten |

Vorsicht auch mit **`output/report.html`**. Mit Login enthält er Charaktername, Orders, Wallet und Assets,
also nicht öffentlich posten.

Alles andere, was nicht in Git ist, wird generiert und kann jederzeit gelöscht werden: `sde/`,
`sde_cache.json`, `history_cache.json`, `contracts_cache.json`, `lp_cache.json`, `campaign_lp_cache.json`,
`campaign_history.json`, `output/`, `__pycache__/`. Wer die privaten Tracking-Dateien oben löscht, verliert
Historie, die ESI nicht mehr hat.

Um dem Tool den Zugriff komplett zu entziehen, `token.json` löschen und die Anwendung aus der Liste der
autorisierten Drittanbieter-Anwendungen deines EVE-Accounts entfernen.

## Fehlerbehebung

**„EVE login expired - run login.bat again“**
Der gespeicherte Login konnte nicht erneuert werden. `login.bat` ausführen (oder im Report **Log in again**
klicken). Bis dahin gelten Skills und Gebühren aus `config.json`, und die Tracking-Tabs bleiben leer.

**„Some tracking needs new permissions (…) - run login.bat once more“** / ein Tab zeigt *Not available yet*
Deinem Login fehlen Scopes. Prüfen, ob deine Anwendung alle neun Scopes von oben hat, dann neu einloggen.

**„Put your application's Client ID into config.json (esi_client_id) first.“**
`--login` wurde ohne Client ID aufgerufen. Schritt 2 der Einrichtung erledigen.

**Die Loginseite zeigt einen Fehler, oder das Tool wartet nach dem Login weiter**
Die Callback URL deiner Anwendung muss genau `http://localhost:8765/callback` lauten, gleich wie
`esi_callback`, und Port 8765 muss frei sein.

**`run.bat` / `login.bat` finden Python nicht**
Sie rufen `%LOCALAPPDATA%\Programs\Python\Python312\python.exe` auf. Entweder Python 3.12 dort installieren
(Standard von python.org bei „nur für mich“) oder die Batch-Dateien weglassen und `python server.py` bzw.
`python eve_invention.py --login` starten.

**Der Update-Button meldet „Could not start the updater“**
Der Server läuft nicht, und der `everoi://`-Link ist nicht installiert oder wurde nicht erlaubt. `run.bat`
starten oder `python install_link.py` ausführen und die Browser-Abfrage erlauben.

**Absturz beim ersten Start: `FileNotFoundError` für `sde\icons.jsonl`**
Das passiert, wenn der EVE-Client unter `C:\CCP\EVE` (bzw. `eve_client_dir`) installiert ist. Das Tool
versucht dann, neuere Icons zu kopieren, aber der automatische SDE-Download entpackt `icons.jsonl` nicht.
Entweder `icons.jsonl` aus dem SDE-Zip
(<https://developers.eveonline.com/static-data/eve-online-static-data-latest-jsonl.zip>) in den Ordner `sde\`
entpacken oder in `config.json` `"eve_client_dir"` auf einen Ordner ohne Spielclient setzen.

**Neue Items aus dem letzten Patch fehlen**
`python eve_invention.py --update-sde` lädt die neueste SDE (etwa 100 MB, danach etwa 30 s Einlesen).

**„Unknown solar system '…'“**, **„Unknown category …“**, **„Unknown decryptor '…'“**
Schreibweise in `config.json` prüfen. Bei Kategorien und Decryptoren listet die Meldung die gültigen Werte auf.

**„ESI kept refusing … - try the update again in a minute“**
ESI hat gedrosselt oder war down. Das Tool versucht es schon mehrmals mit Pausen, also einfach später erneut
probieren. Fallen Contracts, LP Stores, Mutaplasmide oder die Kampagne aus, läuft das Update ohne sie weiter.

**Die Industry-/Economy-Tabs zeigen „–“**
Du bist nicht eingeloggt, oder dem Login fehlen die Wallet-/Industry-Scopes.

**Meine Filter oder die Shopping List sind weg**
Sie werden pro Browser und pro Adresse gespeichert. `http://localhost:8766/` und die Datei
`output\report.html` haben getrennte Einstellungen.

## Dateien

| Datei | Zweck |
|---|---|
| `eve_invention.py` | Hauptskript: statische Daten, Marktdaten, Kosten- und ROI-Berechnung, Report |
| `tracking.py` | deine Orders, Trades, Jobs, Wallet, Planeten, echte Kostenrechnung |
| `campaign.py` / `campaign.json` | Campaign- und LP-Store-Tab |
| `chains.py` | Make-or-Buy-Ketten (Teile selbst bauen oder kaufen) |
| `esi_auth.py` | EVE-SSO-Login (PKCE, ohne Secret) und Charakterdaten |
| `server.py` | lokaler Server für den Report und den Update-Button |
| `install_link.py` | registriert den `everoi://`-Link für den Update-Button |
| `report_template.html` | die Report-Seite |
| `config.example.json` | Vorlage für die Einstellungen |
| `run.bat`, `login.bat` | Windows-Verknüpfungen |
