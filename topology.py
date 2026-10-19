"""
Shared topology definition for the fat-tree network.
Every algorithm (round_robin, least_congested, q_learning, ...) imports
this instead of redefining the graph -- one source of truth.

Step 5 (failure/recovery testing) addition: ADJACENCY is mutated in
place (not reassigned) when a link is marked down/up via set_link_down()
/ set_link_up(). Every algorithm imports ADJACENCY by reference (e.g.
`from topology import ADJACENCY`), so an in-place mutation here is
immediately visible everywhere without changing a single algorithm
file -- bfs_path(), all_shortest_paths(), and every algorithm's own BFS
helpers naturally stop considering a down link on their very next call.

A separate, permanently-static copy of the original port<->neighbor
mapping (_STATIC_PORT_NEIGHBOR / _STATIC_NEIGHBOR_PORT) is frozen at
import time, before any failure is ever injected. This lets
controller.py resolve "which switch used to be on the other end of
this port" even after set_link_down() has already removed that entry
from the live ADJACENCY -- needed both to bring the link back up later
and to know which port to evict flows from on link-down.
"""

from collections import deque

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

# Frozen at import time, before ADJACENCY is ever mutated by a failure
# injection -- the permanent record of "what was originally connected
# to what, on which port", independent of current up/down state.
_STATIC_PORT_NEIGHBOR = {
    (node, port): neighbor
    for node, ports in ADJACENCY.items()
    for port, neighbor in ports.items()
}
_STATIC_NEIGHBOR_PORT = {
    (node, neighbor): port
    for (node, port), neighbor in _STATIC_PORT_NEIGHBOR.items()
}

# Currently-down links: frozenset({a, b}) -> ((a, port_a), (b, port_b)),
# so set_link_up() can restore the exact entries set_link_down() removed.
_DOWN_LINKS = {}


def bfs_path(src, dst, adjacency=ADJACENCY):
    if src == dst:
        return [src]
    visited = {src}
    queue = deque([[src]])
    while queue:
        path = queue.popleft()
        node = path[-1]
        for neighbor in adjacency.get(node, {}).values():
            if neighbor in visited:
                continue
            new_path = path + [neighbor]
            if neighbor == dst:
                return new_path
            visited.add(neighbor)
            queue.append(new_path)
    return None


def all_shortest_paths(src, dst, adjacency=ADJACENCY):
    if src == dst:
        return [[src]]
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


def port_toward(node, next_node, adjacency=ADJACENCY):
    for port, neighbor in adjacency[node].items():
        if neighbor == next_node:
            return port
    return None


def static_port_toward(node, neighbor):
    """Like port_toward(), but always answers from the original,
    never-mutated topology -- works even while the link is currently
    marked down (port_toward() would return None then, since the entry
    has been removed from the live ADJACENCY)."""
    return _STATIC_NEIGHBOR_PORT.get((node, neighbor))


def neighbor_via_port(node, port):
    """Which switch was originally on the other end of this port,
    regardless of current up/down state. Returns None for a host-facing
    port (never in the switch-to-switch adjacency to begin with) --
    controller.py uses this to distinguish a real inter-switch link
    failure from an irrelevant host-facing port event."""
    return _STATIC_PORT_NEIGHBOR.get((node, port))


def set_link_down(node_a, node_b, adjacency=ADJACENCY):
    """Remove the link between node_a and node_b from the live
    adjacency (both directions), so every algorithm's next path
    computation naturally avoids it. Returns True if this call actually
    changed anything (False if the link was already marked down, or
    node_a/node_b aren't directly connected in the original topology)."""
    key = frozenset((node_a, node_b))
    if key in _DOWN_LINKS:
        return False
    port_a = static_port_toward(node_a, node_b)
    port_b = static_port_toward(node_b, node_a)
    if port_a is None or port_b is None:
        return False
    if port_a not in adjacency.get(node_a, {}) or port_b not in adjacency.get(node_b, {}):
        return False
    _DOWN_LINKS[key] = ((node_a, port_a), (node_b, port_b))
    del adjacency[node_a][port_a]
    del adjacency[node_b][port_b]
    return True


def set_link_up(node_a, node_b, adjacency=ADJACENCY):
    """Restore a link previously marked down with set_link_down().
    Returns True if this call actually changed anything (False if the
    link wasn't marked down)."""
    key = frozenset((node_a, node_b))
    if key not in _DOWN_LINKS:
        return False
    (a, port_a), (b, port_b) = _DOWN_LINKS.pop(key)
    adjacency[a][port_a] = b
    adjacency[b][port_b] = a
    return True


def is_link_down(node_a, node_b):
    return frozenset((node_a, node_b)) in _DOWN_LINKS


def down_links():
    """Currently-down links, as a list of (node_a, node_b) tuples --
    for logging/debugging."""
    return [tuple(sorted(key)) for key in _DOWN_LINKS.keys()]
