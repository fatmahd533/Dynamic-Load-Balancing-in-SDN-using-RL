"""
Writes port-statistics snapshots to CSV as the controller polls switches.
One CSV per run, named with the algorithm and timestamp, so different
runs never overwrite each other. This is the raw data future KPI
calculations (throughput, utilization) and plots will read from.

Sanitising (so one bad poll can't look like infinite congestion):
  * reserved/virtual ports (OFPP_LOCAL etc., port >= 0xffffff00) are ignored
  * "counter unavailable" sentinels (2**64-1 from OVS) are ignored; the
    last known rate is returned instead
  * a counter that goes backwards (reset) yields rate 0 for that sample
  * two samples closer than MIN_DT seconds are not used for a rate
"""

import csv
import os
import time
from datetime import datetime

STATS_DIR = 'stats'
OFPP_MAX = 0xffffff00      # ports above this are virtual (LOCAL, CONTROLLER...)
COUNTER_SENTINEL = 2 ** 63  # OVS reports 2**64-1 for "not available"
MIN_DT = 0.1               # seconds


class StatsLogger:
    def __init__(self, algorithm_name):
        os.makedirs(STATS_DIR, exist_ok=True)
        ts = datetime.now().strftime('%Y%m%d_%H%M%S')
        self.filepath = os.path.join(STATS_DIR, f'port_stats_{algorithm_name}_{ts}.csv')
        self._file = open(self.filepath, 'w', newline='')
        self._writer = csv.writer(self._file)
        self._writer.writerow(
            ['timestamp', 'dpid', 'port', 'tx_bytes', 'rx_bytes',
             'tx_rate_Bps', 'rx_rate_Bps']
        )
        self._file.flush()
        self._prev = {}       # (dpid, port) -> (tx_bytes, rx_bytes, time)
        self._last_rate = {}  # (dpid, port) -> (tx_rate, rx_rate)

    def log(self, dpid, port, tx_bytes, rx_bytes):
        key = (dpid, port)
        last = self._last_rate.get(key, (0.0, 0.0))

        # Virtual ports and unavailable counters: not real link data.
        if port >= OFPP_MAX:
            return 0.0, 0.0
        if tx_bytes >= COUNTER_SENTINEL or rx_bytes >= COUNTER_SENTINEL:
            return last

        now = time.time()
        tx_rate = rx_rate = 0.0
        if key in self._prev:
            prev_tx, prev_rx, prev_time = self._prev[key]
            dt = now - prev_time
            if dt < MIN_DT:
                return last  # too close to the previous sample to be meaningful
            tx_rate = max(0.0, (tx_bytes - prev_tx) / dt)
            rx_rate = max(0.0, (rx_bytes - prev_rx) / dt)
        self._prev[key] = (tx_bytes, rx_bytes, now)
        self._last_rate[key] = (tx_rate, rx_rate)
        self._writer.writerow(
            [f'{now:.3f}', dpid, port, tx_bytes, rx_bytes,
             f'{tx_rate:.1f}', f'{rx_rate:.1f}']
        )
        self._file.flush()
        return tx_rate, rx_rate

    def close(self):
        self._file.close()
