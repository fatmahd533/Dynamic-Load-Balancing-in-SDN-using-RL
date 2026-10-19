"""
Control baseline: pick uniformly at random among the equal-cost shortest
paths, independently for every new flow. Needs no network information.

Why it exists: with stale link statistics, simply spreading flows
randomly already helps compared with a single path. Any learned or
load-aware algorithm has to beat THIS, not just the single-path baseline,
to claim it exploits network state.
"""

import random

from topology import all_shortest_paths


class PathSelector:
    def __init__(self):
        self.last_info = None

    def pick_path(self, src, dst, logger=None):
        paths = all_shortest_paths(src, dst)
        if not paths:
            return None
        path = random.choice(paths)
        self.last_info = {'candidates': len(paths)}
        return path
