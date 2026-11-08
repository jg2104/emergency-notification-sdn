# Architecture: Emergency Notification Network (UDP + SDN)

## 1. Problem and objectives

Emergency alerts must reach many subscribers quickly, even when the network is congested by ordinary traffic. This project builds a UDP notification system inside a Mininet network managed by an SDN controller (Ryu, OpenFlow 1.3). The network recognizes emergency traffic and forwards it with priority.

**Objectives**
1. Deliver alerts to registered subscribers over UDP.
2. Let subscribers register and unregister dynamically.
3. Classify traffic into three priority levels (critical, high, normal).
4. Enforce priority with flow rules and Open vSwitch queues.
5. Provide delivery confirmation (ACK and retransmission) where required.
6. Generate congestion and measure latency, loss, delivery success and throughput.
7. Compare three modes: baseline, static SDN QoS and dynamic SDN QoS.

## 2. System overview

```
 alert_trigger -UDP 5004->  SERVER ----REST/HTTP 8080---->  RYU CONTROLLER
                            ^    |                              |      ^
        REGISTER UDP 5000 --+    | alerts UDP 5001/5002/5006    |      | port stats
        ACK      UDP 5003 -------+ (data plane)        OpenFlow 6653   |
                                 |                              v      |
                       +---------+--------------------------------------+
                       |  OVS switches s1, s2 == bottleneck link ==     |
                       |  queues: critical / high / best-effort         |
                       +---------+-------------------+------------------+
                            SUBSCRIBERS          iperf hosts (background load)
```

## 3. Components

| Component | File | Responsibility |
|---|---|---|
| Alert server | `app/server.py` | Subscriber table, alert sending, ACK tracking, retransmission, REST notifications to the controller |
| Subscriber | `app/subscriber.py` | Registers, receives alerts, ACKs critical/high, suppresses duplicates, logs receive time |
| Alert trigger | `app/alert_trigger.py` | Fires single or repeating alerts through the server's control port |
| Protocol and config | `app/protocol.py`, `app/config.py` | Message encode/decode (JSON), shared ports, timeouts, retry limits |
| Logger | `app/logger.py` | Thread-safe CSV logs used by the analysis |
| Controller | `sdn/controller.py` | Learning switch, classification rules, REST API, congestion monitor, throttling |
| Queue setup | `sdn/qos_setup.sh` | Creates OVS egress queues and the bottleneck rate |
| Topology | `topology/topo.py` | Mininet network with a bottleneck link |
| Experiments | `experiments/run_experiments.py`, `experiments/analyze.py` | Scenario runner, metrics and plots |

## 4. Topology

- **Switches:** s1 (server side) and s2 (subscriber side), joined by the bottleneck link (s1-eth3 and s2-eth1).
- **Hosts:** server `h1` (10.0.0.1), background source `hb1` (10.0.0.10), background sink `hb2` (10.0.0.11), subscribers `sub1..subN` (10.0.0.101 and up, default 4, up to 6 or more).
- Background traffic crosses the same bottleneck as the alerts.
- The controller runs outside Mininet as a remote controller on 127.0.0.1:6653.
- **Root-namespace link:** s1 also connects to the VM's root namespace as 10.0.0.254, so the server (inside Mininet) can reach the controller's REST API at `http://10.0.0.254:8080`.
- **Where the bottleneck is shaped:** links are not shaped in Mininet. The bottleneck capacity (default 10 Mbit/s) is set with HTB on the OVS ports, so Mininet's `tc` shaping and OVS queues do not conflict.

## 5. Priority levels and ports

| Level | Meaning | ACK | Queue | UDP port |
|---|---|---|---|---|
| Critical | Life-safety alerts | Yes, up to 5 retries | Highest (queue 2) | 5002 |
| High | Important warnings | Yes, up to 3 retries | Middle (queue 1) | 5006 |
| Normal | Routine information | No | Best effort (queue 0) | 5001 |
| Control | ACKs | n/a | Highest (queue 2) | 5003 |

| Purpose | Protocol | Port |
|---|---|---|
| Registration | UDP | 5000 |
| Alert trigger | UDP | 5004 |
| Background traffic (iperf) | UDP | 5201 |
| Controller REST | TCP | 8080 |
| OpenFlow | TCP | 6653 |

iperf's default port (5001) is the normal alert port, so background traffic is always run on 5201.

## 6. Message flow

| # | Step | From to | Protocol, port | Content |
|---|---|---|---|---|
| 1 | Trigger | trigger tool to server | UDP 5004 | level, text, count, interval |
| 2 | Register | subscriber to server | UDP 5000 | id, listening port offset, subscribed levels |
| 3 | Register reply | server to subscriber | UDP 5000 | REGISTER_OK |
| 4 | Alert start | server to controller | HTTP POST 8080 `/alert/start` | alert id |
| 5 | Alert delivery | server to each subscriber | UDP 5002 / 5006 / 5001 | alert id, message id, level, send timestamp, retry count, text |
| 6 | ACK | subscriber to server | UDP 5003 | message id, subscriber id |
| 7 | Retransmit | server to subscriber | UDP 5002 / 5006 | same message id, retry + 1, exponential backoff |
| 8 | Alert end | server to controller | HTTP POST 8080 `/alert/end` | alert id (sent after every delivery thread, including retries, has finished) |
| 9 | Flow and queue rules | controller to switches | OpenFlow 6653 | match UDP port, set queue, output |
| 10 | Port statistics | controller and switches | OpenFlow 6653 | bottleneck byte counters, polled every second (dynamic mode) |

## 7. Application design

**Registration.** A subscriber sends REGISTER and waits for REGISTER_OK, resending if it does not arrive. The server takes the subscriber's IP from the packet source address and stores the ID, endpoint, subscribed levels and delivery statistics. A duplicate registration updates the existing entry. UNREGISTER removes the entry.

**Delivery.** Unicast, one datagram per subscribed subscriber. A repeated alert is a new message with a new message id. A retransmission reuses the message id with a higher retry count.

**Reliability.** Critical and high alerts require an ACK. Normal alerts are fire-and-forget, because ACKs add traffic and delay, which matters most on a congested link, and a missed routine message costs little. Timeout and retry limits per level are configurable. Retries use exponential backoff. The ACK timeout can be overridden with the `ACK_TIMEOUT` environment variable for experiments.

**Duplicates.** Each subscriber remembers seen message ids, ignores duplicate content and always re-ACKs, since a duplicate usually means the first ACK was lost.

**Unreachable subscribers.** When retries run out, the message is logged as undelivered and the server continues with the others. Failures are counted but subscribers are not removed automatically, because congestion and a dead subscriber look the same from the server.

**Concurrency.** Threads handle registration, the trigger port, ACK reception and each (message, subscriber) delivery, so a slow or dead subscriber never blocks others.

**Message format.** JSON text (readable in Wireshark). The format is isolated in `protocol.py`.

**Timing.** Timestamps use a monotonic clock. All Mininet hosts share the VM's kernel, so latency (receive time minus send time) is valid across hosts.

## 8. SDN design

**Responsibilities of each mechanism**

| Mechanism | Used for |
|---|---|
| OVS HTB root | Bottleneck capacity |
| OVS queues | Priority (critical, high, best effort) |
| OpenFlow meter | Throttling background traffic (dynamic mode) |

**Classification** is by UDP destination port. Rules are installed once the controller has learned a destination, and stay in place permanently.

| Rule | Flow priority | Action |
|---|---|---|
| Table miss | 0 | Send to controller |
| Learned forwarding (by destination MAC) | 10 | Output |
| Background traffic, UDP 5201 (dynamic mode) | 80 | Meter, then output |
| High alerts, UDP 5006 | 90 | Set queue 1, output |
| Critical alerts, UDP 5002, and ACKs, UDP 5003 | 100 | Set queue 2, output |

Every classification rule sets both the queue and the output port. Flow priority only decides which rule matches. The queue is what provides preferential service. Queues are created with `ovs-vsctl` in `sdn/qos_setup.sh`, outside the controller.

**Dynamic mode** throttles background traffic (by modifying the meter) when either condition holds:
- an alert is active (set by the server's `/alert/start` and `/alert/end` calls), or
- the bottleneck is congested, measured from port statistics with hysteresis (start above a high utilization threshold, stop below a lower one, with a minimum hold time).

The throttle is released only when neither condition holds.

## 9. Experiment modes

| Mode | Controller behavior | Queues |
|---|---|---|
| Baseline | Learning switch only | One shared queue |
| Static | Baseline plus permanent classification rules | Three queues |
| Dynamic | Static plus alert-driven and congestion-driven throttling | Three queues |

The mode is chosen by the `MODE` environment variable at controller start. Topology, applications, load, alert counts and durations are identical across modes, so differences come only from the SDN logic.

## 10. Measurement

| Metric | How |
|---|---|
| Notification latency (mean, p95, max) | Receive time minus original send time, per level |
| Transmission loss | First-attempt messages never received, per level |
| Delivery success | Messages received at least once (after retries) |
| Retransmissions | Retries per message from server logs |
| Background throughput | iperf receiver report |

Conditions: no load, moderate load, heavy load, repeated several times per mode. An additional scenario uses a short ACK timeout to show retransmission behavior under congestion. Evidence captured per run: flow tables, queue statistics, `tc` counters, controller log, server and subscriber logs.

## 11. Robustness scenarios

- Heavy congestion on the bottleneck (all modes).
- A subscriber stops mid-alert: the server logs the undelivered messages for that subscriber only and continues.
- Duplicate registration, unsubscribe, and malformed packets on every server port.

## 12. Known limitations

- Single machine and emulated links, so results show relative behavior, not real-network numbers.
- Classification by port is simple but couples the network to application ports (DSCP would be more general).
- Dynamic mode can oscillate (throttle, release, congestion, throttle); thresholds and the hold time reduce but do not remove this.
- Priority queues can add delay for lower classes, and dynamic mode trades background throughput for alert latency.
- Delivery for the normal class is not confirmed by design.

## 13. Future work

DSCP marking, multicast delivery with OpenFlow group tables, a link-failure and rerouting scenario, and a binary message format.