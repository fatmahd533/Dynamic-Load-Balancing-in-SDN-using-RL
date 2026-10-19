"""
Shared Ryu controller engine. SDN_ALGORITHM selects the path-selection
algorithm; this file never changes between algorithms.

Step 4: class-aware routing via cookie-tracked active_udp_links.
Step 5: physical link-failure detection (EventOFPPortStatus) with
proactive flow eviction.
Decision-latency instrumentation: every pick_path() call is timed and
logged as "DECISION_LATENCY: X.XXXXms".

Extension -- anomaly-aware path exclusion: an unsupervised Isolation
Forest (trained offline via experiments/train_anomaly_model.py on this
project's own existing "legitimate" traffic CSVs) scores every
inter-switch link's live tx/rx rate on each stats poll. A link flagged
anomalous by EITHER endpoint's latest reading is excluded from routing
via the same live-adjacency mechanism used for physical failures
(topology.set_link_down/up) -- so anomaly-aware exclusion required no
changes to any algorithm file, exactly like Step 5's failure handling.

A link can now be excluded for two independent reasons (physical
failure, anomaly). self._link_down_reasons tracks which reasons are
currently active per link, so restoring one reason doesn't incorrectly
restore a link still excluded for the other.

If scikit-learn/joblib aren't installed, or no trained model exists yet,
anomaly detection is silently disabled (logged once at startup) --
everything else in the controller works exactly as before.

Dashboard extension -- structured event logging: every decision,
anomaly detect/clear, link up/down, and flow eviction is ALSO written as
one JSON line to events/events_<algorithm>_<start-time>.jsonl, via
_emit_event(). This is purely additive: the human-readable self.logger
calls are unchanged, this just gives the replay dashboard (see
dashboard/) something structured to parse instead of regexing prose.
"""

import os
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import time
import json
import importlib
import importlib.util
import inspect
from collections import Counter, defaultdict

from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import CONFIG_DISPATCHER, MAIN_DISPATCHER, DEAD_DISPATCHER
from ryu.controller.handler import set_ev_cls
from ryu.ofproto import ofproto_v1_3
from ryu.lib.packet import packet, ethernet, arp, ipv4, tcp, udp, ether_types
from ryu.lib import hub

from topology import (HOST_LOCATION, port_toward, neighbor_via_port,
                       static_port_toward, set_link_down, set_link_up)
from stats_logger import StatsLogger
from topology import ADJACENCY

STATS_POLL_INTERVAL = float(os.environ.get('SDN_STATS_POLL', '2'))
BULK_METER_ID = 1
BULK_METER_RATE_KBPS = 5000
ANOMALY_MODEL_PATH = os.path.join('models', 'anomaly_model.joblib')
ANOMALY_CONFIRM = 3    # consecutive anomalous readings before a link is excluded
ANOMALY_CLEAR = 10     # consecutive normal readings before it is restored

ALGORITHM = os.environ.get('SDN_ALGORITHM', 'round_robin')
algo_module = importlib.import_module('algorithms.' + ALGORITHM)


class FatTreeController(app_manager.RyuApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    def __init__(self, *args, **kwargs):
        super(FatTreeController, self).__init__(*args, **kwargs)
        self.datapaths = {}
        self.selector = algo_module.PathSelector()
        self.stats_logger = StatsLogger(ALGORITHM)
        self.logger.info("Controller started with algorithm: %s", ALGORITHM)
        self.logger.info("Logging port stats to: %s", self.stats_logger.filepath)

        # Structured event log for the dashboard (JSON lines, one event
        # per line). Purely additive -- doesn't change any existing logging.
        self._events_dir = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), 'events')
        os.makedirs(self._events_dir, exist_ok=True)
        self._events_path = os.path.join(
            self._events_dir, 'events_%s_%d.jsonl' % (ALGORITHM, int(time.time())))
        self._events_file = open(self._events_path, 'a')
        self.logger.info("Writing structured events to: %s", self._events_path)

        self.port_stats = {}
        self.monitor_thread = hub.spawn(self._monitor_loop)

        self.active_udp_links = set()
        self._udp_link_counts = Counter()
        self._udp_link_by_cookie = {}
        self._next_cookie = 1

        # Link-exclusion reason tracking: frozenset({a,b}) -> {'physical','anomaly'}
        self._link_down_reasons = defaultdict(set)

        # Anomaly detection setup
        self.anomaly_model = None
        self._anomalous_ports = set()  # (dpid, port) currently confirmed anomalous
        self._anom_streak = defaultdict(int)    # (dpid, port) -> consecutive anomalous readings
        self._normal_streak = defaultdict(int)  # (dpid, port) -> consecutive normal readings
        try:
            import joblib
            _af_path = os.path.join(
                os.path.dirname(os.path.abspath(__file__)),
                'anomaly_features.py')
            _af_spec = importlib.util.spec_from_file_location(
                'anomaly_features', _af_path)
            _af_module = importlib.util.module_from_spec(_af_spec)
            _af_spec.loader.exec_module(_af_module)
            extract_features = _af_module.extract_features
            self._extract_features = extract_features
            if os.path.exists(ANOMALY_MODEL_PATH):
                self.anomaly_model = joblib.load(ANOMALY_MODEL_PATH)
                self.logger.info("Anomaly detection ENABLED (model: %s)",
                                  ANOMALY_MODEL_PATH)
            else:
                self.logger.warning(
                    "Anomaly detection disabled: no trained model at %s "
                    "(run experiments/train_anomaly_model.py first)",
                    ANOMALY_MODEL_PATH)
        except ImportError as e:
            self.logger.warning(
                "Anomaly detection disabled: %s (pip install scikit-learn "
                "joblib numpy into the SAME interpreter ryu-manager uses -- "
                "check with: head -1 $(which ryu-manager))", e)

    def _emit_event(self, event_type, **fields):
        """Appends one JSON record to this run's events file, for the
        replay dashboard. Never raises -- a logging failure must not
        take down routing."""
        record = {'ts': time.time(), 'type': event_type, 'algorithm': ALGORITHM}
        record.update(fields)
        try:
            self._events_file.write(json.dumps(record) + '\n')
            self._events_file.flush()
        except Exception as e:
            self.logger.error("Failed to write dashboard event: %s", e)

    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def switch_features_handler(self, ev):
        datapath = ev.msg.datapath
        self.datapaths[datapath.id] = datapath
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser
        match = parser.OFPMatch()
        actions = [parser.OFPActionOutput(ofproto.OFPP_CONTROLLER,
                                           ofproto.OFPCML_NO_BUFFER)]
        self.add_flow(datapath, 0, match, actions)
        self._install_bulk_meter(datapath)
        self.logger.info("Switch %s connected", datapath.id)

    def _install_bulk_meter(self, datapath):
        parser = datapath.ofproto_parser
        ofproto = datapath.ofproto
        band = parser.OFPMeterBandDrop(rate=BULK_METER_RATE_KBPS, burst_size=0)
        mod = parser.OFPMeterMod(
            datapath=datapath, command=ofproto.OFPMC_ADD,
            flags=ofproto.OFPMF_KBPS, meter_id=BULK_METER_ID, bands=[band]
        )
        datapath.send_msg(mod)

    @set_ev_cls(ofp_event.EventOFPStateChange, [MAIN_DISPATCHER, DEAD_DISPATCHER])
    def state_change_handler(self, ev):
        datapath = ev.datapath
        if ev.state == DEAD_DISPATCHER:
            if datapath.id in self.datapaths:
                del self.datapaths[datapath.id]
            self.port_stats.pop(datapath.id, None)

    def add_flow(self, datapath, priority, match, actions, idle_timeout=0,
                 meter_id=None, cookie=0, flags=0):
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser
        inst = []
        if meter_id is not None:
            inst.append(parser.OFPInstructionMeter(meter_id))
        inst.append(parser.OFPInstructionActions(ofproto.OFPIT_APPLY_ACTIONS, actions))
        mod = parser.OFPFlowMod(datapath=datapath, priority=priority,
                                 match=match, instructions=inst,
                                 idle_timeout=idle_timeout, cookie=cookie,
                                 flags=flags)
        datapath.send_msg(mod)

    def _monitor_loop(self):
        while True:
            for datapath in list(self.datapaths.values()):
                self._request_port_stats(datapath)
            hub.sleep(STATS_POLL_INTERVAL)

    def _request_port_stats(self, datapath):
        parser = datapath.ofproto_parser
        ofproto = datapath.ofproto
        req = parser.OFPPortStatsRequest(datapath, 0, ofproto.OFPP_ANY)
        datapath.send_msg(req)

    @set_ev_cls(ofp_event.EventOFPPortStatsReply, MAIN_DISPATCHER)
    def port_stats_reply_handler(self, ev):
        dpid = ev.msg.datapath.id
        self.port_stats[dpid] = {}
        for stat in ev.msg.body:
            tx_rate, rx_rate = self.stats_logger.log(
                dpid, stat.port_no, stat.tx_bytes, stat.rx_bytes)
            self.port_stats[dpid][stat.port_no] = {
                'tx_bytes': stat.tx_bytes,
                'rx_bytes': stat.rx_bytes,
                'tx_rate': tx_rate,
                'rx_rate': rx_rate,
            }

            if self.anomaly_model is not None and \
                    neighbor_via_port(dpid, stat.port_no) is not None:
                is_anom = self._check_port_anomaly(tx_rate, rx_rate)
                key = (dpid, stat.port_no)
                if is_anom:
                    self._anom_streak[key] += 1
                    self._normal_streak[key] = 0
                    if self._anom_streak[key] >= ANOMALY_CONFIRM:
                        self._anomalous_ports.add(key)
                else:
                    self._normal_streak[key] += 1
                    self._anom_streak[key] = 0
                    if self._normal_streak[key] >= ANOMALY_CLEAR:
                        self._anomalous_ports.discard(key)
                self._update_link_anomaly_status(dpid, stat.port_no)

    def _check_port_anomaly(self, tx_rate, rx_rate):
        feats = self._extract_features(tx_rate, rx_rate).reshape(1, -1)
        return self.anomaly_model.predict(feats)[0] == -1

    def _update_link_anomaly_status(self, dpid, port):
        neighbor = neighbor_via_port(dpid, port)
        if neighbor is None:
            return
        port_b = static_port_toward(neighbor, dpid)
        if port_b is None:
            return
        a_flagged = (dpid, port) in self._anomalous_ports
        b_flagged = (neighbor, port_b) in self._anomalous_ports
        link_anomalous = a_flagged or b_flagged

        if link_anomalous:
            key = frozenset((dpid, neighbor))
            already = 'anomaly' in self._link_down_reasons.get(key, set())
            if not already and (len(ADJACENCY.get(dpid, {})) <= 1 or
                                len(ADJACENCY.get(neighbor, {})) <= 1):
                return  # would cut a switch off completely: never exclude it
            changed = self._mark_link_down(dpid, neighbor, 'anomaly')
            if changed:
                self.logger.warning(
                    "[t=%.3f] ANOMALY DETECTED: switch %s <-> switch %s excluded from routing",
                    time.time(), dpid, neighbor)
                self._emit_event('anomaly', switch_a=dpid, switch_b=neighbor,
                                  status='detected')
                self._evict_flows_for_link(dpid, neighbor)
        else:
            changed = self._mark_link_up(dpid, neighbor, 'anomaly')
            if changed:
                self.logger.warning(
                    "[t=%.3f] ANOMALY CLEARED: switch %s <-> switch %s restored",
                    time.time(), dpid, neighbor)
                self._emit_event('anomaly', switch_a=dpid, switch_b=neighbor,
                                  status='cleared')

    def _mark_link_down(self, node_a, node_b, reason):
        """Adds `reason` to this link's active-exclusion-reasons set.
        Only actually mutates the live adjacency (and returns True) if
        this is the FIRST active reason -- so a link already down for
        one reason isn't double-processed when a second reason arrives."""
        key = frozenset((node_a, node_b))
        was_clear = len(self._link_down_reasons[key]) == 0
        self._link_down_reasons[key].add(reason)
        if was_clear:
            return set_link_down(node_a, node_b)
        return False

    def _mark_link_up(self, node_a, node_b, reason):
        """Removes `reason` from this link's active-exclusion-reasons set.
        Only actually restores the live adjacency (and returns True) if
        NO reasons remain -- so clearing one reason doesn't wrongly
        restore a link still excluded for the other."""
        key = frozenset((node_a, node_b))
        if reason not in self._link_down_reasons[key]:
            return False
        self._link_down_reasons[key].discard(reason)
        if not self._link_down_reasons[key]:
            del self._link_down_reasons[key]
            return set_link_up(node_a, node_b)
        return False

    @set_ev_cls(ofp_event.EventOFPFlowRemoved, MAIN_DISPATCHER)
    def flow_removed_handler(self, ev):
        cookie = ev.msg.cookie
        link = self._udp_link_by_cookie.pop(cookie, None)
        if link is None:
            return
        self._udp_link_counts[link] -= 1
        if self._udp_link_counts[link] <= 0:
            self._udp_link_counts.pop(link, None)
            self.active_udp_links.discard(link)

    @set_ev_cls(ofp_event.EventOFPPortStatus, MAIN_DISPATCHER)
    def port_status_handler(self, ev):
        msg = ev.msg
        dp = msg.datapath
        ofproto = dp.ofproto
        dpid = dp.id
        port_no = msg.desc.port_no
        is_down = bool(msg.desc.state & ofproto.OFPPS_LINK_DOWN)

        neighbor = neighbor_via_port(dpid, port_no)
        if neighbor is None:
            return

        if is_down:
            changed = self._mark_link_down(dpid, neighbor, 'physical')
            if changed:
                self.logger.warning(
                    "LINK DOWN: switch %s <-> switch %s (port %s on switch %s)",
                    dpid, neighbor, port_no, dpid)
                self._emit_event('link_status', switch_a=dpid, switch_b=neighbor,
                                  status='down', reason='physical')
                self._evict_flows_for_link(dpid, neighbor)
        else:
            changed = self._mark_link_up(dpid, neighbor, 'physical')
            if changed:
                self.logger.warning(
                    "LINK UP: switch %s <-> switch %s (port %s on switch %s)",
                    dpid, neighbor, port_no, dpid)
                self._emit_event('link_status', switch_a=dpid, switch_b=neighbor,
                                  status='up', reason='physical')

    def _evict_flows_for_link(self, node_a, node_b):
        self.__dict__.setdefault('_flow_path_cache', {}).clear()
        port_a = static_port_toward(node_a, node_b)
        port_b = static_port_toward(node_b, node_a)
        for node, port, other in ((node_a, port_a, node_b), (node_b, port_b, node_a)):
            if port is None:
                continue
            dp = self.datapaths.get(node)
            if dp is None:
                continue
            parser = dp.ofproto_parser
            ofproto = dp.ofproto
            mod = parser.OFPFlowMod(
                datapath=dp, command=ofproto.OFPFC_DELETE,
                table_id=ofproto.OFPTT_ALL,
                out_port=port, out_group=ofproto.OFPG_ANY,
                match=parser.OFPMatch())
            dp.send_msg(mod)
            self.logger.warning(
                "Evicted flows on switch %s routed via port %s (dead link to switch %s)",
                node, port, other)
            self._emit_event('eviction', switch=node, port=port, other_switch=other)

    def _build_match_fields(self, pkt, eth):
        if eth.ethertype == ether_types.ETH_TYPE_ARP:
            arp_pkt = pkt.get_protocol(arp.arp)
            return {'eth_type': 0x0806, 'arp_tpa': arp_pkt.dst_ip}, arp_pkt.dst_ip

        if eth.ethertype != ether_types.ETH_TYPE_IP:
            return None, None

        ip_pkt = pkt.get_protocol(ipv4.ipv4)
        fields = {
            'eth_type': 0x0800,
            'ipv4_src': ip_pkt.src,
            'ipv4_dst': ip_pkt.dst,
            'ip_proto': ip_pkt.proto,
        }

        tcp_pkt = pkt.get_protocol(tcp.tcp)
        udp_pkt = pkt.get_protocol(udp.udp)
        if tcp_pkt:
            fields['tcp_src'] = tcp_pkt.src_port
            fields['tcp_dst'] = tcp_pkt.dst_port
        elif udp_pkt:
            fields['udp_src'] = udp_pkt.src_port
            fields['udp_dst'] = udp_pkt.dst_port

        return fields, ip_pkt.dst

    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def packet_in_handler(self, ev):
        msg = ev.msg
        datapath = msg.datapath
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser
        dpid = datapath.id

        pkt = packet.Packet(msg.data)
        eth = pkt.get_protocol(ethernet.ethernet)
        if eth is None or eth.ethertype == ether_types.ETH_TYPE_LLDP:
            return

        match_fields, dst_ip = self._build_match_fields(pkt, eth)
        if match_fields is None:
            return

        if dst_ip not in HOST_LOCATION:
            self.logger.warning("Unknown destination host %s, dropping", dst_ip)
            return

        dst_switch, dst_port = HOST_LOCATION[dst_ip]
        flow_desc = f"{dst_ip}"
        if 'tcp_dst' in match_fields:
            flow_desc = f"{dst_ip}:tcp/{match_fields['tcp_dst']}"
        elif 'udp_dst' in match_fields:
            flow_desc = f"{dst_ip}:udp/{match_fields['udp_dst']}"
        elif match_fields.get('ip_proto') == 1:
            flow_desc = f"{dst_ip}:icmp"
        self.logger.info("--- packet_in at switch %s for %s ---", dpid, flow_desc)

        is_bulk = match_fields.get('ip_proto') == 6
        is_udp = match_fields.get('ip_proto') == 17
        flow_class = 'tcp' if is_bulk else ('udp' if is_udp else None)
        meter_id = BULK_METER_ID if is_bulk else None

        # --- decide once per flow: repeat packet-ins reuse the first decision ---
        _ttl = float(os.environ.get('SDN_PATH_CACHE_TTL', '5'))
        _key = tuple(sorted(match_fields.items()))
        _cache = self.__dict__.setdefault('_flow_path_cache', {})
        _now = time.time()
        for _k in [k for k, v in _cache.items() if _now - v[1] > _ttl]:
            del _cache[_k]
        _hit = _cache.get(_key)
        if _hit is not None and dpid in _hit[0]:
            _sub = _hit[0][_hit[0].index(dpid):]
            _port = port_toward(_sub[0], _sub[1]) if len(_sub) > 1 else dst_port
            _data = msg.data if msg.buffer_id == ofproto.OFP_NO_BUFFER else None
            datapath.send_msg(parser.OFPPacketOut(
                datapath=datapath, buffer_id=msg.buffer_id,
                in_port=msg.match['in_port'],
                actions=[parser.OFPActionOutput(_port)], data=_data))
            return

        pick_kwargs = {'src': dpid, 'dst': dst_switch, 'logger': self.logger,
                        'port_stats': self.port_stats, 'flow_class': flow_class,
                        'active_udp_links': self.active_udp_links}
        accepted = inspect.signature(self.selector.pick_path).parameters
        pick_kwargs = {k: v for k, v in pick_kwargs.items() if k in accepted}

        if hasattr(self.selector, 'last_info'):
            self.selector.last_info = None  # don't report a stale previous decision
        decision_start = time.time()
        path = self.selector.pick_path(**pick_kwargs)
        decision_latency_ms = (time.time() - decision_start) * 1000.0
        self.logger.info("DECISION_LATENCY: %.4fms", decision_latency_ms)

        if path is None:
            self.logger.warning("[t=%.3f] No path from switch %s to %s",
                                 time.time(), dpid, dst_switch)
            self._emit_event('decision', src=dpid, dst_switch=dst_switch,
                              flow_class=flow_class, path=None,
                              decision_latency_ms=decision_latency_ms,
                              flow=flow_desc, src_ip=match_fields.get('ipv4_src'),
                              algo_info=getattr(self.selector, 'last_info', None))
            return

        self._emit_event('decision', src=dpid, dst_switch=dst_switch,
                          flow_class=flow_class, path=path,
                          decision_latency_ms=decision_latency_ms,
                          flow=flow_desc, src_ip=match_fields.get('ipv4_src'),
                          algo_info=getattr(self.selector, 'last_info', None))

        _cache[_key] = (list(path), _now)

        for i, node in enumerate(path):
            if i < len(path) - 1:
                out_port = port_toward(node, path[i + 1])
            else:
                out_port = dst_port
            node_dp = self.datapaths.get(node)
            if node_dp is None:
                continue
            node_parser = node_dp.ofproto_parser
            match = node_parser.OFPMatch(**match_fields)
            actions = [node_parser.OFPActionOutput(out_port)]

            cookie = 0
            flags = 0
            if flow_class == 'udp' and i < len(path) - 1:
                link = (node, path[i + 1])
                cookie = self._next_cookie
                self._next_cookie += 1
                self._udp_link_by_cookie[cookie] = link
                self._udp_link_counts[link] += 1
                self.active_udp_links.add(link)
                flags = node_dp.ofproto.OFPFF_SEND_FLOW_REM

            self.add_flow(node_dp, 1, match, actions, idle_timeout=10,
                          meter_id=meter_id, cookie=cookie, flags=flags)

        first_hop_port = (port_toward(path[0], path[1])
                           if len(path) > 1 else dst_port)
        actions = [parser.OFPActionOutput(first_hop_port)]
        data = msg.data if msg.buffer_id == ofproto.OFP_NO_BUFFER else None
        out = parser.OFPPacketOut(datapath=datapath, buffer_id=msg.buffer_id,
                                   in_port=msg.match['in_port'],
                                   actions=actions, data=data)
        datapath.send_msg(out)
