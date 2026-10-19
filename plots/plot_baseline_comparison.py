"""
Reads results/baseline_comparison.csv (produced by
experiments/multi_run_baseline.py) and plots h2's achieved throughput
per algorithm as a bar chart with error bars (mean +/- std across
trials), plus the individual trial points overlaid as dots.

Run with: python3 plots/plot_baseline_comparison.py
(No sudo needed -- this just reads a CSV and draws a chart.)
"""

import csv
from collections import defaultdict
import statistics

import matplotlib.pyplot as plt

RESULTS_FILE = 'results/baseline_comparison.csv'
OUTPUT_FILE = 'plots/baseline_comparison.png'


def main():
    data = defaultdict(list)
    with open(RESULTS_FILE, 'r') as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row['h2_throughput_mbps']:
                data[row['algorithm']].append(float(row['h2_throughput_mbps']))

    if not data:
        print("No data found in", RESULTS_FILE)
        return

    algorithms = sorted(data.keys())
    means = [statistics.mean(data[a]) for a in algorithms]
    stds = [statistics.stdev(data[a]) if len(data[a]) > 1 else 0 for a in algorithms]

    fig, ax = plt.subplots(figsize=(7, 5))
    bars = ax.bar(algorithms, means, yerr=stds, capsize=8,
                   color=['#6B7280', '#0F6E56'], alpha=0.85)

    for i, algo in enumerate(algorithms):
        jitter_x = [i + 0.02 * (j - len(data[algo]) / 2) for j in range(len(data[algo]))]
        ax.scatter(jitter_x, data[algo], color='black', zorder=3, s=25, alpha=0.6)

    ax.set_ylabel('h2 -> h6 achieved throughput (Mbps)')
    ax.set_title('Baseline comparison: h2\'s throughput while h1 congests the shared path')
    ax.axhline(8, color='gray', linestyle='--', linewidth=1, label='iperf target (8 Mbps)')
    ax.legend()
    ax.set_ylim(0, 9)

    for i, (m, s) in enumerate(zip(means, stds)):
        ax.text(i, m + s + 0.15, f'{m:.2f} ± {s:.2f}', ha='center', fontsize=9)

    plt.tight_layout()
    plt.savefig(OUTPUT_FILE, dpi=150)
    print(f"Saved plot to {OUTPUT_FILE}")
    print("\nSummary:")
    for a in algorithms:
        print(f"  {a}: n={len(data[a])}, mean={statistics.mean(data[a]):.2f} Mbps, "
              f"std={stds[algorithms.index(a)]:.2f}")


if __name__ == '__main__':
    main()
