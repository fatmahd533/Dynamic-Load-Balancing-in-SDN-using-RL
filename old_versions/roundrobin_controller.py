"""
Round-Robin baseline controller for the fat-tree topology.

Concept: when multiple equal-length paths exist between two switches,
cycle through them in turn for each new flow, instead of always picking
the same one. This spreads load blindly across available paths -- no
awareness of actual congestion. This is the "naive floor" baseline that
Least-Congested-Path and Q-Learning will be compared against.
"""

from collections import deque, defaultdict

from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import CONFIG_DISPATCHER, MAIN_DISPATCHER
from ryu.controller.handler import set_ev_cls
from ryu.ofproto import ofproto_v1_3
from ryu.lib.packet import packet, ethernet, arp, ipv4, ether_types


ADJACENCY = {
    1:  {1: 7, 2: 8, 3: 5, 4: 6},
    2:  {1: 7, 2: 8, 3: 5, 4: 6},
    3:  {1: 9, 2: 10, 3: 5, 4: 6},
    4:  {1: 9, 2: 10, 3: 5, 4: 6},
    5:  {1: 1, 2: 2, 3: 3, 4: 4},
    6:  {1: 1, 2: 2, 3: 3, 4: 4},
    7:  {3: 1, 4: 2},
    8:  {3: 1, 4: 2},
    9:  {3: 3, 4: 4},
    10: {3: 3, 4: 4},
}

HOST_LOCATION = {
    '10.0.0.1': (7, 1),
    '10.0.0.2': (7, 2),
    '10.0.0.3': (8, 1),
    '10.0.0.4': (8, 2),
    '10.0.0.5': (9, 1),
    '10.0.0.6': (9, 2),
    '10.0.0.7': (10, 1),
    '10.0.0.8': (10, 2),
}


def all_shortest_paths(src, dst, adjacency):
    """Return every path of minimum length between src and dst (as dpid lists)."""
    if src == dst:
        return [[src]]

    # First BFS pass: find the shortest distance to every node from src
    dist = {src: 0}
    queue = deque([src])
    while queue:
        node = queue.popleft()
        for neighbor in adjacency.get(node, {}).values():
            if neighbor not in dist:
                dist[neighbor] = dist[node] + 1
                queue.append(neighbor)

    if dst not in dist:
        return []

    # Second pass: collect all paths that follow strictly increasing distance
    paths = []

    def dfs(node, path):
        if node == dst:
            paths.append(list(path))
            return
        for neighbor in adjacency.get(node, {}).values():
            if dist.get(neighbor, -1) == dist[node] + 1:
                path.append(neighbor)
                dfs(neighbor, path)
                path.pop()

    dfs(src, [src])
    return paths


def port_toward(adjacency, node, next_node):
    for port, neighbor in adjacency[node].items():
        if neighbor == next_node:
            return port
    return None


class RoundRobinController(app_manager.RyuApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    def __init__(self, *args, **kwargs):
        super(RoundRobinController, self).__init__(*args, **kwargs)
        self.datapaths = {}
        # Counter per (src_switch, dst_switch) pair, to cycle through paths
        self.rr_counter = defaultdict(int)

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
        self.logger.info("Switch %s connected", datapath.id)

    def add_flow(self, datapath, priority, match, actions):
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser
        inst = [parser.OFPInstructionActions(ofproto.OFPIT_APPLY_ACTIONS, actions)]
        mod = parser.OFPFlowMod(datapath=datapath, priority=priority,
                                 match=match, instructions=inst)
        datapath.send_msg(mod)

    def pick_path(self, src_switch, dst_switch):
        """Round-robin selection among all equal-length paths."""
        paths = all_shortest_paths(src_switch, dst_switch, ADJACENCY)
        if not paths:
            return None
        key = (src_switch, dst_switch)
        idx = self.rr_counter[key] % len(paths)
        self.rr_counter[key] += 1
        chosen = paths[idx]
        self.logger.info(
            "RR: %s -> %s | %d equal paths available | chose path %s",
            src_switch, dst_switch, len(paths), chosen
        )
        return chosen

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

        if eth.ethertype == ether_types.ETH_TYPE_ARP:
            arp_pkt = pkt.get_protocol(arp.arp)
            dst_ip = arp_pkt.dst_ip
            match_fields = {'eth_type': 0x0806, 'arp_tpa': dst_ip}
        elif eth.ethertype == ether_types.ETH_TYPE_IP:
            ip_pkt = pkt.get_protocol(ipv4.ipv4)
            dst_ip = ip_pkt.dst
            match_fields = {'eth_type': 0x0800, 'ipv4_dst': dst_ip}
        else:
            return

        if dst_ip not in HOST_LOCATION:
            self.logger.warning("Unknown destination host %s, dropping", dst_ip)
            return

        dst_switch, dst_port = HOST_LOCATION[dst_ip]
        path = self.pick_path(dpid, dst_switch)
        if path is None:
            self.logger.warning("No path from switch %s to %s", dpid, dst_switch)
            return

        for i, node in enumerate(path):
            if i < len(path) - 1:
                out_port = port_toward(ADJACENCY, node, path[i + 1])
            else:
                out_port = dst_port

            node_dp = self.datapaths.get(node)
            if node_dp is None:
                continue
            node_parser = node_dp.ofproto_parser
            match = node_parser.OFPMatch(**match_fields)
            actions = [node_parser.OFPActionOutput(out_port)]
            self.add_flow(node_dp, 1, match, actions)

        first_hop_port = (port_toward(ADJACENCY, path[0], path[1])
                           if len(path) > 1 else dst_port)
        actions = [parser.OFPActionOutput(first_hop_port)]
        data = msg.data if msg.buffer_id == ofproto.OFP_NO_BUFFER else None
        out = parser.OFPPacketOut(datapath=datapath, buffer_id=msg.buffer_id,
                                   in_port=msg.match['in_port'],
                                   actions=actions, data=data)
        datapath.send_msg(out)
