"""
Q-learning baseline (tabular, not deep RL -- deliberately, per project
scope). Builds a path one hop at a time via epsilon-greedy action
selection, then does a multi-step Bellman backup across the whole hop
sequence just taken. This is what "correct multi-step Bellman
bootstrapping" means here: value propagates backward through every
intermediate switch's decision, not just a single atomic path choice.

State:   (current_switch, destination_switch, congestion_bucket, flow_class)
         congestion_bucket is 0/1/2 (low/med/high), based on the busiest
         candidate outgoing link at this decision point -- this is the
         "congestion-aware state, not just ingress port" requirement.
         flow_class ('tcp'/'udp'/None) is the Step 4 addition: the same
         switch/dest/congestion situation is now a DIFFERENT state
         depending on traffic class, so the agent can learn different
         policies for each rather than one blended policy.
Action:  which neighboring switch to hop to next (only neighbors that
         make real progress toward the destination are eligible, so
         paths stay loop-free and no longer than the shortest paths the
         baselines already consider).
Reward:  -congestion_of_chosen_link * class_penalty - small per-hop cost.
         class_penalty is UDP_PENALTY_MULTIPLIER for UDP flows and 1.0
         otherwise -- Step 4's class-aware reward shaping: the same
         congestion level hurts more when the traffic is latency-
         sensitive, so the agent is pushed to route UDP away from
         congestion more aggressively than TCP.
Epsilon: decays with wall-clock time since this PathSelector was
         created, not with number of decisions made.

Evaluation options (all OFF by default -- with no environment variables
set, behaviour is identical to the original):
  SDN_QL_STATE=per_link  state holds the congestion bucket of EACH
                         candidate link (sorted by node id) instead of only
                         the busiest one, so the agent can tell WHICH
                         candidate is busy. Default 'busiest' (original).
  SDN_QL_SAVE=<file>     write the Q-table to JSON every SDN_QL_SAVE_EVERY
                         (default 5) decisions
  SDN_QL_LOAD=<file>     load a Q-table at start (error if missing)
  SDN_QL_FREEZE=1        evaluation mode: no learning, epsilon 0 unless
                         SDN_QL_EPS is set
  SDN_QL_EPS=<float>     fixed epsilon instead of the time-decay schedule
  SDN_QL_TIEBREAK=random choose randomly among equal-valued actions
  SDN_QL_PENDING=1       add the expected load of flows placed in the last
                         SDN_PENDING_TTL seconds to the measured link rates,
                         in the state AND in the reward (see pending.py)
                         (default 'first', original)
"""

import csv
import json
import math
import os
import random
import time
from datetime import datetime
from collections import deque, defaultdict

from topology import ADJACENCY, port_toward
from algorithms.pending import PendingLoad

TRAINING_LOG_DIR = 'results'

# -- Hyperparameters (deliberately simple/tabular, see project scope) --
ALPHA = 0.3          # learning rate
GAMMA = 0.9           # discount factor
EPS_MAX = 1.0         # initial exploration rate
EPS_MIN = 0.05        # exploration rate floor
EPS_DECAY_RATE = 0.01 # higher = faster decay (wall-clock seconds)
PER_HOP_COST = 0.02   # small constant cost per hop, discourages wandering

# Step 4: same congestion level penalized more heavily for UDP than TCP,
# so the reward itself -- not just routing rules -- reflects that
# latency-sensitive traffic should avoid congestion more aggressively.
UDP_PENALTY_MULTIPLIER = 2.0

# Assumed link capacity (bytes/sec), must match fattree_topo.py's
# LINK_BW_MBPS (10 Mbps) -- used only to bucket congestion into
# low/med/high, not for exact accounting.
LINK_CAPACITY_BPS = 10 * 1_000_000 / 8


def _distances_from(dst, adjacency=ADJACENCY):
    """BFS distance from every reachable node to dst (graph is
    undirected/symmetric, so this doubles as distance-to-dst)."""
    dist = {dst: 0}
    queue = deque([dst])
    while queue:
        node = queue.popleft()
        for neighbor in adjacency.get(node, {}).values():
            if neighbor not in dist:
                dist[neighbor] = dist[node] + 1
                queue.append(neighbor)
    return dist


def _congestion_bucket(rate_bps):
    frac = rate_bps / LINK_CAPACITY_BPS
    if frac < 0.3:
        return 0
    elif frac < 0.7:
        return 1
    return 2


def _class_penalty(flow_class):
    return UDP_PENALTY_MULTIPLIER if flow_class == 'udp' else 1.0


class PathSelector:
    def __init__(self):
        env = os.environ
        self.state_mode = env.get('SDN_QL_STATE', 'busiest')
        if self.state_mode not in ('busiest', 'per_link'):
            raise ValueError("SDN_QL_STATE must be 'busiest' or 'per_link'")
        self.tiebreak = env.get('SDN_QL_TIEBREAK', 'first')
        self.pending = PendingLoad() if env.get('SDN_QL_PENDING') == '1' else None
        self.freeze = env.get('SDN_QL_FREEZE') == '1'
        eps_env = env.get('SDN_QL_EPS')
        self.eps_fixed = (float(eps_env) if eps_env not in (None, '')
                          else (0.0 if self.freeze else None))
        self.save_path = env.get('SDN_QL_SAVE') or None
        self.load_path = env.get('SDN_QL_LOAD') or None
        self.save_every = int(env.get('SDN_QL_SAVE_EVERY', '5'))

        # Q[(current, dst, congestion_bucket, flow_class)][next_node] = value
        self.q_table = defaultdict(lambda: defaultdict(float))
        self.start_time = time.time()
        self.decision_count = 0
        self.last_info = None  # read by the controller for the dashboard

        if self.load_path:
            self._load(self.load_path)

        self._log_file = None
        self._log_writer = None
        if not self.freeze:
            os.makedirs(TRAINING_LOG_DIR, exist_ok=True)
            ts = datetime.now().strftime('%Y%m%d_%H%M%S')
            self._log_path = os.path.join(TRAINING_LOG_DIR, f'q_learning_training_{ts}.csv')
            self._log_file = open(self._log_path, 'w', newline='')
            self._log_writer = csv.writer(self._log_file)
            self._log_writer.writerow(
                ['elapsed_seconds', 'epsilon', 'q_table_size', 'avg_reward_this_update',
                 'flow_class']
            )
            self._log_file.flush()

    # -- Q-table persistence (JSON; tuple keys flattened to lists) --
    def _save(self):
        if not self.save_path:
            return
        data = {
            'state_mode': self.state_mode,
            'decisions': self.decision_count,
            'q': [[list(k), {str(a): v for a, v in acts.items()}]
                  for k, acts in self.q_table.items() if acts],
        }
        tmp = self.save_path + '.tmp'
        d = os.path.dirname(self.save_path)
        if d:
            os.makedirs(d, exist_ok=True)
        with open(tmp, 'w') as f:
            json.dump(data, f)
        os.replace(tmp, self.save_path)

    def _load(self, path):
        if not os.path.exists(path):
            raise RuntimeError(f"SDN_QL_LOAD file not found: {path} "
                               f"(train first with experiments/train_ql.py)")
        with open(path) as f:
            data = json.load(f)
        if data.get('state_mode') != self.state_mode:
            raise RuntimeError(
                f"Q-table was trained with state '{data.get('state_mode')}' "
                f"but SDN_QL_STATE is '{self.state_mode}'")
        for k, acts in data['q']:
            bucket = tuple(k[2]) if isinstance(k[2], list) else k[2]
            key = (k[0], k[1], bucket, k[3])
            for a, v in acts.items():
                self.q_table[key][int(a)] = v

    def _epsilon(self):
        if self.eps_fixed is not None:
            return self.eps_fixed
        elapsed = time.time() - self.start_time
        return EPS_MIN + (EPS_MAX - EPS_MIN) * math.exp(-EPS_DECAY_RATE * elapsed)

    def _link_rate(self, node, next_node, port_stats):
        port = port_toward(node, next_node)
        if port is None:
            return 0.0
        rate = port_stats.get(node, {}).get(port, {}).get('tx_rate', 0.0)
        if self.pending is not None:
            rate += self.pending.extra(node, next_node)
        return rate

    def _valid_next_hops(self, node, dist_map):
        """Neighbors that make real progress toward dst (dist decreases
        by exactly 1) -- keeps paths loop-free and shortest-length,
        same guarantee the baselines' all_shortest_paths() gives."""
        if node not in dist_map:
            return []
        return [
            n for n in ADJACENCY.get(node, {}).values()
            if dist_map.get(n, float('inf')) == dist_map[node] - 1
        ]

    def _bucket(self, node, candidates, port_stats):
        if self.state_mode == 'per_link':
            return tuple(_congestion_bucket(self._link_rate(node, c, port_stats))
                         for c in sorted(candidates))
        if not candidates:
            return 0
        return _congestion_bucket(
            max(self._link_rate(node, c, port_stats) for c in candidates))

    def _state(self, node, dst, dist_map, port_stats, flow_class):
        candidates = self._valid_next_hops(node, dist_map)
        return (node, dst, self._bucket(node, candidates, port_stats), flow_class)

    def pick_path(self, src, dst, logger=None, port_stats=None, flow_class=None):
        port_stats = port_stats or {}
        dist_map = _distances_from(dst)
        if src not in dist_map:
            return None

        epsilon = self._epsilon()
        path = [src]
        trajectory = []  # list of (state, action) taken, in order
        explored_hops = 0
        current = src
        max_hops = len(ADJACENCY) + 2  # safety cutoff, avoids infinite loops

        while current != dst and len(path) < max_hops:
            candidates = self._valid_next_hops(current, dist_map)
            if not candidates:
                break  # shouldn't happen in a connected topology

            state = self._state(current, dst, dist_map, port_stats, flow_class)

            if random.random() < epsilon:
                action = random.choice(candidates)  # explore
                explored_hops += 1
            else:
                q_values = self.q_table[state]
                if self.tiebreak == 'random':
                    best = max(q_values[a] for a in candidates)
                    action = random.choice([a for a in candidates if q_values[a] == best])
                else:
                    action = max(candidates, key=lambda a: q_values[a])  # exploit

            trajectory.append((state, action))
            path.append(action)
            current = action

        rewards_this_call = []
        if not self.freeze:
            # --- Multi-step Bellman backup, walked backward through the
            # --- hops we just took, so value propagates from the
            # --- destination back toward the source over many decisions.
            penalty = _class_penalty(flow_class)
            for state, action in reversed(trajectory):
                link_rate = self._link_rate(state[0], action, port_stats)
                reward = -(link_rate / LINK_CAPACITY_BPS) * penalty - PER_HOP_COST

                if action == dst:
                    max_next_q = 0.0
                else:
                    next_candidates = self._valid_next_hops(action, dist_map)
                    if next_candidates:
                        next_state = (action, dst,
                                      self._bucket(action, next_candidates, port_stats),
                                      flow_class)
                        max_next_q = max(self.q_table[next_state][a] for a in next_candidates) \
                            if self.q_table[next_state] else 0.0
                    else:
                        max_next_q = 0.0

                old_q = self.q_table[state][action]
                self.q_table[state][action] = old_q + ALPHA * (
                    reward + GAMMA * max_next_q - old_q
                )
                rewards_this_call.append(reward)

        if self.pending is not None and current == dst and len(path) > 1:
            self.pending.add(path, flow_class)
        self.decision_count += 1
        self.last_info = {
            'epsilon': round(epsilon, 4), 'explored_hops': explored_hops,
            'hops': len(trajectory), 'q_table_size': len(self.q_table),
            'state': self.state_mode, 'frozen': self.freeze,
            'pending': self.pending is not None,
        }
        if (not self.freeze and self.save_path
                and self.decision_count % self.save_every == 0):
            self._save()
        if trajectory and not self.freeze:
            avg_reward = sum(rewards_this_call) / len(rewards_this_call)
            self._log_writer.writerow([
                f'{time.time() - self.start_time:.2f}',
                f'{epsilon:.4f}',
                len(self.q_table),
                f'{avg_reward:.4f}',
                flow_class,
            ])
            self._log_file.flush()
        if logger and current == dst:
            logger.info(
                "QL: %s -> %s | class=%s | eps=%.3f | chose %s | q_table size=%d",
                src, dst, flow_class, epsilon, path, len(self.q_table)
            )

        return path if current == dst else None
