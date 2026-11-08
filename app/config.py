# Shared by every component. No port or timeout is hardcoded anywhere else.
import os
ACK_TIMEOUT = float(os.environ.get("ACK_TIMEOUT", 0.3))

SERVER_IP = "10.0.0.1"          # in Mininet, use the server host's IP
BG_PORT = 5201

REG_PORT     = 5000
PORT_NORMAL  = 5001
PORT_CRITICAL = 5002
PORT_ACK     = 5003
PORT_TRIGGER = 5004
PORT_HIGH    = 5006

LEVELS = ("critical", "high", "normal")

LEVEL_PORT = {
    "critical": PORT_CRITICAL,
    "high": PORT_HIGH,
    "normal": PORT_NORMAL,
}

REQUIRES_ACK = {"critical": True, "high": True, "normal": False}
MAX_RETRIES  = {"critical": 5,    "high": 3,    "normal": 0}

ACK_TIMEOUT = 0.3      # seconds before first retransmission
BACKOFF     = 2.0      # timeout multiplier per retry
MAX_TIMEOUT = 2.0      # cap on the retry interval

REG_TIMEOUT  = 1.0
REG_ATTEMPTS = 5

BUF = 2048

CONTROLLER_URL = "http://10.0.0.254:8080"   # set to None for localhost-only tests