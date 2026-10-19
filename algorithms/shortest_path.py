"""Always picks the single shortest path. Used for connectivity testing,
not one of the three compared algorithms."""

from topology import bfs_path


class PathSelector:
    def pick_path(self, src, dst, logger=None, port_stats=None):
        return bfs_path(src, dst)
