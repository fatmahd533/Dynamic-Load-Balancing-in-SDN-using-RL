"""
QoS-aware Least-Congested-Path: the Step 4 class-aware baseline, for
fair comparison against class-agnostic LCP (algorithms/least_congested.py)
and class-aware Q-learning.

UDP (latency-sensitive): identical policy to class-agnostic LCP -- pick
the path with the lowest bottleneck rate. Already optimal for latency,
no change needed.

TCP (bulk): actively avoid links currently carrying UDP traffic, not
just generally busy links. Among shortest paths, prefer any path with
zero overlap with active_udp_links; break ties by bottleneck rate. If
every candidate path overlaps an active UDP link, fall back to plain
bottleneck-rate LCP (no path is UDP-free, so don't strand the flow).
"""

from topology import all_shortest_paths, port_toward


class PathSelector:
    last_info = None  # read by the controller for the dashboard

    def _link_rate(self, node, next_node, port_stats):
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

    def _path_links(self, path):
        return {(path[i], path[i + 1]) for i in range(len(path) - 1)}

    def pick_path(self, src, dst, logger=None, port_stats=None,
                   flow_class=None, active_udp_links=None):
        paths = all_shortest_paths(src, dst)
        if not paths:
            return None
        port_stats = port_stats or {}
        active_udp_links = active_udp_links or set()

        scored = [(self._path_bottleneck(p, port_stats), p) for p in paths]
        scored.sort(key=lambda pair: pair[0])

        if flow_class == 'udp':
            best_rate, chosen = scored[0]
            reason = "udp: min-bottleneck (unchanged policy)"
        else:
            udp_free = [
                (rate, p) for rate, p in scored
                if not (self._path_links(p) & active_udp_links)
            ]
            if udp_free:
                best_rate, chosen = udp_free[0]
                reason = "tcp: avoided active UDP links"
            else:
                best_rate, chosen = scored[0]
                reason = "tcp: no UDP-free path available, fell back to min-bottleneck"

        self.last_info = {'candidates': len(paths), 'bottleneck_Bps': best_rate,
                          'reason': reason}

        if logger:
            logger.info(
                "LCP-QoS: %s -> %s | class=%s | %d candidates | chose %s "
                "(bottleneck rate=%.0f B/s) | %s",
                src, dst, flow_class, len(paths), chosen, best_rate, reason
            )
        return chosen
