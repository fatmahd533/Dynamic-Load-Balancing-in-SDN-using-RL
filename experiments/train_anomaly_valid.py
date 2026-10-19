"""
Retrains the anomaly model on VALID data only: the port-stats files of the
capacity-limited experiments (listed in results/gate_runs*.csv). Run from the
project root:
    python3 experiments/train_anomaly_valid.py
Writes models/anomaly_model.joblib (this ENABLES anomaly detection the next
time the controller starts; move it to .off again for routing experiments).
"""
import csv
import glob
import os
import sys

sys.path.insert(0, '.')
sys.path.insert(0, 'experiments')
import numpy as np
import joblib
from sklearn.ensemble import IsolationForest
from topology import neighbor_via_port
from anomaly_features import extract_features

files = set()
for p in glob.glob('results/gate_runs*.csv'):
    for r in csv.DictReader(open(p)):
        f = r.get('stats_csv')
        if f and os.path.exists(f):
            files.add(f)
if not files:
    sys.exit("No stats files found via results/gate_runs*.csv")
X = []
for f in sorted(files):
    for row in csv.DictReader(open(f)):
        dpid, port = int(row['dpid']), int(row['port'])
        if neighbor_via_port(dpid, port) is None:
            continue
        tx, rx = float(row['tx_rate_Bps']), float(row['rx_rate_Bps'])
        if tx > 2e6 or rx > 2e6:   # above 10 Mbps line rate: corrupt sample
            continue
        X.append(extract_features(tx, rx))
X = np.array(X)
print(f"{len(files)} stats files, {len(X)} samples")
model = IsolationForest(n_estimators=100, contamination=0.01, random_state=42).fit(X)
flagged = int((model.predict(X) == -1).sum())
print(f"flagged in training data: {flagged}/{len(X)}")
for name, tx in [('idle', 100), ('4 Mbps', 5e5), ('line rate 10 Mbps', 1.2e6)]:
    print(f"  {name:18s} -> {'ANOMALOUS' if model.predict(extract_features(tx, tx).reshape(1, -1))[0] == -1 else 'normal'}")
os.makedirs('models', exist_ok=True)
joblib.dump(model, 'models/anomaly_model.joblib')
print("Saved models/anomaly_model.joblib")
