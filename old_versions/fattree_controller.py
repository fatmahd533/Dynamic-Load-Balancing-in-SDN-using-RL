"""
Minimal path-computing Ryu controller for the fat-tree topology.

No flooding, no STP negotiation, no broadcast storms: the entire switch
adjacency graph and every host's location are known in advance, so for
any destination the controller computes the exact shortest switch path
via BFS and installs exact-match flow rules along it. This is also the
foundation Round-Robin and Least-Congested-Path (Step 1 of the project)
will build on -- they'll swap out "shortest path" for their own path
choice logic, using this same flow-install mechanism.
"""

from collections import deque

from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import CONFIG_DISPATCHER, MAIN_DISPATCHER
from ryu.controller.handler import set_ev_cls
from ryu.ofproto import ofproto_v1_3
from ryu.lib.packet import packet, ethernet, arp, ipv4, ether_types


# --- Switch dpid <-> name, for reference ---
# 1=a1  2=a2  3=a3  4=a4  5=c1  6=c2  7=e1  8=e2  9=e3  10=e4

# Switch adjacency graph: dpid -> {port: neighbor_dpid}
ADJACENCY = {
    1:  {1: 7, 2: 8, 3: 5, 4: 6},    # a1 -> e1, e2, c1, c2
    2:  {1: 7, 2: 8, 3: 5, 4: 6},    # a2 -> e1, e2, c1, c2
    3:  {1: 9, 2: 10, 3: 5, 4: 6},   # a3 -> e3, e4, c1, c2
    4:  {1: 9, 2: 10, 3: 5, 4: 6},   # a4 -> e3, e4, c1, c2
    5:  {1: 1, 2: 2, 3: 3, 4: 4},    # c1 -> a1, a2, a3, a4
    6:  {1: 1, 2: 2, 3: 3, 4: 4},    # c2 -> a1, a2, a3, a4
    7:  {3: 1, 4: 2},                # e1 -> a1, a2  (ports 1,2 go to hosts)
    8:  {3: 1, 4: 2},                # e2 -> a1, a2
    9:  {3: 3, 4: 4},                # e3 -> a3, a4
    10: {3: 3, 4: 4},                # e4 -> a3, a4
}

# Host IP -> (edge switch dpid, port on that switch)
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


def bfs_path(src, dst, adjacency):
    """Shortest path between two switch dpids, as a list of dpids."""
    if src == dst:
        return [src]
    visited = {src}
    queue = deque([[src]])
    while queue:
        path = queue.popleft()
        node = path[-1]
        for _port, neighbor in adjacency.get(node, {}).items():
            if neighbor in visited:
                continue
            new_path = path + [neighbor]
            if neighbor == dst:
                return new_path
            visited.add(neighbor)
            queue.append(new_path)
    return None


def port_toward(adjacency, node, next_node):
    """Which port on `node` leads to `next_node`."""
    for port, neighbor in adjacency[node].items():
        if neighbor == next_node:
            return port
    return None


class FatTreeController(app_manager.RyuApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    def __init__(self, *args, **kwargs):
        super(FatTreeController, self).__init__(*args, **kwargs)
        self.datapaths = {}  # dpid -> datapath object

    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def switch_features_handler(self, ev):
        datapath = ev.msg.datapath
        self.datapaths[datapath.id] = datapath
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser

        # Table-miss rule: anything unmatched goes to the controller
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

        dst_ip = None
        if eth.ethertype == ether_types.ETH_TYPE_ARP:
            arp_pkt = pkt.get_protocol(arp.arp)
            dst_ip = arp_pkt.dst_ip
            match_fields = {'eth_type': 0x0806, 'arp_tpa': dst_ip}
        elif eth.ethertype == ether_types.ETH_TYPE_IP:
            ip_pkt = pkt.get_protocol(ipv4.ipv4)
            dst_ip = ip_pkt.dst
            match_fields = {'eth_type': 0x0800, 'ipv4_dst': dst_ip}
        else:
            return  # ignore anything else (IPv6 ND, etc.)

        if dst_ip not in HOST_LOCATION:
            self.logger.warning("Unknown destination host %s, dropping", dst_ip)
            return

        dst_switch, dst_port = HOST_LOCATION[dst_ip]
        path = bfs_path(dpid, dst_switch, ADJACENCY)
        if path is None:
            self.logger.warning("No path from switch %s to %s", dpid, dst_switch)
            return

        # Install a flow at every switch along the path
        for i, node in enumerate(path):
            if i < len(path) - 1:
                out_port = port_toward(ADJACENCY, node, path[i + 1])
            else:
                out_port = dst_port  # last switch: send straight to host

            node_dp = self.datapaths.get(node)
            if node_dp is None:
                continue  # switch hasn't connected yet, skip
            node_parser = node_dp.ofproto_parser
            match = node_parser.OFPMatch(**match_fields)
            actions = [node_parser.OFPActionOutput(out_port)]
            self.add_flow(node_dp, 1, match, actions)

        # Send this specific packet out immediately (flow install is for
        # future packets; this one already arrived and needs handling now)
        first_hop_port = (port_toward(ADJACENCY, path[0], path[1])
                           if len(path) > 1 else dst_port)
        actions = [parser.OFPActionOutput(first_hop_port)]
        data = msg.data if msg.buffer_id == ofproto.OFP_NO_BUFFER else None
        out = parser.OFPPacketOut(datapath=datapath, buffer_id=msg.buffer_id,
                                   in_port=msg.match['in_port'],
                                   actions=actions, data=data)
        datapath.send_msg(out)
