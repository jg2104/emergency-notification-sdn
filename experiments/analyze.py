#!/usr/bin/env python3
import csv
import json
import re
import statistics
import sys
from collections import defaultdict
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

LEVELS = ("critical", "high", "normal")
MODE_ORDER = ("baseline", "static", "dynamic")
LOAD_ORDER = ("none", "moderate", "heavy")
NAN = float("nan")


def read_csv(path):
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def pct(values, p):
    if not values:
        return NAN
    s = sorted(values)
    k = (len(s) - 1) * p / 100
    lo = int(k)
    hi = min(lo + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def parse_iperf(path):
    """Throughput from the server report line that the iperf client prints."""
    if not path.exists():
        return NAN
    rx = re.compile(r"([\d.]+)\s+([KMG])bits/sec\s+[\d.]+\s+ms\s+\d+\s*/\s*\d+")
    last = None
    for line in path.read_text(errors="ignore").splitlines():
        m = rx.search(line)
        if m:
            last = m
    if not last:
        return NAN
    return float(last.group(1)) * {"K": 1e-3, "M": 1.0, "G": 1e3}[last.group(2)]


def analyze_run(d):
    meta = json.loads((d / "meta.json").read_text())
    sent, retries = {}, defaultdict(int)
    for r in read_csv(d / "server_log.csv"):
        if r["event"] != "SEND":
            continue
        if r["retry"] == "0":
            sent[(r["msg_id"], r["subscriber"])] = r["level"]
        else:
            retries[r["level"]] += 1

    first_rx, any_rx, lat = set(), set(), defaultdict(list)
    for f in d.glob("sub*_log.csv"):
        for r in read_csv(f):
            key = (r["msg_id"], r["subscriber"])
            if r["event"] in ("RX", "DUPLICATE") and r["retry"] == "0":
                first_rx.add(key)
            if r["event"] == "RX":
                any_rx.add(key)
                m = re.search(r"latency=([\d.eE+-]+)", r["detail"])
                if m:
                    lat[r["level"]].append(float(m.group(1)) * 1000)   # ms

    res = dict(meta)
    for lvl in LEVELS:
        keys = [k for k, l in sent.items() if l == lvl]
        n = len(keys)
        res[f"{lvl}_sent"] = n
        res[f"{lvl}_tx_loss_pct"] = (100 * (1 - sum(k in first_rx for k in keys) / n)
                                     if n else NAN)
        res[f"{lvl}_delivery_pct"] = (100 * sum(k in any_rx for k in keys) / n
                                      if n else NAN)
        res[f"{lvl}_retries_per_msg"] = retries[lvl] / n if n else NAN
        v = lat[lvl]
        res[f"{lvl}_mean_ms"] = statistics.mean(v) if v else NAN
        res[f"{lvl}_p95_ms"] = pct(v, 95)
        res[f"{lvl}_max_ms"] = max(v) if v else NAN
    res["bg_mbps"] = 0.0 if meta["load_mbps"] == 0 else parse_iperf(d / "iperf_client.txt")
    return res


def mean_std(vals):
    vals = [v for v in vals if v == v]
    if not vals:
        return NAN, NAN
    return statistics.mean(vals), (statistics.stdev(vals) if len(vals) > 1 else 0.0)


PLOTS = [
    ("critical_p95_ms", "Critical latency, 95th percentile (ms)"),
    ("critical_mean_ms", "Critical latency, mean (ms)"),
    ("high_p95_ms", "High latency, 95th percentile (ms)"),
    ("normal_p95_ms", "Normal latency, 95th percentile (ms)"),
    ("critical_tx_loss_pct", "Critical first-attempt loss (%)"),
    ("critical_delivery_pct", "Critical eventual delivery (%)"),
    ("normal_delivery_pct", "Normal delivery (%)  [no ACKs]"),
    ("critical_retries_per_msg", "Critical retransmissions per message"),
    ("bg_mbps", "Background throughput (Mbps)"),
]


def main():
    root = Path(sys.argv[1] if len(sys.argv) > 1 else "../results")
    groups = defaultdict(list)
    for d in sorted(root.iterdir()):
        if d.is_dir() and (d / "meta.json").exists() and (d / "server_log.csv").exists():
            r = analyze_run(d)
            groups[(r["mode"], r["load"])].append(r)
    if not groups:
        sys.exit("no completed runs found")

    modes = [m for m in MODE_ORDER if any(k[0] == m for k in groups)]
    loads = [l for l in LOAD_ORDER if any(k[1] == l for k in groups)]
    metrics = [k for k, _ in PLOTS] + [
        f"{l}_{s}" for l in LEVELS for s in ("sent", "tx_loss_pct", "delivery_pct",
                                              "retries_per_msg", "mean_ms",
                                              "p95_ms", "max_ms")]
    metrics = list(dict.fromkeys(metrics))

    with open(root / "summary.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["mode", "load", "runs"] + [f"{m}{s}" for m in metrics
                                               for s in ("", "_std")])
        for mode in modes:
            for load in loads:
                rs = groups.get((mode, load))
                if not rs:
                    continue
                row = [mode, load, len(rs)]
                for m in metrics:
                    mu, sd = mean_std([r[m] for r in rs])
                    row += [f"{mu:.3f}", f"{sd:.3f}"]
                w.writerow(row)

    (root / "plots").mkdir(exist_ok=True)
    width = 0.8 / max(len(modes), 1)
    for metric, title in PLOTS:
        fig, ax = plt.subplots(figsize=(7, 4))
        for i, mode in enumerate(modes):
            stats = [mean_std([r[metric] for r in groups.get((mode, l), [])])
                     for l in loads]
            ax.bar([j + i * width for j in range(len(loads))],
                   [s[0] for s in stats], width, yerr=[s[1] for s in stats],
                   capsize=3, label=mode)
        ax.set_xticks([j + width * (len(modes) - 1) / 2 for j in range(len(loads))])
        ax.set_xticklabels(loads)
        ax.set_xlabel("background load")
        ax.set_ylabel(title)
        ax.set_title(title)
        ax.legend()
        fig.tight_layout()
        fig.savefig(root / "plots" / f"{metric}.png", dpi=150)
        plt.close(fig)
    print(f"wrote {root / 'summary.csv'} and {len(PLOTS)} plots")


if __name__ == "__main__":
    main()