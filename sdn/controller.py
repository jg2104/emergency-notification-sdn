"""
Ryu controller. Mode is chosen with an environment variable:
    MODE=baseline | static | dynamic   (default: baseline)
Run:  MODE=static ryu-manager controller.py
REST API listens on port 8080 (Ryu's default).
"""
import json
import os
import time

from ryu.app.wsgi import ControllerBase, WSGIApplication, route
from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import CONFIG_DISPATCHER, MAIN_DISPATCHER, set_ev_cls
from ryu.lib import hub
from ryu.lib.packet import ethernet, ether_types, packet
from ryu.ofproto import ofproto_v1_3
from webob import Response

MODE = os.environ.get("MODE", "baseline").lower()

# ---- must match config.py ----
PORT_CRITICAL = 5002
PORT_HIGH = 5006
PORT_ACK = 5003
BG_PORT = 5201                       # iperf background traffic: iperf -p 5201

# ---- queue ids (must match qos_setup.sh) ----
QUEUE_BE, QUEUE_HIGH, QUEUE_CRIT = 0, 1, 2

# ---- flow priorities ----
PRIO_FWD, PRIO_BG, PRIO_HIGH, PRIO_CRIT = 10, 80, 90, 100

# ---- dynamic mode settings (tune these) ----
METER_ID = 1
UNTHROTTLED_KBPS = 100000            # above link capacity = effectively no limit
THROTTLED_KBPS = 2000                # background limit while throttling
LINK_BPS = float(os.environ.get("LINK_BPS", 10_000_000))
BN_DPID = int(os.environ.get("BN_DPID", 1))     # switch with the bottleneck port
BN_PORT = int(os.environ.get("BN_PORT", 3))     # its port toward the other switch
UTIL_HIGH, UTIL_LOW = 0.80, 0.50     # hysteresis thresholds
HOLD_S = 5.0                         # minimum time to stay throttled
POLL_S = 1.0

# (udp dst port, queue, flow priority)
CLASSES = [
    (PORT_CRITICAL, QUEUE_CRIT, PRIO_CRIT),
    (PORT_ACK,      QUEUE_CRIT, PRIO_CRIT),
    (PORT_HIGH,     QUEUE_HIGH, PRIO_HIGH),
]


class EmergencyController(app_manager.RyuApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]
    _CONTEXTS = {"wsgi": WSGIApplication}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        kwargs["wsgi"].register(AlertAPI, {"app": self})
        self.datapaths = {}
        self.mac_to_port = {}        # dpid -> {mac: port}
        self.installed = {}          # (dpid, mac) -> out_port already installed
        self.active_alerts = set()
        self.congested = False
        self.throttled = False
        self.hold_until = 0.0
        self.last_sample = None      # (tx_bytes, time)
        self.logger.info("=== MODE: %s ===", MODE)
        if MODE == "dynamic":
            hub.spawn(self.monitor)

    # ------------------------------------------------------------ helpers
    def add_flow(self, dp, priority, match, actions, meter=None):
        ofp, parser = dp.ofproto, dp.ofproto_parser
        inst = []
        if meter is not None:
            inst.append(parser.OFPInstructionMeter(meter))
        inst.append(parser.OFPInstructionActions(ofp.OFPIT_APPLY_ACTIONS, actions))
        dp.send_msg(parser.OFPFlowMod(datapath=dp, priority=priority,
                                      match=match, instructions=inst))

    def send_meter(self, dp, kbps, command):
        ofp, parser = dp.ofproto, dp.ofproto_parser
        bands = [parser.OFPMeterBandDrop(rate=kbps, burst_size=max(1, kbps // 10))]
        dp.send_msg(parser.OFPMeterMod(datapath=dp, command=command,
                                       flags=ofp.OFPMF_KBPS,
                                       meter_id=METER_ID, bands=bands))

    # ------------------------------------------------------------ switch setup
    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def switch_features(self, ev):
        dp = ev.msg.datapath
        ofp, parser = dp.ofproto, dp.ofproto_parser
        self.datapaths[dp.id] = dp
        self.mac_to_port.setdefault(dp.id, {})
        # table-miss: send unknown packets to the controller
        self.add_flow(dp, 0, parser.OFPMatch(),
                      [parser.OFPActionOutput(ofp.OFPP_CONTROLLER,
                                              ofp.OFPCML_NO_BUFFER)])
        if MODE == "dynamic":
            self.send_meter(dp, UNTHROTTLED_KBPS, ofp.OFPMC_ADD)
        self.logger.info("switch %s connected", dp.id)

    @set_ev_cls(ofp_event.EventOFPErrorMsg, MAIN_DISPATCHER)
    def error_handler(self, ev):
        m = ev.msg
        self.logger.error("OpenFlow error type=%s code=%s (switch %s)",
                          m.type, m.code, m.datapath.id)

    # ------------------------------------------------------------ learning switch
    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def packet_in(self, ev):
        msg = ev.msg
        dp = msg.datapath
        ofp, parser = dp.ofproto, dp.ofproto_parser
        in_port = msg.match["in_port"]

        eth = packet.Packet(msg.data).get_protocols(ethernet.ethernet)[0]
        if eth.ethertype == ether_types.ETH_TYPE_LLDP:
            return
        src, dst = eth.src, eth.dst

        table = self.mac_to_port.setdefault(dp.id, {})
        table[src] = in_port

        if dst in table:
            out_port = table[dst]
            self.install_dst_rules(dp, dst, out_port)
        else:
            out_port = ofp.OFPP_FLOOD

        dp.send_msg(parser.OFPPacketOut(
            datapath=dp, buffer_id=ofp.OFP_NO_BUFFER, in_port=in_port,
            actions=[parser.OFPActionOutput(out_port)], data=msg.data))

    def install_dst_rules(self, dp, dst, out_port):
        """Once a destination's port is known, install every rule for it."""
        if self.installed.get((dp.id, dst)) == out_port:
            return
        self.installed[(dp.id, dst)] = out_port
        parser = dp.ofproto_parser

        # plain forwarding (all modes)
        self.add_flow(dp, PRIO_FWD, parser.OFPMatch(eth_dst=dst),
                      [parser.OFPActionOutput(out_port)])
        if MODE == "baseline":
            return

        # classification: set queue AND output (forgetting output = dropped)
        for udp_port, queue, prio in CLASSES:
            match = parser.OFPMatch(eth_type=0x0800, ip_proto=17,
                                    udp_dst=udp_port, eth_dst=dst)
            self.add_flow(dp, prio, match,
                          [parser.OFPActionSetQueue(queue),
                           parser.OFPActionOutput(out_port)])

        # background traffic goes through the meter (dynamic mode only)
        if MODE == "dynamic":
            match = parser.OFPMatch(eth_type=0x0800, ip_proto=17,
                                    udp_dst=BG_PORT, eth_dst=dst)
            self.add_flow(dp, PRIO_BG, match,
                          [parser.OFPActionOutput(out_port)], meter=METER_ID)

    # ------------------------------------------------------------ dynamic logic
    def update_throttle(self):
        if MODE != "dynamic":
            return
        want = bool(self.active_alerts) or self.congested
        if want == self.throttled:
            return
        self.throttled = want
        kbps = THROTTLED_KBPS if want else UNTHROTTLED_KBPS
        for dp in self.datapaths.values():
            self.send_meter(dp, kbps, dp.ofproto.OFPMC_MODIFY)
        self.logger.info("background %s (alerts=%s congested=%s)",
                         "THROTTLED" if want else "RELEASED",
                         sorted(self.active_alerts), self.congested)

    def monitor(self):
        while True:
            dp = self.datapaths.get(BN_DPID)
            if dp is not None:
                dp.send_msg(dp.ofproto_parser.OFPPortStatsRequest(dp, 0, BN_PORT))
            hub.sleep(POLL_S)

    @set_ev_cls(ofp_event.EventOFPPortStatsReply, MAIN_DISPATCHER)
    def port_stats_reply(self, ev):
        dp = ev.msg.datapath
        if dp.id != BN_DPID:
            return
        now = time.monotonic()
        for st in ev.msg.body:
            if st.port_no != BN_PORT:
                continue
            if self.last_sample is not None:
                last_bytes, last_t = self.last_sample
                util = (st.tx_bytes - last_bytes) * 8 / (now - last_t) / LINK_BPS
                self.on_utilization(util, now)
            self.last_sample = (st.tx_bytes, now)

    def on_utilization(self, util, now):
        if not self.congested and util > UTIL_HIGH:
            self.congested = True
            self.hold_until = now + HOLD_S
            self.logger.info("congestion detected (util=%.2f)", util)
        elif self.congested and util < UTIL_LOW and now >= self.hold_until:
            self.congested = False
            self.logger.info("congestion cleared (util=%.2f)", util)
        self.update_throttle()


class AlertAPI(ControllerBase):
    """REST: POST /alert/start, POST /alert/end, GET /status"""

    def __init__(self, req, link, data, **config):
        super().__init__(req, link, data, **config)
        self.app = data["app"]

    def _reply(self, obj, status=200):
        return Response(status=status, content_type="application/json",
                        body=json.dumps(obj).encode("utf-8"))

    def _alert_id(self, req):
        try:
            return str(json.loads(req.body or b"{}").get("alert_id", "unknown"))
        except ValueError:
            return None

    @route("alert_start", "/alert/start", methods=["POST"])
    def start(self, req, **kw):
        aid = self._alert_id(req)
        if aid is None:
            return self._reply({"error": "bad json"}, 400)
        self.app.active_alerts.add(aid)
        self.app.logger.info("alert START %s", aid)
        self.app.update_throttle()
        return self._reply({"ok": True, "alert_id": aid})

    @route("alert_end", "/alert/end", methods=["POST"])
    def end(self, req, **kw):
        aid = self._alert_id(req)
        if aid is None:
            return self._reply({"error": "bad json"}, 400)
        self.app.active_alerts.discard(aid)
        self.app.logger.info("alert END %s", aid)
        self.app.update_throttle()
        return self._reply({"ok": True, "alert_id": aid})

    @route("status", "/status", methods=["GET"])
    def status(self, req, **kw):
        a = self.app
        return self._reply({"mode": MODE, "throttled": a.throttled,
                            "congested": a.congested,
                            "active_alerts": sorted(a.active_alerts)})