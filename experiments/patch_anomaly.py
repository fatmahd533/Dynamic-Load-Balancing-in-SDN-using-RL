"""
Makes the anomaly layer safe. Run once from the project root:
    python3 experiments/patch_anomaly.py
Backs up controller.py to controller.py.bak3 and changes only the anomaly code:
  1. Hysteresis: a link is excluded only after ANOMALY_CONFIRM consecutive
     anomalous readings, and restored only after ANOMALY_CLEAR consecutive
     normal readings.
  2. Partition guard: a link is never excluded for an anomaly if that would
     leave either of its switches with no inter-switch link at all.
"""
import os
import shutil
import sys

path = 'controller.py'
s = open(path).read()
if 'ANOMALY_CONFIRM' in s:
    sys.exit("Already patched.")

def rep(old, new):
    global s
    if s.count(old) != 1:
        sys.exit("Could not find the expected code block (controller.py differs "
                 "from what I expect). Nothing was changed.\nMissing:\n" + old[:120])
    s = s.replace(old, new)

rep("from stats_logger import StatsLogger\n",
    "from stats_logger import StatsLogger\nfrom topology import ADJACENCY\n")

rep("ANOMALY_MODEL_PATH = os.path.join('models', 'anomaly_model.joblib')\n",
    "ANOMALY_MODEL_PATH = os.path.join('models', 'anomaly_model.joblib')\n"
    "ANOMALY_CONFIRM = 3    # consecutive anomalous readings before a link is excluded\n"
    "ANOMALY_CLEAR = 10     # consecutive normal readings before it is restored\n")

rep("        self._anomalous_ports = set()  # (dpid, port) flagged on most recent poll\n",
    "        self._anomalous_ports = set()  # (dpid, port) currently confirmed anomalous\n"
    "        self._anom_streak = defaultdict(int)    # (dpid, port) -> consecutive anomalous readings\n"
    "        self._normal_streak = defaultdict(int)  # (dpid, port) -> consecutive normal readings\n")

rep("""                is_anom = self._check_port_anomaly(tx_rate, rx_rate)
                key = (dpid, stat.port_no)
                if is_anom:
                    self._anomalous_ports.add(key)
                else:
                    self._anomalous_ports.discard(key)
                self._update_link_anomaly_status(dpid, stat.port_no)
""", """                is_anom = self._check_port_anomaly(tx_rate, rx_rate)
                key = (dpid, stat.port_no)
                if is_anom:
                    self._anom_streak[key] += 1
                    self._normal_streak[key] = 0
                    if self._anom_streak[key] >= ANOMALY_CONFIRM:
                        self._anomalous_ports.add(key)
                else:
                    self._normal_streak[key] += 1
                    self._anom_streak[key] = 0
                    if self._normal_streak[key] >= ANOMALY_CLEAR:
                        self._anomalous_ports.discard(key)
                self._update_link_anomaly_status(dpid, stat.port_no)
""")

rep("""        if link_anomalous:
            changed = self._mark_link_down(dpid, neighbor, 'anomaly')
""", """        if link_anomalous:
            key = frozenset((dpid, neighbor))
            already = 'anomaly' in self._link_down_reasons.get(key, set())
            if not already and (len(ADJACENCY.get(dpid, {})) <= 1 or
                                len(ADJACENCY.get(neighbor, {})) <= 1):
                return  # would cut a switch off completely: never exclude it
            changed = self._mark_link_down(dpid, neighbor, 'anomaly')
""")

shutil.copy(path, path + '.bak3')
open(path, 'w').write(s)
print("Patched controller.py (backup: controller.py.bak3).")
