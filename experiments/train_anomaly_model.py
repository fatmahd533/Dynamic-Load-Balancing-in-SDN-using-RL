"""
Fits an unsupervised Isolation Forest on port-stats from EXISTING,
already-collected experiment runs (stats/port_stats_*.csv) as the
"known-legitimate" traffic distribution -- deliberately includes
low-load, congestion, and burst runs together, not just the cleanest
baseline, so organic contention isn't mistaken for an attack later.
Only inter-switch link ports are used (host-facing ports filtered via
topology.neighbor_via_port()), matching exactly what the live detector
in controller.py scores at inference time.

Caveat worth keeping in mind: "anomalous" here means "statistically
unlike everything already tested," not "malicious" specifically -- a
genuinely new but legitimate traffic pattern not resembling any prior
test could still be flagged.

Saves the fitted model to models/anomaly_model.joblib.

Run with:
    python3 experiments/train_anomaly_model.py
Optional: --pattern 'stats/port_stats_*.csv' --contamination 0.02
"""

import sys
import os
import csv
import glob
import argparse

sys.path.insert(0, '.')
import numpy as np
from sklearn.ensemble import IsolationForest
import joblib

from topology import neighbor_via_port
from anomaly_features import extract_features

MODEL_DIR = 'models'
MODEL_PATH = os.path.join(MODEL_DIR, 'anomaly_model.joblib')


def load_features(pattern):
    files = sorted(glob.glob(pattern))
    if not files:
        print(f"No files matched pattern: {pattern}")
        sys.exit(1)
    features = []
    for path in files:
        with open(path) as f:
            reader = csv.DictReader(f)
            count = 0
            for row in reader:
                dpid = int(row['dpid'])
                port = int(row['port'])
                if neighbor_via_port(dpid, port) is None:
                    continue  # host-facing port, not a routable inter-switch link
                tx_rate = float(row['tx_rate_Bps'])
                rx_rate = float(row['rx_rate_Bps'])
                features.append(extract_features(tx_rate, rx_rate))
                count += 1
        print(f"  {path}: {count} inter-switch samples")
    return files, np.array(features)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--pattern', default='stats/port_stats_*.csv')
    parser.add_argument('--contamination', type=float, default=0.02)
    args = parser.parse_args()

    print(f"Loading training data matching: {args.pattern}")
    files, X = load_features(args.pattern)
    print(f"\nTotal training samples: {len(X)} from {len(files)} files")
    if len(X) < 50:
        print("WARNING: very few samples -- consider this a minimum viable "
              "model; more organic test data would give a fuller picture of "
              "legitimate traffic.")

    model = IsolationForest(n_estimators=100, contamination=args.contamination,
                             random_state=42)
    model.fit(X)

    scores = model.decision_function(X)
    print(f"\nTraining decision_function stats: "
          f"min={scores.min():.4f} mean={scores.mean():.4f} max={scores.max():.4f}")
    n_flagged = int((model.predict(X) == -1).sum())
    print(f"Training samples flagged as outliers by the fitted model: "
          f"{n_flagged}/{len(X)} ({100*n_flagged/len(X):.1f}%) "
          f"-- should roughly match --contamination ({args.contamination})")

    os.makedirs(MODEL_DIR, exist_ok=True)
    joblib.dump(model, MODEL_PATH)
    print(f"\nModel saved to {MODEL_PATH}")
    print("Restart the controller to pick it up: anomaly detection activates "
          "automatically once this file exists.")


if __name__ == '__main__':
    main()
