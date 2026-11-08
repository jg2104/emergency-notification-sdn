import argparse
import socket
import threading
import time

import config
import protocol
from logger import Logger


class Subscriber:
    def __init__(self, sub_id, server_ip, offset, levels, logfile, drop_first):
        self.sub_id = sub_id
        self.server_ip = server_ip
        self.offset = offset
        self.levels = levels
        self.drop_first = drop_first
        self.log = Logger(logfile)
        self.seen = set()
        self.seen_lock = threading.Lock()
        self.dropped = set()
        self.ack_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        # bind listening sockets BEFORE registering so no alert can be missed
        self.socks = {lvl: protocol.bind_udp(config.LEVEL_PORT[lvl] + offset)
                      for lvl in levels}

    def register(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(config.REG_TIMEOUT)
        req = protocol.encode(protocol.REGISTER, id=self.sub_id,
                              offset=self.offset, levels=self.levels)
        for attempt in range(1, config.REG_ATTEMPTS + 1):
            sock.sendto(req, (self.server_ip, config.REG_PORT))
            try:
                data, _ = sock.recvfrom(config.BUF)
                msg = protocol.decode(data)
                if (msg and msg["type"] == protocol.REGISTER_OK
                        and msg.get("id") == self.sub_id):
                    self.log.log("REGISTERED", subscriber=self.sub_id,
                                 retry=attempt - 1)
                    sock.close()
                    return True
            except socket.timeout:
                pass
            except ConnectionResetError:     # Windows: server not up yet
                time.sleep(config.REG_TIMEOUT)
            self.log.log("REGISTER_RETRY", subscriber=self.sub_id, retry=attempt)
        sock.close()
        return False

    def unregister(self):
        try:
            self.ack_sock.sendto(
                protocol.encode(protocol.UNREGISTER, id=self.sub_id),
                (self.server_ip, config.REG_PORT))
        except OSError:
            pass

    def listen(self, level):
        sock = self.socks[level]
        while True:
            try:
                data, addr = sock.recvfrom(config.BUF)
            except ConnectionResetError:
                continue
            rx = protocol.now()
            msg = protocol.decode(data)
            if msg is None or msg["type"] != protocol.ALERT:
                continue
            self.handle_alert(msg, level, rx)

    def handle_alert(self, msg, level, rx):
        msg_id = msg.get("msg_id")
        if not isinstance(msg_id, int):
            return
        alert_id = msg.get("alert_id", "")
        retry = msg.get("retry", 0)
        needs_ack = config.REQUIRES_ACK[level]

        # testing aid: ignore the first copy of each ACK-required message
        if self.drop_first and needs_ack and msg_id not in self.dropped:
            self.dropped.add(msg_id)
            self.log.log("DROPPED", alert_id, msg_id, level, self.sub_id, retry)
            return

        with self.seen_lock:
            duplicate = msg_id in self.seen
            self.seen.add(msg_id)

        if duplicate:
            self.log.log("DUPLICATE", alert_id, msg_id, level, self.sub_id, retry)
        else:
            ts = msg.get("ts")
            latency = f"latency={rx - ts:.6f}" if isinstance(ts, (int, float)) else ""
            self.log.log("RX", alert_id, msg_id, level, self.sub_id, retry, latency)
            print(f"[{self.sub_id}] {level.upper()} #{msg_id}: {msg.get('text')}")

        if needs_ack:                        # always ACK, even duplicates
            try:
                self.ack_sock.sendto(
                    protocol.encode(protocol.ACK, msg_id=msg_id, id=self.sub_id),
                    (self.server_ip, config.PORT_ACK))
                self.log.log("ACK_SENT", alert_id, msg_id, level, self.sub_id, retry)
            except OSError:
                pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("id")
    ap.add_argument("--server", default=config.SERVER_IP)
    ap.add_argument("--offset", type=int, default=0,
                    help="port offset; only needed to run several "
                         "subscribers on one machine")
    ap.add_argument("--levels", nargs="+", choices=config.LEVELS,
                    default=list(config.LEVELS))
    ap.add_argument("--log", default=None)
    ap.add_argument("--drop-first", action="store_true",
                    help="ignore first copy of critical/high messages (testing)")
    args = ap.parse_args()

    sub = Subscriber(args.id, args.server, args.offset, args.levels,
                     args.log or f"{args.id}_log.csv", args.drop_first)
    if not sub.register():
        print("Registration failed: is the server running?")
        return
    print(f"[{args.id}] registered, listening on levels {args.levels}")
    for lvl in args.levels:
        threading.Thread(target=sub.listen, args=(lvl,), daemon=True).start()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        sub.unregister()
        print("Unregistered, bye")


if __name__ == "__main__":
    main()