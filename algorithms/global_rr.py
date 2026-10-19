"""
Baseline: ONE global round-robin counter shared by all flows, cycling
through the equal-cost shortest paths. Unlike round_robin.py (counter per
switch pair, which equals shortest_path when every flow has its own
pair), this really spreads consecutive flows over different paths.
No network information is used.
"""

from topology import all_shortest_paths


class PathSelector:
    def __init__(self):
        self.counter = 0
        self.last_info = None

    def pick_path(self, src, dst, logger=None, port_stats=None):
        paths = all_shortest_paths(src, dst)
        if not paths:
            return None
        idx = self.counter % len(paths)
        self.counter += 1
        self.last_info = {'candidates': len(paths)}
        return paths[idx]
