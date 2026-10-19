"""
Analyzes the output of q_learning_convergence_test.py. Two data
sources, answering two different questions:

1. The controller's captured stdout log (passed as argv[1]) -- every
   "QL: ..." decision line includes epsilon, which strictly decreases
   with wall-clock time regardless of decision count. This lets us
   identify "late in training" decisions by epsilon threshold, without
   needing timestamps the raw Ryu log doesn't reliably include.
   Used to answer: once epsilon is low, does the agent make the SAME
   decision when it sees the same (or near-same) state again? This is
   the actual "did it learn a policy" question -- q_table size going up
   only shows exploration happened, not that it converged to anything
   useful.

2. The algorithm's own results/q_learning_training_*.csv (auto-located
   as the most recently modified matching file) -- has real
   elapsed_seconds and avg_reward_this_update columns. Used to answer:
   did average reward improve (get less negative) as training
   progressed, which is what "learning" should look like on a reward
   curve.

Run with: python3 experiments/analyze_qlearning_log.py ryu_qlearning_convergence.log
"""

import sys
import os
import re
import glob
import csv
from collections import defaultdict, Counter

QL_LINE = re.compile(
    r'QL: (\d+) -> (\d+) \| class=(\w+|None) \| eps=([\d.]+) \| '
    r'chose (\[[\d, ]+\]) \| q_table size=(\d+)'
)

LOW_EPS_THRESHOLD = 0.15  # "late in training" cutoff


def parse_ryu_log(path):
    records = []
    with open(path) as f:
        for line in f:
            m = QL_LINE.search(line)
            if not m:
                continue
            src, dst, flow_class, eps, path_str, qsize = m.groups()
            records.append({
                'src': int(src), 'dst': int(dst), 'class': flow_class,
                'eps': float(eps), 'path': path_str, 'qsize': int(qsize),
            })
    return records


def analyze_policy_consistency(records):
    print("\n=== Policy consistency (decisions with eps <= %.2f) ===" % LOW_EPS_THRESHOLD)
    low_eps = [r for r in records if r['eps'] <= LOW_EPS_THRESHOLD]
    if not low_eps:
        print(f"  No decisions found with eps <= {LOW_EPS_THRESHOLD} -- "
              f"training likely didn't run long enough. Final epsilon "
              f"observed: {records[-1]['eps'] if records else 'N/A'}")
        return

    print(f"  {len(low_eps)} low-epsilon decisions found "
          f"(out of {len(records)} total)")

    groups = defaultdict(list)
    for r in low_eps:
        groups[(r['src'], r['dst'], r['class'])].append(r['path'])

    print(f"\n  {'src->dst (class)':<22} {'n':>4} {'mode path':<28} {'consistency':>12}")
    print("  " + "-" * 70)
    overall_consistency = []
    for (src, dst, cls), paths in sorted(groups.items()):
        if len(paths) < 3:
            continue
        counts = Counter(paths)
        mode_path, mode_count = counts.most_common(1)[0]
        consistency = mode_count / len(paths)
        overall_consistency.append(consistency)
        print(f"  {src}->{dst} ({cls}){'':<8} {len(paths):>4} "
              f"{mode_path:<28} {consistency*100:>10.1f}%")

    if overall_consistency:
        avg = sum(overall_consistency) / len(overall_consistency)
        print(f"\n  Average policy consistency across groups with >=3 "
              f"observations: {avg*100:.1f}%")
        print("  (100% = always picked the same path once epsilon was low; "
              "closer to 1/n_candidates = still essentially random)")
    else:
        print("\n  No (src,dst,class) group had >=3 low-epsilon observations -- "
              "increase --eval-reps or --train-seconds and rerun.")


def find_latest_training_csv():
    candidates = glob.glob('results/q_learning_training_*.csv')
    if not candidates:
        return None
    return max(candidates, key=os.path.getmtime)


def analyze_reward_trend(csv_path):
    print(f"\n=== Reward trend (from {csv_path}) ===")
    rows = []
    with open(csv_path) as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                rows.append({
                    'elapsed': float(row['elapsed_seconds']),
                    'reward': float(row['avg_reward_this_update']),
                })
            except (ValueError, KeyError):
                continue

    if len(rows) < 10:
        print(f"  Only {len(rows)} rows found -- not enough for a trend. "
              f"Was this really the active training session's CSV?")
        return

    max_elapsed = rows[-1]['elapsed']
    early = [r['reward'] for r in rows if r['elapsed'] <= max_elapsed * 0.2]
    late = [r['reward'] for r in rows if r['elapsed'] >= max_elapsed * 0.8]

    avg_early = sum(early) / len(early) if early else None
    avg_late = sum(late) / len(late) if late else None

    print(f"  Total elapsed: {max_elapsed:.1f}s, {len(rows)} logged updates")
    print(f"  Avg reward, first 20% of training: {avg_early:.4f} (n={len(early)})"
          if avg_early is not None else "  Not enough early data")
    print(f"  Avg reward, last 20% of training:  {avg_late:.4f} (n={len(late)})"
          if avg_late is not None else "  Not enough late data")
    if avg_early is not None and avg_late is not None:
        delta = avg_late - avg_early
        direction = "IMPROVED (less negative)" if delta > 0 else "WORSENED or flat"
        print(f"  Change: {delta:+.4f} -- {direction}")
        print("  (Reward is negative congestion + a small per-hop cost, so "
              "'improved' means routes got less congested over training, "
              "not that reward became positive.)")


def main():
    if len(sys.argv) < 2:
        print("Usage: python3 experiments/analyze_qlearning_log.py <ryu_log_file>")
        sys.exit(1)

    log_path = sys.argv[1]
    if not os.path.exists(log_path):
        print(f"Log file not found: {log_path}")
        sys.exit(1)

    records = parse_ryu_log(log_path)
    print(f"Parsed {len(records)} QL decision lines from {log_path}")
    if not records:
        print("No QL lines found -- check this is the right log file, and "
              "that SDN_ALGORITHM=q_learning was actually set.")
        sys.exit(1)

    print(f"Epsilon range observed: {records[0]['eps']:.3f} (start) "
          f"-> {records[-1]['eps']:.3f} (end)")
    print(f"Final q_table size: {records[-1]['qsize']}")

    analyze_policy_consistency(records)

    csv_path = find_latest_training_csv()
    if csv_path:
        analyze_reward_trend(csv_path)
    else:
        print("\nNo results/q_learning_training_*.csv found -- skipping reward trend.")


if __name__ == '__main__':
    main()
