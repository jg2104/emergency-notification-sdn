import argparse
import socket
import threading
import time

import config
import protocol
from logger import Logger
import json
import urllib.request

def notify_controller(action, alert_id):
    if not config.CONTROLLER_URL:
        return
    try:
        req = urllib.request.Request(
            f"{config.CONTROLLER_URL}/alert/{action}",
            data=json.dumps({"alert_id": str(alert_id)}).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        urllib.request.urlopen(req, timeout=0.5).read()
    except Exception:
        pass   # the controller being unreachable must never break alerting

class Server:
    def __init__(self, logfile):
        self.log = Logger(logfile, echo=True)
        self.subs = {}                       # sub_id -> info dict
        self.subs_lock = threading.Lock()
        self.pending = {}                    # (msg_id, sub_id) -> Event
        self.pending_lock = threading.Lock()
        self.id_lock = threading.Lock()
        self.next_msg_id = 1
        self.next_alert_id = 1
        self.send_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    #helpers
    def reply(self, sock, msg_type, addr, **fields):
        try:
            sock.sendto(protocol.encode(msg_type, **fields), addr)
        except OSError:
            pass

    def new_msg_id(self):
        with self.id_lock:
            self.next_msg_id += 1
            return self.next_msg_id - 1

    def new_alert_id(self):
        with self.id_lock:
            self.next_alert_id += 1
            return self.next_alert_id - 1

    # registration (UDP 5000) 
    def registration_loop(self):
        sock = protocol.bind_udp(config.REG_PORT)
        while True:
            try:
                data, addr = sock.recvfrom(config.BUF)
            except ConnectionResetError:     # Windows ICMP quirk
                continue
            msg = protocol.decode(data)
            if msg is None:
                continue
            sub_id = msg.get("id")
            if not isinstance(sub_id, str) or not sub_id:
                continue

            if msg["type"] == protocol.REGISTER:
                levels = msg.get("levels", list(config.LEVELS))
                if (not isinstance(levels, list) or
                        not all(l in config.LEVELS for l in levels)):
                    continue
                try:
                    offset = int(msg.get("offset", 0))
                except (TypeError, ValueError):
                    continue
                with self.subs_lock:         # duplicate ID just updates entry
                    old = self.subs.get(sub_id)
                    self.subs[sub_id] = {
                        "ip": addr[0], "offset": offset, "levels": levels,
                        "undelivered": old["undelivered"] if old else 0,
                    }
                    total = len(self.subs)
                self.log.log("REGISTER", subscriber=sub_id,
                             detail=f"{addr[0]} offset={offset} "
                                    f"levels={','.join(levels)} total={total}")
                self.reply(sock, protocol.REGISTER_OK, addr, id=sub_id)

            elif msg["type"] == protocol.UNREGISTER:
                with self.subs_lock:
                    self.subs.pop(sub_id, None)
                    total = len(self.subs)
                self.log.log("UNREGISTER", subscriber=sub_id,
                             detail=f"total={total}")

    # ACKs (UDP 5003) 
    def ack_loop(self):
        sock = protocol.bind_udp(config.PORT_ACK)
        while True:
            try:
                data, addr = sock.recvfrom(config.BUF)
            except ConnectionResetError:
                continue
            msg = protocol.decode(data)
            if msg is None or msg["type"] != protocol.ACK:
                continue
            try:
                key = (msg.get("msg_id"), msg.get("id"))
                with self.pending_lock:
                    ev = self.pending.get(key)
            except TypeError:                # unhashable garbage
                continue
            if ev is not None:
                ev.set()

    #  trigger (UDP 5004) 
    def trigger_loop(self):
        sock = protocol.bind_udp(config.PORT_TRIGGER)
        while True:
            try:
                data, addr = sock.recvfrom(config.BUF)
            except ConnectionResetError:
                continue
            msg = protocol.decode(data)
            if msg is None or msg["type"] != protocol.TRIGGER:
                continue
            level = msg.get("level")
            if level not in config.LEVELS:
                self.reply(sock, protocol.ERROR, addr, reason="bad level")
                continue
            try:
                count = max(1, int(msg.get("count", 1)))
                interval = max(0.0, float(msg.get("interval", 1.0)))
            except (TypeError, ValueError):
                self.reply(sock, protocol.ERROR, addr, reason="bad count/interval")
                continue
            text = str(msg.get("text", ""))
            alert_id = self.new_alert_id()
            self.reply(sock, protocol.TRIGGER_OK, addr, alert_id=alert_id)
            threading.Thread(target=self.run_alert, daemon=True,
                             args=(alert_id, level, text, count, interval)).start()

    # alert sending
    def run_alert(self, alert_id, level, text, count, interval):
        self.log.log("ALERT_START", alert_id=alert_id, level=level,
                    detail=f"count={count} interval={interval}")
        notify_controller("start", alert_id)
        threads = []
        for i in range(count):
            msg_id = self.new_msg_id()
            ts = protocol.now()
            with self.subs_lock:
                targets = [(sid, dict(s)) for sid, s in self.subs.items()
                       if level in s["levels"]]
            for sid, info in targets:
                t = threading.Thread(
                    target=self.deliver, daemon=True,
                    args=(sid, info, level, alert_id, msg_id, text, ts))
                t.start()
                threads.append(t)
            if i < count - 1:
                time.sleep(interval)
        self.log.log("ALERT_END", alert_id=alert_id, level=level)
        for t in threads:
            t.join()
        self.log.log("ALERT_DONE", alert_id=alert_id, level=level)
        notify_controller("end", alert_id)

    def deliver(self, sub_id, info, level, alert_id, msg_id, text, ts):
        dest = (info["ip"], config.LEVEL_PORT[level] + info["offset"])
        needs_ack = config.REQUIRES_ACK[level]
        max_retries = config.MAX_RETRIES[level]
        key = (msg_id, sub_id)
        ev = threading.Event()
        if needs_ack:
            with self.pending_lock:
                self.pending[key] = ev
        try:
            for attempt in range(max_retries + 1):
                pkt = protocol.encode(
                    protocol.ALERT, alert_id=alert_id, msg_id=msg_id,
                    level=level, ts=ts, retry=attempt, text=text)
                try:
                    self.send_sock.sendto(pkt, dest)
                except OSError:
                    pass
                self.log.log("SEND", alert_id, msg_id, level, sub_id, attempt)
                if not needs_ack:
                    return                   # normal: fire and forget
                timeout = min(config.ACK_TIMEOUT * config.BACKOFF ** attempt,
                              config.MAX_TIMEOUT)
                if ev.wait(timeout):
                    self.log.log("DELIVERED", alert_id, msg_id, level,
                                 sub_id, attempt)
                    return
            # retries exhausted: record it, keep subscriber registered
            with self.subs_lock:
                if sub_id in self.subs:
                    self.subs[sub_id]["undelivered"] += 1
            self.log.log("UNDELIVERED", alert_id, msg_id, level, sub_id,
                         str(max_retries))
        finally:
            if needs_ack:
                with self.pending_lock:
                    self.pending.pop(key, None)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--log", default="server_log.csv")
    args = ap.parse_args()

    srv = Server(args.log)
    for target in (srv.registration_loop, srv.ack_loop, srv.trigger_loop):
        threading.Thread(target=target, daemon=True).start()
    print("Server running (Ctrl+C to stop)")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("Stopped")


if __name__ == "__main__":
    main()