#!/usr/bin/env python3
"""
2 switches, 1 server, N subscribers, 2 background hosts.
  s1: port1=server, port2=bg source, port3=trunk (bottleneck), port4=root-NS link
  s2: port1=trunk (bottleneck), ports 2..N+1=subscribers, port N+2=bg sink
Links are NOT shaped here; the bottleneck rate is set on OVS by qos_setup.sh.
The root-NS interface (10.0.0.254) lets hosts reach the controller's REST API.
"""
from mininet.cli import CLI
from mininet.log import setLogLevel
from mininet.net import Mininet
from mininet.node import OVSSwitch, RemoteController
from mininet.node import Node

CONTROLLER_IP, CONTROLLER_PORT = "127.0.0.1", 6653
ROOT_IP = "10.0.0.254/24"
BOTTLENECK_IFACES = ("s1-eth3", "s2-eth1")

def connectToRootNS(network, switch, ip, routes):
    """Connect the switch to the VM's root namespace so hosts can reach the
    controller's REST API. Also starts the network."""
    root = Node("root", inNamespace=False)
    intf = network.addLink(root, switch).intf1
    root.setIP(ip, intf=intf)
    network.start()
    for route in routes:
        root.cmd("route add -net " + route + " dev " + str(intf))

def build(num_subs=4):
    net = Mininet(controller=None, switch=OVSSwitch, autoSetMacs=False)
    net.addController("c0", controller=RemoteController,
                      ip=CONTROLLER_IP, port=CONTROLLER_PORT)
    s1 = net.addSwitch("s1", protocols="OpenFlow13")
    s2 = net.addSwitch("s2", protocols="OpenFlow13")

    server = net.addHost("h1", ip="10.0.0.1/24", mac="00:00:00:00:00:01")
    bg_src = net.addHost("hb1", ip="10.0.0.10/24", mac="00:00:00:00:00:0a")
    bg_sink = net.addHost("hb2", ip="10.0.0.11/24", mac="00:00:00:00:00:0b")
    subs = [net.addHost(f"sub{i}", ip=f"10.0.0.{100 + i}/24",
                        mac=f"00:00:00:00:01:{i:02x}")
            for i in range(1, num_subs + 1)]

    net.addLink(server, s1, port2=1)
    net.addLink(bg_src, s1, port2=2)
    net.addLink(s1, s2, port1=3, port2=1)            # the bottleneck
    for i, h in enumerate(subs):
        net.addLink(h, s2, port2=2 + i)
    net.addLink(bg_sink, s2, port2=2 + num_subs)

    connectToRootNS(net, s1, ROOT_IP, [])            # also starts the network
    return net, {"server": server, "bg_src": bg_src,
                 "bg_sink": bg_sink, "subs": subs}


if __name__ == "__main__":
    setLogLevel("info")
    net, _ = build(4)
    CLI(net)
    net.stop()