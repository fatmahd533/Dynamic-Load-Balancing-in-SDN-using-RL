"""
Least-Congested-Path baseline: among all equal-length candidate paths,
picks the one whose most-loaded link (the bottleneck) currently has the
lowest transmit rate. Rates are computed continuously by the controller
on every polling cycle (see controller.py's port_stats_reply_handler),
not reactively when a decision is needed -- this matters, because
computing load "since the last time we checked" only works if checks
happen on a steady schedule, which decision-triggered checks don't.
"""

from topology import all_shortest_paths, port_toward


class PathSelector:
    last_info = None  # read by the controller for the dashboard

    def _link_rate(self, node, next_node, port_stats):
        """Most recently measured tx rate (bytes/sec) leaving `node`
        toward `next_node`. Missing data (not polled yet) = assume 0."""
        port = port_toward(node, next_node)
        if port is None:
            return 0.0
        port_data = port_stats.get(node, {}).get(port)
        if port_data is None:
            return 0.0
        return port_data.get('tx_rate', 0.0)

    def _path_bottleneck(self, path, port_stats):
        rates = [
            self._link_rate(path[i], path[i + 1], port_stats)
            for i in range(len(path) - 1)
        ]
        return max(rates) if rates else 0.0

    def pick_path(self, src, dst, logger=None, port_stats=None):
        paths = all_shortest_paths(src, dst)
        if not paths:
            return None
        port_stats = port_stats or {}

        scored = [(self._path_bottleneck(p, port_stats), p) for p in paths]
        scored.sort(key=lambda pair: pair[0])
        best_rate, chosen = scored[0]
        self.last_info = {'candidates': len(paths), 'bottleneck_Bps': best_rate}

        if logger:
            logger.info(
                "LCP: %s -> %s | %d candidates | chose %s (bottleneck rate=%.0f B/s)",
                src, dst, len(paths), chosen, best_rate
            )
        return chosen
