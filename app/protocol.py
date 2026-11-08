import json
import socket
import time

# Message types
REGISTER = "REGISTER"
REGISTER_OK = "REGISTER_OK"
UNREGISTER = "UNREGISTER"
ALERT = "ALERT"
ACK = "ACK"
TRIGGER = "TRIGGER"
TRIGGER_OK = "TRIGGER_OK"
ERROR = "ERROR"


def now():
    return time.monotonic()


def encode(msg_type, **fields):
    d = {"type": msg_type}
    d.update(fields)
    return json.dumps(d, separators=(",", ":")).encode("utf-8")


def decode(data):
    """Return a dict, or None for anything malformed. Never raises."""
    try:
        d = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(d, dict) or not isinstance(d.get("type"), str):
        return None
    return d


def bind_udp(port, host="0.0.0.0"):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind((host, port))
    return s