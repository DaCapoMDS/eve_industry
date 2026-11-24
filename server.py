"""
Local web server for the report, so the page can refresh itself.

    python server.py        (run.bat does this)

Serves output/report.html on http://localhost:8766/ and runs eve_invention.py
when the page's Update button asks for it. Only listens on 127.0.0.1.

Also started by the everoi:// link (see install_link.py) when the Update button
is clicked while the server isn't running; then it runs without a console and
stops by itself after IDLE_HOURS without any page talking to it.
"""
import time
import datetime as dt
import http.server
import json
import os
import subprocess
import sys
import threading
import webbrowser

HERE = os.path.dirname(os.path.abspath(__file__))
REPORT = os.path.join(HERE, "output", "report.html")
PORT = 8766
IDLE_HOURS = 3
last_seen = time.time()

FILES = {"config": os.path.join(HERE, "config.json"), "campaign": os.path.join(HERE, "campaign.json")}
HIDDEN = {"esi_client_id", "esi_callback"}  # never sent to the page, never changed from it
EXTRA_KEYS = {"eve_client_dir", "faction_bpc_me", "faction_bpc_te", "bp_containers", "structure_me_bonus",
              "structure_te_bonus", "structure_job_cost_bonus", "facility_tax", "scc_surcharge", "reaction_system",
              "reaction_me_bonus", "reaction_tax", "hub_fee_discount"}


def read_settings():
    out = {}
    for name, path in FILES.items():
        try:
            with open(path, encoding="utf8") as f:
                out[name] = {k: v for k, v in json.load(f).items() if k not in HIDDEN}
        except (OSError, ValueError):
            out[name] = {}
    return out


def _same_kind(old, new):
    num = (int, float)
    if old is None or new is None:
        return True
    if isinstance(old, bool) or isinstance(new, bool):
        return isinstance(old, bool) and isinstance(new, bool)
    if isinstance(old, num):
        return isinstance(new, num)
    return type(old) is type(new)


def save_settings(changes):
    """Merge top-level keys into config.json / campaign.json (backup first). Only known keys, same value kinds."""
    example = {}
    try:
        with open(os.path.join(HERE, "config.example.json"), encoding="utf8") as f:
            example = json.load(f)
    except (OSError, ValueError):
        pass
    for name, upd in (changes or {}).items():
        if name not in FILES or not isinstance(upd, dict) or not upd:
            continue
        path = FILES[name]
        with open(path, encoding="utf8") as f:
            cur = json.load(f)
        known = set(cur) | (set(example) | EXTRA_KEYS if name == "config" else set())
        for k, v in upd.items():
            if k.startswith("_") or k in HIDDEN or k not in known:
                raise ValueError(f"unknown setting: {k}")
            if not _same_kind(cur.get(k, example.get(k)), v):
                raise ValueError(f"wrong kind of value for {k}")
        with open(path + ".bak", "w", encoding="utf8") as f:
            json.dump(cur, f, indent=2, ensure_ascii=False)
        cur.update(upd)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf8") as f:
            json.dump(cur, f, indent=2, ensure_ascii=False)
        os.replace(tmp, path)
    return read_settings()


state = {"running": False, "log": [], "started": None, "finished": None, "ok": None, "login": False}
lock = threading.Lock()


def run_update(login=False):
    """Run the report generator in the background, keeping its output for the page."""
    with lock:
        if state["running"]:
            return False
        state.update(running=True, log=[], started=dt.datetime.now().isoformat(timespec="seconds"),
                     finished=None, ok=None, login=login)

    def work():
        cmd = [sys.executable, "-u", os.path.join(HERE, "eve_invention.py")] + (["--login"] if login else [])
        proc = subprocess.Popen(cmd, cwd=HERE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                env={**os.environ, "PYTHONIOENCODING": "utf8"}, text=True, encoding="utf8")
        for line in proc.stdout:
            line = line.rstrip()
            if line:
                print(line)
                state["log"].append(line)
        proc.wait()
        state.update(running=False, ok=proc.returncode == 0,
                     finished=dt.datetime.now().isoformat(timespec="seconds"))

    threading.Thread(target=work, daemon=True).start()
    return True


class Handler(http.server.BaseHTTPRequestHandler):
    def _cors(self):
        # the report opened straight from disk (file://) has origin "null"; allow only that
        if self.headers.get("Origin") == "null":
            self.send_header("Access-Control-Allow-Origin", "null")
            self.send_header("Access-Control-Allow-Headers", "X-Eve-Update, Content-Type")
            self.send_header("Access-Control-Allow-Methods", "GET, POST")

    def do_OPTIONS(self):
        self.send_response(204 if self.headers.get("Origin") == "null" else 403)
        self._cors()
        self.end_headers()

    def _send(self, code, body, ctype="application/json"):
        global last_seen
        last_seen = time.time()
        data = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self._cors()
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/", "/index.html", "/report.html"):
            if not os.path.exists(REPORT):
                return self._send(200, "<meta http-equiv=refresh content=3><p style='font-family:sans-serif'>"
                                       "Building the report for the first time, this page reloads by itself…</p>",
                                  "text/html; charset=utf-8")
            with open(REPORT, "rb") as f:
                return self._send(200, f.read(), "text/html; charset=utf-8")
        if path == "/settings":
            return self._send(200, json.dumps(read_settings()))
        if path == "/status":
            return self._send(200, json.dumps({**state, "log": state["log"][-6:]}))
        self._send(404, "{}")

    def do_POST(self):
        # custom header = browsers must do a CORS preflight first, which we never allow,
        # so other websites can't trigger updates in the background
        if self.headers.get("X-Eve-Update") != "1":
            return self._send(403, "{}")
        path = self.path.split("?")[0]
        if path == "/update":
            started = run_update(login="login=1" in self.path)
            return self._send(202 if started else 409, json.dumps({"started": started}))
        if path == "/settings":
            try:
                n = int(self.headers.get("Content-Length") or 0)
                if n > 200000:
                    raise ValueError("too large")
                body = json.loads(self.rfile.read(n) or b"{}")
                return self._send(200, json.dumps(save_settings(body)))
            except (ValueError, OSError) as e:
                return self._send(400, json.dumps({"error": str(e)}))
        self._send(404, "{}")

    def log_message(self, *a):
        pass


def run_remote_update():
    """Server already running: just ask it to update."""
    import urllib.request
    req = urllib.request.Request(f"http://127.0.0.1:{PORT}/update", method="POST", headers={"X-Eve-Update": "1"})
    try:
        urllib.request.urlopen(req, timeout=5)
    except Exception:
        pass


def main():
    try:
        server = http.server.ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    except OSError:
        if any(a.startswith("everoi:") for a in sys.argv[1:]):
            run_remote_update()
            return
        print(f"Already running - opening http://localhost:{PORT}/")
        webbrowser.open(f"http://localhost:{PORT}/")
        return
    url = f"http://localhost:{PORT}/"
    print(f"EVE Industry ROI running at {url}  (close this window to stop)")
    run_update()  # fresh data on every start
    # started from the page's Update button (everoi://): that page is already open
    if "--no-browser" not in sys.argv and not any(a.startswith("everoi:") for a in sys.argv[1:]):
        webbrowser.open(url)

    def idle_stop():
        while True:
            time.sleep(300)
            if not state["running"] and time.time() - last_seen > IDLE_HOURS * 3600:
                server.shutdown()
                return
    threading.Thread(target=idle_stop, daemon=True).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
