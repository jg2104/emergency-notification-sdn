import argparse
import socket

import config
import protocol


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("level", choices=config.LEVELS)
    ap.add_argument("text")
    ap.add_argument("--count", type=int, default=1)
    ap.add_argument("--interval", type=float, default=1.0)
    ap.add_argument("--server", default=config.SERVER_IP)
    args = ap.parse_args()

    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(2.0)
    s.sendto(protocol.encode(protocol.TRIGGER, level=args.level, text=args.text,
                             count=args.count, interval=args.interval),
             (args.server, config.PORT_TRIGGER))
    try:
        data, _ = s.recvfrom(config.BUF)
        print(protocol.decode(data))
    except (socket.timeout, ConnectionResetError):
        print("No reply from server")


if __name__ == "__main__":
    main()