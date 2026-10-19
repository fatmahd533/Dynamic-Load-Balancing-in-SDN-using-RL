"""
Least-Congested-Path that also counts flows it has just placed (see
pending.py). This is the fair, simple baseline for the same idea as the
Q-learning agent with pending load: same information, no learning.
"""

from topology import all_shortest_paths, port_toward
from algorithms.pending import PendingLoad


class PathSelector:
    def __init__(self):
        self.pending = PendingLoad()
        self.last_info = None

    def _link_rate(self, node, next_node, port_stats):
        port = port_toward(node, next_node)
        measured = 0.0
        if port is not None:
            measured = port_stats.get(node, {}).get(port, {}).get('tx_rate', 0.0)
        return measured + self.pending.extra(node, next_node)

    def pick_path(self, src, dst, logger=None, port_stats=None, flow_class=None):
        paths = all_shortest_paths(src, dst)
        if not paths:
            return None
        port_stats = port_stats or {}
        scored = []
        for p in paths:
            rates = [self._link_rate(p[i], p[i + 1], port_stats) for i in range(len(p) - 1)]
            scored.append((max(rates) if rates else 0.0, p))
        scored.sort(key=lambda pair: pair[0])
        chosen = scored[0][1]
        self.pending.add(chosen, flow_class)
        self.last_info = {'candidates': len(paths)}
        if logger:
            logger.info("LCP-pending: %s -> %s | chose %s (effective bottleneck=%.0f B/s)",
                        src, dst, chosen, scored[0][0])
        return chosen
