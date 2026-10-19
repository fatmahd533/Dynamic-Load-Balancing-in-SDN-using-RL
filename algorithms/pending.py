"""
Remembers the flows the controller placed very recently, so a selector can
add their expected load to the measured link rates.

Why: port statistics lag (0.5 to 2 s). Flows that start at the same moment
all read an idle network and pile onto the same path. Each placement is
therefore reserved on every directed link of its path for a short time (TTL).
After the TTL the flow shows up in the measured statistics and the
reservation is dropped.

Assumption (stated in the report): a new flow is expected to need
SDN_PENDING_BPS bytes/s (default 1.0e6, about 8 Mbps, for UDP; 0.625e6, the
5 Mbps meter rate, for TCP). The TTL is SDN_PENDING_TTL seconds (default 2).
"""

import os
import time

TCP_RESERVE_BPS = 5000 * 1000 / 8  # the bulk meter rate, bytes/s


class PendingLoad:
    def __init__(self):
        self.ttl = float(os.environ.get('SDN_PENDING_TTL', '2.0'))
        self.udp_bps = float(os.environ.get('SDN_PENDING_BPS', '1000000'))
        self._items = []  # (time, [(node, next_node), ...], bytes/s)

    def _prune(self, now):
        self._items = [it for it in self._items if now - it[0] < self.ttl]

    def add(self, path, flow_class=None, now=None):
        now = time.time() if now is None else now
        self._prune(now)
        amount = min(self.udp_bps, TCP_RESERVE_BPS) if flow_class == 'tcp' else self.udp_bps
        edges = [(path[i], path[i + 1]) for i in range(len(path) - 1)]
        self._items.append((now, edges, amount))

    def extra(self, node, next_node, now=None):
        now = time.time() if now is None else now
        self._prune(now)
        return sum(a for t, edges, a in self._items if (node, next_node) in edges)
