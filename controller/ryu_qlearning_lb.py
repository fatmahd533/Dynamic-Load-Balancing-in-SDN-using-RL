# controller/rl_load_balancer.py
from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import MAIN_DISPATCHER, CONFIG_DISPATCHER, set_ev_cls
from ryu.ofproto import ofproto_v1_3
from ryu.lib import hub
from ryu.lib.packet import packet, ethernet
from ryu.topology.api import get_link
from ryu.topology import event as topo_event
import random
import time

class RLLB(app_manager.RyuApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    def __init__(self, *args, **kwargs):
        super(RLLB, self).__init__(*args, **kwargs)
        self.datapaths = {}          # dp_id -> datapath
        self.port_stats = {}         # dp_id -> {port: tx_bytes}
        self.last_port_stats = {}    # dp_id -> {port: last tx_bytes}
        self.q_table = {}            # (state, action) -> qvalue
        self.alpha = 0.5
        self.gamma = 0.9
        self.epsilon = 0.2
        self.mac_to_port = {}        # dp_id -> {mac: port}
        self.host_location = {}      # mac -> (dp_id, port)
        self.neighbors = {}          # dp_id -> {neighbor_dpid: local_port}
        self.logger.info("✅ RLLB initialized (topology discovery + Q-Learning)")
        self.monitor_thread = hub.spawn(self._monitor)
        hub.spawn_after(1, self._discover_topology)

    # ---------------- Topology discovery ----------------
    def _discover_topology(self):
        hub.sleep(1)  # wait for switches
        try:
            links = get_link(self, None)
        except Exception:
            links = []
        neigh = {}
        for l in links:
            src_dpid, dst_dpid = l.src.dpid, l.dst.dpid
            src_port, dst_port = l.src.port_no, l.dst.port_no
            neigh.setdefault(src_dpid, {})[dst_dpid] = src_port
            neigh.setdefault(dst_dpid, {})[src_dpid] = dst_port
        self.neighbors = neigh
        self.logger.info(f"🔍 Topology discovered: {self.neighbors}")

    @set_ev_cls(topo_event.EventLinkAdd)
    def _link_add_handler(self, ev):
        hub.spawn(self._discover_topology)

    @set_ev_cls(topo_event.EventLinkDelete)
    def _link_del_handler(self, ev):
        hub.spawn(self._discover_topology)

    # ---------------- Datapaths management ----------------
    @set_ev_cls(ofp_event.EventOFPStateChange, [MAIN_DISPATCHER, CONFIG_DISPATCHER])
    def _state_change_handler(self, ev):
        datapath = ev.datapath
        if ev.state == MAIN_DISPATCHER:
            self.datapaths[datapath.id] = datapath
            self.logger.info(f"🔗 Switch {datapath.id} connected")
            hub.spawn(self._discover_topology)
        elif datapath.id in self.datapaths:
            del self.datapaths[datapath.id]
            self.logger.info(f"❌ Switch {datapath.id} disconnected")
            hub.spawn(self._discover_topology)

    # ---------------- Monitoring ----------------
    def _monitor(self):
        while True:
            for dp in list(self.datapaths.values()):
                self._request_stats(dp)
            hub.sleep(5)

    def _request_stats(self, datapath):
        parser = datapath.ofproto_parser
        req = parser.OFPPortStatsRequest(datapath, 0, datapath.ofproto.OFPP_ANY)
        datapath.send_msg(req)

    @set_ev_cls(ofp_event.EventOFPPortStatsReply, MAIN_DISPATCHER)
    def port_stats_reply_handler(self, ev):
        dp_id = ev.msg.datapath.id
        body = ev.msg.body
        self.last_port_stats.setdefault(dp_id, self.port_stats.get(dp_id, {}).copy())
        self.port_stats[dp_id] = {}
        for stat in body:
            self.port_stats[dp_id][stat.port_no] = stat.tx_bytes
        self.logger.debug(f"📊 Stats Switch {dp_id}: {self.port_stats[dp_id]}")

    # ---------------- Q-learning utilities ----------------
    def get_Q(self, state, action):
        return self.q_table.get((state, action), 0.0)

    def update_Q(self, state, action, reward, next_state):
        next_actions = list(self.neighbors.get(int(next_state.split('_')[0].lstrip('sw')), {}).keys())
        best_next = max([self.get_Q(next_state, a) for a in next_actions], default=0)
        old_value = self.get_Q(state, action)
        new_value = old_value + self.alpha * (reward + self.gamma * best_next - old_value)
        self.q_table[(state, action)] = new_value
        self.logger.debug(f"🧠 Q[{state},{action}] updated to {new_value:.3f}")

    def choose_action(self, state, actions):
        if not actions:
            return None
        if random.random() < self.epsilon:
            choice = random.choice(actions)
            self.logger.debug(f"🎲 Exploration: {choice} for {state}")
            return choice
        q_vals = [self.get_Q(state, a) for a in actions]
        max_q = max(q_vals)
        best = [a for a, q in zip(actions, q_vals) if q == max_q]
        choice = random.choice(best)
        self.logger.debug(f"🧠 Exploitation: {choice} for {state} (q={max_q:.3f})")
        return choice

    # ---------------- Default flow ----------------
    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def switch_features_handler(self, ev):
        datapath = ev.msg.datapath
        parser = datapath.ofproto_parser
        ofproto = datapath.ofproto
        match = parser.OFPMatch()
        actions = [parser.OFPActionOutput(ofproto.OFPP_CONTROLLER,
                                          ofproto.OFPCML_NO_BUFFER)]
        self.add_flow(datapath, 0, match, actions)

    def add_flow(self, datapath, priority, match, actions, idle_timeout=0, hard_timeout=0):
        parser = datapath.ofproto_parser
        ofproto = datapath.ofproto
        inst = [parser.OFPInstructionActions(ofproto.OFPIT_APPLY_ACTIONS, actions)]
        mod = parser.OFPFlowMod(datapath=datapath, priority=priority,
                                match=match, instructions=inst,
                                idle_timeout=idle_timeout, hard_timeout=hard_timeout)
        datapath.send_msg(mod)

    # ---------------- Packet in handling ----------------
    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def packet_in_handler(self, ev):
        msg = ev.msg
        datapath = msg.datapath
        dp_id = datapath.id
        parser = datapath.ofproto_parser
        ofproto = datapath.ofproto
        in_port = msg.match.get('in_port', 0)
        data = None if msg.buffer_id != ofproto.OFP_NO_BUFFER else msg.data

        pkt = packet.Packet(msg.data)
        eth = pkt.get_protocol(ethernet.ethernet)
        if eth is None:
            return
        src, dst = eth.src, eth.dst

        # MAC learning
        self.mac_to_port.setdefault(dp_id, {})[src] = in_port
        self.host_location[src] = (dp_id, in_port)

        state = f"sw{dp_id}_p{in_port}"

        # Destination known locally
        if dst in self.mac_to_port.get(dp_id, {}):
            out_port = self.mac_to_port[dp_id][dst]
            match = parser.OFPMatch(in_port=in_port, eth_src=src, eth_dst=dst)
            actions = [parser.OFPActionOutput(out_port)]
            self.add_flow(datapath, 10, match, actions, idle_timeout=30)
            out = parser.OFPPacketOut(datapath=datapath, buffer_id=msg.buffer_id,
                                      in_port=in_port, actions=actions, data=data)
            datapath.send_msg(out)
            return

        # Destination known on other switch
        action_taken = None
        if dst in self.host_location:
            dst_dp, dst_port = self.host_location[dst]
            if dst_dp != dp_id:
                neighs = list(self.neighbors.get(dp_id, {}).keys())
                if neighs:
                    chosen_neighbor = self.choose_action(state, neighs)
                    action_taken = chosen_neighbor
                    out_port = self.neighbors[dp_id].get(chosen_neighbor)
                    if out_port is None:
                        out_port = ofproto.OFPP_FLOOD
                    match = parser.OFPMatch(eth_dst=dst)
                    actions = [parser.OFPActionOutput(out_port)]
                else:
                    out_port = ofproto.OFPP_FLOOD
                    actions = [parser.OFPActionOutput(out_port)]
            else:
                out_port = dst_port
                actions = [parser.OFPActionOutput(out_port)]
        else:
            out_port = ofproto.OFPP_FLOOD
            actions = [parser.OFPActionOutput(out_port)]

        out = parser.OFPPacketOut(datapath=datapath, buffer_id=msg.buffer_id,
                                  in_port=in_port, actions=actions, data=data)
        datapath.send_msg(out)

        # Q-learning update
        if action_taken is not None and out_port != ofproto.OFPP_FLOOD:
            prev_tx = self.last_port_stats.get(dp_id, {}).get(out_port, 0)
            tx = self.port_stats.get(dp_id, {}).get(out_port, 0)
            delta = max(0, tx - prev_tx)
            reward = 1.0 / (1.0 + delta)
            self.update_Q(state, action_taken, reward, state)

