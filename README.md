# Emergency Notification Network (UDP + SDN)

A UDP-based emergency notification system running in Mininet, with a Ryu
(OpenFlow 1.3) controller that identifies emergency traffic and forwards it
with priority, even when the network is congested.

> Course mini project: SDN + socket programming (TCP/UDP + Mininet + SDN controller).

## How it works

- A **server** sends alerts over UDP to registered **subscribers**.
- Three priority levels: **critical** (UDP 5002), **high** (5006), **normal** (5001).
  Critical and high alerts are acknowledged and retransmitted with backoff; normal alerts are fire-and-forget.
- The **controller** classifies traffic by UDP port and puts it into priority queues on Open vSwitch.
- Three modes are compared under congestion:
  1. **baseline**: plain learning switch, one shared queue
  2. **static**: permanent classification rules and three priority queues
  3. **dynamic**: static, plus throttling of background traffic while an alert is active or the bottleneck is congested
- Measured: notification latency, packet loss, delivery success, retransmissions, background throughput.

```
alert_trigger -> SERVER --REST--> RYU CONTROLLER --OpenFlow--> OVS switches
                   |  ^                                          |
        UDP alerts |  | ACKs                     bottleneck link (s1 - s2)
                   v  |                                          |
               SUBSCRIBERS                       iperf background traffic
```

Details: [docs/architecture.md](docs/architecture.md)

## Repository layout

```
app/          server.py, subscriber.py, alert_trigger.py, protocol.py, config.py, logger.py
sdn/          controller.py (Ryu app), qos_setup.sh (OVS queues)
topology/     topo.py (Mininet topology)
experiments/  run_experiments.py (runner), analyze.py (metrics and plots)
docs/         architecture, protocol, test plan, report
evidence/     sample runs, summaries and plots used in the report
```

## Setup

See **[SETUP.md](SETUP.md)** for the full installation guide, versions, and troubleshooting.
Short version: Ubuntu VM with Mininet, Open vSwitch, iperf (v2) and Python 3, plus Ryu 4.34 in a separate **Python 3.9** virtual environment.

## Configuration

Ports, timeouts and retry limits are in `app/config.py`. For Mininet runs:
`SERVER_IP = "10.0.0.1"` and `CONTROLLER_URL = "http://10.0.0.254:8080"`.
The ACK timeout can be overridden with the `ACK_TIMEOUT` environment variable.

## Manual demo

Start in this order, using three terminals. Quote paths that contain spaces.

**1. Controller** (no sudo; `MODE` is `baseline`, `static` or `dynamic`):
```bash
source ~/ryu-venv39/bin/activate
cd sdn
MODE=static ryu-manager controller.py
```

**2. Topology:**
```bash
sudo mn -c
cd topology
sudo python3 topo.py
```

**3. At the `mininet>` prompt** (replace `<repo>` with the repository path):
```
pingall
sh env RATE=10000000 bash <repo>/sdn/qos_setup.sh prio s1-eth3 s2-eth1
h1 python3 <repo>/app/server.py --log /tmp/server.csv > /tmp/server.out 2>&1 &
sub1 python3 <repo>/app/subscriber.py sub1 --server 10.0.0.1 --log /tmp/sub1.csv > /tmp/sub1.out 2>&1 &
sub2 python3 <repo>/app/subscriber.py sub2 --server 10.0.0.1 --log /tmp/sub2.csv > /tmp/sub2.out 2>&1 &
h1 python3 <repo>/app/alert_trigger.py critical "test alert" --count 5 --interval 1 --server 10.0.0.1
```

Use `qos_setup.sh base ...` instead of `prio` for baseline mode. Add `--drop-first` to a subscriber to see retransmission in the logs.

Optional congestion while sending alerts:
```
hb2 iperf -s -u -p 5201 &
hb1 iperf -c 10.0.0.11 -u -b 20M -p 5201 -t 20 &
```

Useful checks:
```bash
sudo ovs-ofctl -O OpenFlow13 dump-flows s1      # classification rules and counters
sudo ovs-ofctl -O OpenFlow13 queue-stats s1     # per-queue traffic
curl http://127.0.0.1:8080/status               # controller state
```

## Run the experiments

```bash
cd experiments
sudo python3 run_experiments.py --ryu ~/ryu-venv39/bin/ryu-manager --runs 3
sudo chown -R $USER ../results
python3 analyze.py ../results
```

Runs every combination of mode (baseline, static, dynamic), load (none, moderate, heavy) and repetition.
Output: `results/summary.csv` and `results/plots/*.png`.

Useful options:

| Option | Meaning |
|---|---|
| `--modes static` | Only some modes |
| `--loads heavy` | Only some loads |
| `--runs 1` | Number of repetitions |
| `--subs 6` | Number of subscribers |
| `--ack-timeout 0.1` | Short ACK timeout (retransmission scenario) |
| `--outdir ../results_x` | Output folder |

Each run folder contains server and subscriber logs, the controller log, flow tables, queue statistics, `tc` counters and iperf output.

## Test cases

See [docs/test-plan.md](docs/test-plan.md). They cover delivery to multiple subscribers, ACK and retransmission, duplicate registration, unsubscribe, a subscriber failing mid-alert, malformed packets, flow and queue verification, and congestion in each mode.

## Results

See [docs/report.md](docs/report.md), `evidence/summaries/` and `evidence/plots/`.
