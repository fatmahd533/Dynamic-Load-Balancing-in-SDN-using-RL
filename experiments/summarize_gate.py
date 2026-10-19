"""
Summarises results/gate_runs.csv: per algorithm mean +/- 95% CI, and the
paired difference against a reference algorithm (same repetition = same
flow pairs). No claim is made beyond what n supports; with n < ~10 treat
the intervals as rough.

Usage: python3 experiments/summarize_gate.py [--file results/gate_runs.csv] [--ref shortest_path]
"""

import argparse
import csv
import math
from collections import defaultdict

METRICS = [  # (column, better direction)
    ('agg_delivered_mbps', 'higher'), ('delivered_ratio', 'higher'),
    ('mean_loss_pct', 'lower'), ('jain_flows', 'higher'),
    ('max_link_mean_util', 'lower'), ('uplink_jain', 'higher'),
    ('bg_delivered_mbps', 'higher')]

T975 = {1: 12.706, 2: 4.303, 3: 3.182, 4: 2.776, 5: 2.571, 6: 2.447, 7: 2.365,
        8: 2.306, 9: 2.262, 10: 2.228, 11: 2.201, 12: 2.179, 13: 2.160,
        14: 2.145, 15: 2.131, 16: 2.120, 17: 2.110, 18: 2.101, 19: 2.093,
        20: 2.086, 25: 2.060, 30: 2.042}


def tcrit(df):
    if df < 1:
        return float('nan')
    keys = [k for k in sorted(T975) if k <= df]
    return T975[keys[-1]] if df < 31 else 1.96


def mean_ci(xs):
    n = len(xs)
    m = sum(xs) / n
    if n < 2:
        return m, float('nan')
    sd = math.sqrt(sum((x - m) ** 2 for x in xs) / (n - 1))
    return m, tcrit(n - 1) * sd / math.sqrt(n)


def num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--file', default='results/gate_runs.csv')
    ap.add_argument('--ref', default='shortest_path')
    args = ap.parse_args()

    by = defaultdict(dict)   # algo -> rep -> row
    for r in csv.DictReader(open(args.file)):
        by[r['algorithm']][r['rep']] = r
    algos = sorted(by)
    print(f"Runs per algorithm: " + ", ".join(f"{a}={len(by[a])}" for a in algos))

    print("\n=== Mean +/- 95% CI per algorithm ===")
    print(f"{'metric':22s}" + "".join(f"{a:>22s}" for a in algos))
    for col, _ in METRICS:
        line = f"{col:22s}"
        for a in algos:
            xs = [num(r.get(col)) for r in by[a].values()]
            xs = [x for x in xs if x is not None]
            if xs:
                m, ci = mean_ci(xs)
                line += f"{m:>11.2f} +/-{ci:<7.2f}"
            else:
                line += f"{'n/a':>22s}"
        print(line)

    if args.ref in by:
        print(f"\n=== Paired difference vs {args.ref} (algorithm - {args.ref}) ===")
        for a in algos:
            if a == args.ref:
                continue
            print(f"\n{a}")
            for col, better in METRICS:
                diffs = []
                for rep, r in by[a].items():
                    ref = by[args.ref].get(rep)
                    x, y = num(r.get(col)), num(ref.get(col)) if ref else None
                    if x is not None and y is not None:
                        diffs.append(x - y)
                if not diffs:
                    continue
                m, ci = mean_ci(diffs)
                good = sum(1 for d in diffs if (d > 0) == (better == 'higher') and d != 0)
                bad = sum(1 for d in diffs if (d < 0) == (better == 'higher') and d != 0)
                print(f"  {col:22s} {m:+9.3f} +/- {ci:<8.3f} "
                      f"better in {good}, worse in {bad}, tied {len(diffs)-good-bad} "
                      f"of {len(diffs)} (want {better})")


if __name__ == '__main__':
    main()
