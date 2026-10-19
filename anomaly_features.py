"""
Shared feature extraction for the anomaly-detection extension, used
identically by experiments/train_anomaly_model.py (offline fit) and
controller.py (live scoring) -- kept in one place so the two can never
silently drift out of sync with each other.

log1p compresses the heavy-tailed distribution of link rates observed
across this project's tests (values from ~50 B/s up to ~600,000 B/s),
so the Isolation Forest isn't dominated by raw magnitude alone.
"""

import numpy as np

FEATURE_NAMES = ['log_tx_rate', 'log_rx_rate']


def extract_features(tx_rate_bps, rx_rate_bps):
    tx = max(0.0, float(tx_rate_bps))
    rx = max(0.0, float(rx_rate_bps))
    return np.array([np.log1p(tx), np.log1p(rx)])
