"""
EVE SSO login (OAuth2 with PKCE, no client secret needed) and the character
data the ROI tool uses: skills, standings and owned blueprints.

The refresh token is stored in token.json next to this file. Delete that file
to log out.
"""
import base64
import hashlib
import http.server
import json
import os
import secrets
import time
import urllib.parse
import webbrowser

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
TOKEN_FILE = os.path.join(HERE, "token.json")
SSO = "https://login.eveonline.com/v2/oauth"
ESI = "https://esi.evetech.net/latest"
SCOPES = [
    "esi-skills.read_skills.v1",
    "esi-characters.read_standings.v1",
    "esi-characters.read_blueprints.v1",
    "esi-markets.read_character_orders.v1",
    "esi-wallet.read_character_wallet.v1",
    "esi-universe.read_structures.v1",
    "esi-industry.read_character_jobs.v1",
    "esi-assets.read_assets.v1",
]


def _b64url(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _jwt_claims(token):
    payload = token.split(".")[1]
    return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))


def _save(tok):
    claims = _jwt_claims(tok["access_token"])
    data = {
        "refresh_token": tok["refresh_token"], "access_token": tok["access_token"],
        "expires_at": time.time() + tok.get("expires_in", 1199) - 30,
        "character_id": int(claims["sub"].split(":")[-1]), "character_name": claims.get("name"),
        "scopes": claims.get("scp") if isinstance(claims.get("scp"), list) else [claims.get("scp")],
    }
    with open(TOKEN_FILE, "w", encoding="utf8") as f:
        json.dump(data, f)
    return data


def login(client_id, callback):
    """Open the EVE login page in the browser and wait for the redirect."""
    verifier = _b64url(secrets.token_bytes(32))
    challenge = _b64url(hashlib.sha256(verifier.encode()).digest())
    state = secrets.token_urlsafe(16)
    url = f"{SSO}/authorize?" + urllib.parse.urlencode({
        "response_type": "code", "redirect_uri": callback, "client_id": client_id,
        "scope": " ".join(SCOPES), "code_challenge": challenge,
        "code_challenge_method": "S256", "state": state,
    })

    result = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if "code" not in q:
                self.send_response(404)
                self.end_headers()
                return
            result.update(code=q["code"][0], state=q.get("state", [""])[0])
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write("<h2>Logged in - you can close this tab.</h2>".encode())

        def log_message(self, *a):
            pass

    parsed = urllib.parse.urlparse(callback)
    server = http.server.HTTPServer((parsed.hostname, parsed.port), Handler)
    print("Opening the EVE login page in your browser...")
    webbrowser.open(url)
    while "code" not in result:
        server.handle_request()
    server.server_close()
    if result["state"] != state:
        raise SystemExit("Login failed: state mismatch, please try again.")

    r = requests.post(f"{SSO}/token", data={
        "grant_type": "authorization_code", "code": result["code"],
        "client_id": client_id, "code_verifier": verifier,
    }, headers={"Content-Type": "application/x-www-form-urlencoded"}, timeout=30)
    r.raise_for_status()
    data = _save(r.json())
    print(f"Logged in as {data['character_name']}.")
    return data


def get_token(client_id):
    """Return a valid access token (refreshing it if needed), or None if not logged in."""
    if not os.path.exists(TOKEN_FILE):
        return None
    with open(TOKEN_FILE, encoding="utf8") as f:
        data = json.load(f)
    if time.time() < data["expires_at"]:
        return data
    r = requests.post(f"{SSO}/token", data={
        "grant_type": "refresh_token", "refresh_token": data["refresh_token"], "client_id": client_id,
    }, headers={"Content-Type": "application/x-www-form-urlencoded"}, timeout=30)
    if r.status_code != 200:
        print("EVE login expired - run login.bat again. Using config.json values for now.")
        return None
    return _save(r.json())


def _get(path, token, **params):
    r = requests.get(f"{ESI}{path}", params=params, timeout=30,
                     headers={"Authorization": f"Bearer {token['access_token']}"})
    r.raise_for_status()
    return r


def _pages(path, token, **params):
    out, page = [], 1
    while True:
        r = _get(path, token, page=page, **params)
        out += r.json()
        if page >= int(r.headers.get("X-Pages", 1)):
            return out
        page += 1


def load_character(client_id, station_id):
    """Skills, standings and blueprints of the logged-in character, or None."""
    tok = get_token(client_id)
    if not tok:
        return None
    cid = tok["character_id"]
    skills = _get(f"/characters/{cid}/skills/", tok).json()
    standings = _get(f"/characters/{cid}/standings/", tok).json()

    blueprints = _pages(f"/characters/{cid}/blueprints/", tok)

    # market orders + wallet transactions (need scopes added later; ask to re-login if missing)
    granted = set(tok.get("scopes") or [])
    orders = orders_history = transactions = journal = balance = jobs = assets = None
    if {"esi-markets.read_character_orders.v1", "esi-wallet.read_character_wallet.v1"} <= granted:
        orders = _get(f"/characters/{cid}/orders/", tok).json()
        orders_history = _pages(f"/characters/{cid}/orders/history/", tok)
        transactions = _get(f"/characters/{cid}/wallet/transactions/", tok).json()
        journal = _pages(f"/characters/{cid}/wallet/journal/", tok)
        balance = _get(f"/characters/{cid}/wallet/", tok).json()
    if {"esi-industry.read_character_jobs.v1", "esi-assets.read_assets.v1"} <= granted:
        jobs = _get(f"/characters/{cid}/industry/jobs/", tok, include_completed="true").json()
        assets = _pages(f"/characters/{cid}/assets/", tok)
    missing = [s for s in SCOPES if s not in granted]
    if missing:
        print(f"Some tracking needs new permissions ({', '.join(missing)}) - run login.bat once more.")

    # who owns the market station (broker fee depends on standings towards them)
    station = requests.get(f"{ESI}/universe/stations/{station_id}/", timeout=30).json()
    corp_id = station.get("owner")
    faction_id = requests.get(f"{ESI}/corporations/{corp_id}/", timeout=30).json().get("faction_id") if corp_id else None
    st = {(s["from_type"], s["from_id"]): s["standing"] for s in standings}

    return {
        "id": cid, "name": tok["character_name"],
        "skills": {s["skill_id"]: s["active_skill_level"] for s in skills.get("skills", [])},
        "total_sp": skills.get("total_sp"),
        "corp_standing": st.get(("npc_corp", corp_id), 0.0),
        "faction_standing": st.get(("faction", faction_id), 0.0),
        "blueprints": blueprints, "orders": orders, "transactions": transactions,
        "journal": journal, "balance": balance, "jobs": jobs, "assets": assets, "orders_history": orders_history,
        "can_read_structures": "esi-universe.read_structures.v1" in granted,
    }


def structure_name(client_id, structure_id):
    """Name of a player structure (only works if the character has docking access)."""
    tok = get_token(client_id)
    try:
        return _get(f"/universe/structures/{structure_id}/", tok).json().get("name")
    except requests.RequestException:
        return None
