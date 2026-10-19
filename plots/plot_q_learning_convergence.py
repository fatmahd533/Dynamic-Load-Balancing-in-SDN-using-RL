"""
Two-panel convergence plot for Q-learning training:
  Top: h2's achieved throughput per trial (from results/baseline_comparison.csv,
       filtered to algorithm='q_learning'), showing whether performance
       improves as training progresses.
  Bottom: epsilon decay over wall-clock time (from the q_learning
       training log), showing the shift from exploration to exploitation.

Run with: python3 plots/plot_q_learning_convergence.py
"""

import csv
import glob
import statistics

import matplotlib.pyplot as plt

RESULTS_FILE = 'results/baseline_comparison.csv'
OUTPUT_FILE = 'plots/q_learning_convergence.png'


def load_throughput_trials():
    trials, throughputs = [], []
    with open(RESULTS_FILE, 'r') as f:
        for row in csv.DictReader(f):
            if row['algorithm'] == 'q_learning' and row['h2_throughput_mbps']:
                trials.append(int(row['trial']))
                throughputs.append(float(row['h2_throughput_mbps']))
    return trials, throughputs


def load_latest_training_log():
    files = sorted(glob.glob('results/q_learning_training_*.csv'))
    if not files:
        return [], []
    latest = files[-1]
    elapsed, epsilon = [], []
    with open(latest, 'r') as f:
        for row in csv.DictReader(f):
            elapsed.append(float(row['elapsed_seconds']))
            epsilon.append(float(row['epsilon']))
    return elapsed, epsilon


def main():
    trials, throughputs = load_throughput_trials()
    elapsed, epsilon = load_latest_training_log()

    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(8, 8))

    if trials:
        ax1.plot(trials, throughputs, marker='o', color='#0F6E56')
        ax1.axhline(statistics.mean(throughputs), color='gray', linestyle='--',
                     label=f'mean = {statistics.mean(throughputs):.2f} Mbps')
        ax1.set_xlabel('Trial number')
        ax1.set_ylabel('h2 -> h6 throughput (Mbps)')
        ax1.set_title('Q-learning: measured throughput per trial')
        ax1.legend()
    else:
        ax1.text(0.5, 0.5, 'No q_learning rows found in baseline_comparison.csv',
                  ha='center', va='center')

    if elapsed:
        ax2.plot(elapsed, epsilon, color='#993556')
        ax2.set_xlabel('Elapsed training time (seconds)')
        ax2.set_ylabel('Epsilon (exploration rate)')
        ax2.set_title('Epsilon decay over wall-clock time')
    else:
        ax2.text(0.5, 0.5, 'No training log found', ha='center', va='center')

    plt.tight_layout()
    plt.savefig(OUTPUT_FILE, dpi=150)
    print(f"Saved to {OUTPUT_FILE}")
    if trials:
        print(f"Trials: {len(trials)}, mean throughput: {statistics.mean(throughputs):.2f} Mbps, "
              f"std: {statistics.stdev(throughputs) if len(throughputs)>1 else 0:.3f}")
    if epsilon:
        print(f"Epsilon: started at {epsilon[0]:.3f}, ended at {epsilon[-1]:.3f}")


if __name__ == '__main__':
    main()
