"""余额卡三态取证：真端点（未绑定 / 有余额 / 账本 0 / 失败）。

用法::

    python tools/balance_states.py [--base http://127.0.0.1:18090]

真端点那一路只读文件中转（key 只进内存、不打印）；其余三态用本地 stdlib 契约服务器，
形状照抄真端点，跑完自动关。输出 JSON，便于原样落盘成证据。
"""
import json, os, sys, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
sys.path.insert(0, '/vol2/@appdata/deepseek.harness/home/DJB联脑/repos/djb-node')
from djb_node.desktop.datasource import LocalDataSource, ProviderError
from djb_node.desktop.nodecloud import NodeCloudClient
from djb_node.desktop.views import build_view

def card(text):
    for line in text.splitlines():
        if line.startswith('[余额（可用）]'):
            return line
    return '(缺卡)'

def note(text):
    return [l for l in text.splitlines() if '余额暂不可用' in l]

out = {}
# ① 真端点：未绑定 → null + node_not_bound
import argparse
parser = argparse.ArgumentParser()
parser.add_argument('--base', default='http://127.0.0.1:18090')
parser.add_argument('--key-file', default='~/.djb/secrets/testnode-client-key')
args = parser.parse_args()
key = open(os.path.expanduser(args.key_file)).read().strip()
live = NodeCloudClient(args.base, key)
out['live_unbound'] = {'raw': live._request_json('node_balance', '/api/v1/node/balance'),
                       'card': card(build_view('earnings', LocalDataSource(home='/tmp/djb-wp6-live', cloud=live).snapshot()).plain_text()),
                       'notes': note(build_view('earnings', LocalDataSource(home='/tmp/djb-wp6-live', cloud=live).snapshot()).plain_text())}

# ② 有余额（本地契约服务器，形状照抄真端点）
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith('/api/v1/node/balance'):
            body = json.dumps({"unit": "micro-USD", "account": "2101",
                               "as_of": "2026-11-01T00:00:00Z", "available_minor": 1234567}).encode()
        elif self.path.startswith('/api/v1/node/earnings'):
            body = json.dumps({"currency": "USDT", "unit": "micro-USD", "settled_minor": 1500000,
                               "pending_minor": 250000, "corrected_minor": 0, "usage_rows": 2,
                               "next_settlement_at": "2026-11-02T00:00:00Z",
                               "rows": [{"request_id": "req-1", "state": "settled",
                                         "amount_minor": 1500000, "settled_at": "2026-11-01T00:00:00Z"}],
                               "page": {"cursor": "", "has_more": False, "next_cursor": None}}).encode()
        elif self.path.startswith('/api/v1/node/tasks') or self.path.startswith('/api/v1/node/receipts'):
            body = json.dumps({"data": [], "page": {"cursor": "", "has_more": False, "next_cursor": None}}).encode()
        elif self.path.startswith('/api/v1/node/profile'):
            body = json.dumps({"node_id": "n_demo", "state": "active", "region": "testnet",
                               "platform": "linux", "bound": True, "last_seen_at": None}).encode()
        else:
            self.send_response(404); self.end_headers(); return
        self.send_response(200); self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body))); self.end_headers(); self.wfile.write(body)
    def log_message(self, *a): pass

httpd = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
threading.Thread(target=httpd.serve_forever, daemon=True).start()
base = 'http://127.0.0.1:%d' % httpd.server_address[1]
bound = LocalDataSource(home='/tmp/djb-wp6-live', cloud=NodeCloudClient(base, 'ck-local'))
text = build_view('earnings', bound.snapshot()).plain_text()
out['bound_with_balance'] = {'card': card(text), 'notes': note(text)}

# ②b 已绑定但账本无记录 → 0（零是事实）
class ZeroHandler(Handler):
    def do_GET(self):
        if self.path.startswith('/api/v1/node/balance'):
            body = json.dumps({"unit": "micro-USD", "account": "2101",
                               "as_of": "2026-11-01T00:00:00Z", "available_minor": 0,
                               "note": "no_balance_record：该账户还没有任何充值/消费凭证，按 0 呈现"}).encode()
            self.send_response(200); self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body))); self.end_headers(); self.wfile.write(body)
            return
        return super().do_GET()

zero = ThreadingHTTPServer(('127.0.0.1', 0), ZeroHandler)
threading.Thread(target=zero.serve_forever, daemon=True).start()
text = build_view('earnings', LocalDataSource(home='/tmp/djb-wp6-live',
        cloud=NodeCloudClient('http://127.0.0.1:%d' % zero.server_address[1], 'ck-local')).snapshot()).plain_text()
out['bound_zero'] = {'card': card(text), 'notes': note(text)}
zero.shutdown()

# ③ 失败（端口不通）
dead = LocalDataSource(home='/tmp/djb-wp6-live', cloud=NodeCloudClient('http://127.0.0.1:1', 'ck-local'))
text = build_view('earnings', dead.snapshot()).plain_text()
out['failed'] = {'card': card(text), 'notes': note(text)}
httpd.shutdown()
print(json.dumps(out, ensure_ascii=False, indent=2))
