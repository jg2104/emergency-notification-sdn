#!/bin/bash
# Configure egress queues on the bottleneck ports.
#   sudo ./qos_setup.sh base  <iface> [<iface> ...]   one queue (baseline mode)
#   sudo ./qos_setup.sh prio  <iface> [<iface> ...]   three queues (static/dynamic)
#   sudo ./qos_setup.sh clear <iface> [<iface> ...]   remove QoS
# RATE env var = bottleneck capacity in bits/s (default 10 Mbps).
# Queue 0 = best-effort, 1 = high, 2 = critical (and ACKs).

MODE=$1; shift
RATE=${RATE:-10000000}

if [ -z "$MODE" ] || [ $# -eq 0 ]; then
    echo "usage: $0 <base|prio|clear> <iface> [<iface> ...]"; exit 1
fi

for IF in "$@"; do
    ovs-vsctl clear port "$IF" qos
done
ovs-vsctl --all destroy qos   >/dev/null 2>&1
ovs-vsctl --all destroy queue >/dev/null 2>&1

[ "$MODE" = "clear" ] && exit 0

for IF in "$@"; do
    if [ "$MODE" = "base" ]; then
        ovs-vsctl set port "$IF" qos=@qos \
          -- --id=@qos create qos type=linux-htb other-config:max-rate=$RATE queues:0=@q0 \
          -- --id=@q0  create queue other-config:max-rate=$RATE
    elif [ "$MODE" = "prio" ]; then
        ovs-vsctl set port "$IF" qos=@qos \
          -- --id=@qos create qos type=linux-htb other-config:max-rate=$RATE \
                 queues:0=@q0 queues:1=@q1 queues:2=@q2 \
          -- --id=@q0 create queue other-config:min-rate=$((RATE/10))   other-config:max-rate=$RATE other-config:priority=3 \
          -- --id=@q1 create queue other-config:min-rate=$((RATE/5))    other-config:max-rate=$RATE other-config:priority=2 \
          -- --id=@q2 create queue other-config:min-rate=$((RATE*3/10)) other-config:max-rate=$RATE other-config:priority=1
    else
        echo "unknown mode $MODE"; exit 1
    fi
    echo "configured $IF ($MODE, $RATE bit/s)"
done