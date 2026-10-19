"""
Round-Robin baseline: cycles through all equal-length paths in turn,
per (src_switch, dst_switch) pair. No awareness of actual congestion.
"""

from collections import defaultdict
from topology import all_shortest_paths


class PathSelector:
    def __init__(self):
        self.counter = defaultdict(int)
        self.last_info = None  # read by the controller for the dashboard

    def pick_path(self, src, dst, logger=None, port_stats=None):
        paths = all_shortest_paths(src, dst)
        if not paths:
            return None
        key = (src, dst)
        idx = self.counter[key] % len(paths)
        self.counter[key] += 1
        chosen = paths[idx]
        self.last_info = {'candidates': len(paths), 'turn': idx + 1}
        if logger:
            logger.info("RR: %s -> %s | %d equal paths | chose %s",
                         src, dst, len(paths), chosen)
        return chosen
