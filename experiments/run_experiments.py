#!/usr/bin/env python3
"""
Runs every (run x load x mode) combination and saves raw logs.
    sudo python3 run_experiments.py --ryu ~/ryu-venv39/bin/ryu-manager
"""
import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP, SDN = ROOT / "app", ROOT / "sdn"
sys.path.insert(0, str(ROOT / "topology"))

from mininet.log import setLogLevel      # noqa: E402
import topo                              # noqa: E402

LOADS = {"none": 0, "moderate": 9, "heavy": 20}   # Mbps of UDP background
RATE = 10_000_000                                  # bottleneck, bit/s
SERVER_IP, SINK_IP, BG_PORT = "10.0.0.1", "10.0.0.11", 5201
DEVNULL = subprocess.DEVNULL


def run_once(mode, load_name, run_idx, a):
    load = LOADS[load_name]
    out = Path(a.outdir) / f"{mode}_{load_name}_run{run_idx}"
    out.mkdir(parents=True, exist_ok=True)
    subprocess.run(["mn", "-c"], stdout=DEVNULL, stderr=DEVNULL)

    env = dict(os.environ, MODE=mode, LINK_BPS=str(RATE))
    ctrl_log = open(out / "controller.log", "w")
    ctrl = subprocess.Popen([a.ryu, "controller.py"], cwd=SDN, env=env,
                            stdout=ctrl_log, stderr=subprocess.STDOUT)
    time.sleep(4)
    if ctrl.poll() is not None:
        raise RuntimeError(f"controller died, see {out / 'controller.log'}")

    net, procs = None, []

    def spawn(host, cmd, logname):
        f = open(out / logname, "w")
        p = host.popen(cmd, cwd=str(APP), stdout=f, stderr=subprocess.STDOUT)
        procs.append((p, f))
        return p

    try:
        net, h = topo.build(a.subs)
        time.sleep(2)                                  # switches connect
        subprocess.run(
            ["bash", str(SDN / "qos_setup.sh"),
             "base" if mode == "baseline" else "prio", *topo.BOTTLENECK_IFACES],
            env=dict(os.environ, RATE=str(RATE)), check=True, stdout=DEVNULL)
        net.pingAll()                                  # controller learns hosts

        cmd = ["python3", "server.py", "--log", str(out / "server_log.csv")]
        if a.ack_timeout:
            cmd = ["env", f"ACK_TIMEOUT={a.ack_timeout}"] + cmd
        spawn(h["server"], cmd, "server_stdout.txt")
        time.sleep(1)
        for s in h["subs"]:
            spawn(s, ["python3", "subscriber.py", s.name, "--server", SERVER_IP,
                      "--log", str(out / f"{s.name}_log.csv")],
                  f"{s.name}_stdout.txt")
        time.sleep(3)                                  # registrations

        alert_time = a.alerts * a.interval
        iperf = None
        if load > 0:
            spawn(h["bg_sink"], ["iperf", "-s", "-u", "-p", str(BG_PORT)],
                  "iperf_server.txt")
            time.sleep(1)
            iperf = spawn(h["bg_src"],
                          ["iperf", "-c", SINK_IP, "-u", "-b", f"{load}M",
                           "-p", str(BG_PORT), "-i", "1",
                           "-t", str(int(a.warmup + alert_time + 4))],
                          "iperf_client.txt")
            time.sleep(a.warmup)                       # let congestion build

        for level in ("critical", "high", "normal"):
            h["server"].popen(
                ["python3", "alert_trigger.py", level, f"{level} test",
                 "--count", str(a.alerts), "--interval", str(a.interval),
                 "--server", SERVER_IP],
                cwd=str(APP), stdout=DEVNULL, stderr=DEVNULL)
        time.sleep(alert_time + a.settle)              # sending + retries finish
        if iperf:
            try:
                iperf.wait(timeout=30)
            except subprocess.TimeoutExpired:
                pass

        for sw in ("s1", "s2"):                        # evidence for the report
            for what in ("dump-flows", "queue-stats"):
                r = subprocess.run(["ovs-ofctl", "-O", "OpenFlow13", what, sw],
                                   capture_output=True, text=True)
                (out / f"{sw}_{what}.txt").write_text(r.stdout + r.stderr)

        for iface in topo.BOTTLENECK_IFACES:           # where packets were dropped
            for what in ("class", "qdisc"):
                r = subprocess.run(["tc", "-s", what, "show", "dev", iface],
                                   capture_output=True, text=True)
                (out / f"tc_{iface}_{what}.txt").write_text(r.stdout + r.stderr)

        (out / "meta.json").write_text(json.dumps({
            "mode": mode, "load": load_name, "load_mbps": load,
            "run": run_idx, "subs": a.subs, "alerts": a.alerts,
            "interval": a.interval, "rate_bps": RATE,
            "ack_timeout": a.ack_timeout}))
    finally:
        for p, _ in procs:
            p.terminate()
        for p, f in procs:
            try:
                p.wait(timeout=3)
            except subprocess.TimeoutExpired:
                p.kill()
            f.close()
        if net:
            net.stop()
        ctrl.terminate()
        try:
            ctrl.wait(timeout=5)
        except subprocess.TimeoutExpired:
            ctrl.kill()
        ctrl_log.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ryu", required=True, help="path to ryu-manager in the venv")
    ap.add_argument("--modes", nargs="+", default=["baseline", "static", "dynamic"])
    ap.add_argument("--loads", nargs="+", default=list(LOADS), choices=list(LOADS))
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--subs", type=int, default=4)
    ap.add_argument("--alerts", type=int, default=40, help="messages per level")
    ap.add_argument("--interval", type=float, default=0.25)
    ap.add_argument("--warmup", type=float, default=3)
    ap.add_argument("--settle", type=float, default=10)
    ap.add_argument("--outdir", default=str(ROOT / "results"))
    ap.add_argument("--ack-timeout", type=float, default=None)
    a = ap.parse_args()

    if os.geteuid() != 0:
        sys.exit("Run with sudo")
    a.ryu = os.path.abspath(os.path.expanduser(a.ryu))
    setLogLevel("warning")

    for run in range(1, a.runs + 1):          # runs outermost: spreads drift evenly
        for load in a.loads:
            for mode in a.modes:
                print(f"=== run {run} | load {load} | mode {mode}", flush=True)
                try:
                    run_once(mode, load, run, a)
                except Exception as e:
                    print("   FAILED:", e, flush=True)


if __name__ == "__main__":
    main()