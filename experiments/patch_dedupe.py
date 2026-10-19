#!/usr/bin/env python3
"""Make the controller decide ONCE per flow.
Run from ~/Documents/sdn-rl:  python3 experiments/patch_dedupe.py
Safe to run twice. Makes controller.py.bak4 first."""
import os, shutil, sys

PATH = 'controller.py'
MARK = 'SDN_PATH_CACHE_TTL'
src = open(PATH).read()
if MARK in src:
    print('Already patched.'); sys.exit(0)

A1 = "        pick_kwargs = {'src': dpid, 'dst': dst_switch, 'logger': self.logger,"
B1 = '''        # --- decide once per flow: repeat packet-ins reuse the first decision ---
        _ttl = float(os.environ.get('SDN_PATH_CACHE_TTL', '5'))
        _key = tuple(sorted(match_fields.items()))
        _cache = self.__dict__.setdefault('_flow_path_cache', {})
        _now = time.time()
        for _k in [k for k, v in _cache.items() if _now - v[1] > _ttl]:
            del _cache[_k]
        _hit = _cache.get(_key)
        if _hit is not None and dpid in _hit[0]:
            _sub = _hit[0][_hit[0].index(dpid):]
            _port = port_toward(_sub[0], _sub[1]) if len(_sub) > 1 else dst_port
            _data = msg.data if msg.buffer_id == ofproto.OFP_NO_BUFFER else None
            datapath.send_msg(parser.OFPPacketOut(
                datapath=datapath, buffer_id=msg.buffer_id,
                in_port=msg.match['in_port'],
                actions=[parser.OFPActionOutput(_port)], data=_data))
            return

'''
A2 = "        for i, node in enumerate(path):\n            if i < len(path) - 1:\n                out_port = port_toward(node, path[i + 1])"
B2 = "        _cache[_key] = (list(path), _now)\n\n"
A3 = "    def _evict_flows_for_link(self, node_a, node_b):\n"
B3 = "        self.__dict__.setdefault('_flow_path_cache', {}).clear()\n"

for a in (A1, A2, A3):
    if src.count(a) != 1:
        print('ANCHOR NOT FOUND (count=%d):\n%s' % (src.count(a), a)); sys.exit(1)
if not any(l.startswith('import os') for l in src.splitlines()):
    print('controller.py has no "import os"'); sys.exit(1)

shutil.copy(PATH, PATH + '.bak4')
src = src.replace(A1, B1 + A1)
src = src.replace(A2, B2 + A2)
src = src.replace(A3, A3 + B3)
open(PATH, 'w').write(src)
print('Patched. Backup: controller.py.bak4')
