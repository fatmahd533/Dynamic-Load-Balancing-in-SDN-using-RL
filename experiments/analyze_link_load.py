"""
Link-load metrics from a controller stats CSV (stats/port_stats_*.csv),
for a chosen time window. This is the direct measure of load balancing:
how evenly the offered traffic is spread over the fabric's links.

Metrics (all over directed switch-to-switch links, utilisation = rate /
link capacity, capacity = 10 Mbps):
  max_link_mean_util  busiest link's mean utilisation in the window
  max_link_peak_util  busiest single sample on any link
  uplink_jain         Jain's fairness index over the 16 upward links
                      (edge->aggregation and aggregation->core);
                      1.0 = perfectly even, 1/16 = everything on one link
  n_links_used        directed links with mean utilisation > 5%
  min_samples         fewest stats samples any link had in the window
  bad_samples         samples discarded as implausible (> 3x capacity)

Usage:  python3 experiments/analyze_link_load.py stats/port_stats_X.csv [t_from t_to]
"""

import csv
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
from topology import ADJACENCY  # noqa: E402

CAP_BPS = 10e6 / 8.0       # 10 Mbps in bytes/second
MAX_PLAUSIBLE = 3.0        # x capacity; larger samples are counter artefacts
BUSY = 0.05

LINKS = [(dpid, port, nb) for dpid, ports in ADJACENCY.items()
         for port, nb in ports.items()]


def tier(dpid):
    if 1 <= dpid <= 4:
        return 'a'
    if 5 <= dpid <= 6:
        return 'c'
    return 'e'


def is_up(a, b):
    return (tier(a), tier(b)) in (('e', 'a'), ('a', 'c'))


def jain(values):
    vals = list(values)
    sq = sum(v * v for v in vals)
    if not vals or sq == 0:
        return None
    return sum(vals) ** 2 / (len(vals) * sq)


def load_samples(path, cap=CAP_BPS):
    samples, bad = {}, 0
    with open(path) as f:
        for r in csv.DictReader(f):
            try:
                t = float(r['timestamp'])
                d = int(r['dpid'])
                p = int(r['port'])
                tx = float(r['tx_rate_Bps'])
            except (ValueError, KeyError, TypeError):
                continue
            if tx < 0 or tx > MAX_PLAUSIBLE * cap:
                bad += 1
                continue
            samples.setdefault((d, p), []).append((t, tx))
    return samples, bad


def link_load_metrics(path, t_from=None, t_to=None, cap=CAP_BPS):
    samples, bad = load_samples(path, cap)
    lo = -float('inf') if t_from is None else t_from
    hi = float('inf') if t_to is None else t_to
    per_link = {}
    for dpid, port, nb in LINKS:
        xs = [tx for (t, tx) in samples.get((dpid, port), []) if lo <= t <= hi]
        if xs:
            per_link[(dpid, port, nb)] = (sum(xs) / len(xs) / cap, max(xs) / cap, len(xs))
    if not per_link:
        return None
    means = [v[0] for v in per_link.values()]
    up = [v[0] for k, v in per_link.items() if is_up(k[0], k[2])]
    return {
        'max_link_mean_util': max(means),
        'max_link_peak_util': max(v[1] for v in per_link.values()),
        'uplink_jain': jain(up),
        'n_links_used': sum(1 for m in means if m > BUSY),
        'min_samples': min(v[2] for v in per_link.values()),
        'bad_samples': bad,
        'per_link': per_link,
    }


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    t_from = float(sys.argv[2]) if len(sys.argv) > 2 else None
    t_to = float(sys.argv[3]) if len(sys.argv) > 3 else None
    m = link_load_metrics(sys.argv[1], t_from, t_to)
    if m is None:
        print("No samples in that window.")
        sys.exit(1)
    for k in ('max_link_mean_util', 'max_link_peak_util', 'uplink_jain',
              'n_links_used', 'min_samples', 'bad_samples'):
        v = m[k]
        print(f"{k:20s} {v:.3f}" if isinstance(v, float) else f"{k:20s} {v}")
    print("\nBusiest links (dpid port -> neighbour): mean% / peak% / samples")
    top = sorted(m['per_link'].items(), key=lambda kv: -kv[1][0])[:8]
    for (d, p, nb), (mean, peak, n) in top:
        print(f"  {d:>2} {p} -> {nb:<2}  {mean*100:6.1f} / {peak*100:6.1f} / {n}")


if __name__ == '__main__':
    main()
