import csv
import threading

import protocol

HEADER = ["ts", "event", "alert_id", "msg_id", "level",
          "subscriber", "retry", "detail"]

#CSV logger
class Logger:
    def __init__(self, path, echo=False):
        self.lock = threading.Lock()
        self.echo = echo
        self.f = open(path, "w", newline="", buffering=1)
        self.w = csv.writer(self.f)
        self.w.writerow(HEADER)

    def log(self, event, alert_id="", msg_id="", level="",
            subscriber="", retry="", detail=""):
        row = [f"{protocol.now():.6f}", event, alert_id, msg_id, level,
               subscriber, retry, detail]
        with self.lock:
            self.w.writerow(row)
            if self.echo:
                print(" | ".join(str(x) for x in row[1:] if x != ""))