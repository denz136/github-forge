"""Lifecycle test: forge registers with the env, serves, then exits 0 when /done flips."""

import json
import subprocess
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, '/home/agentuser/makefun/github-forge')
from test_driver import mock_env, start_forge

state = {'done': False}


class Mock(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get('Content-Length', 0))
        body = json.loads(self.rfile.read(length) or b'{}')
        state['registered'] = body
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(b'{"ok":true}')

    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(json.dumps({'done': state['done']}).encode())

    def log_message(self, *a):
        pass


srv = ThreadingHTTPServer(('127.0.0.1', 38170), Mock)
threading.Thread(target=srv.serve_forever, daemon=True).start()

os_env = {'MAKEFUN_SHOP_TOKEN': 'tok123', 'MAKEFUN_ENV_URL': 'http://127.0.0.1:38170'}
p = subprocess.Popen(['python3', '/home/agentuser/makefun/github-forge/main.py'], env=os_env,
                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
deadline = time.time() + 10
while time.time() < deadline and 'registered' not in state:
    time.sleep(0.05)
assert 'registered' in state, 'forge never registered'
print('registered:', state['registered'])
assert isinstance(state['registered'].get('port'), int)

time.sleep(0.5)
state['done'] = True
rc = p.wait(timeout=15)
print('exit code:', rc)
assert rc == 0, f'expected exit 0, got {rc}'
srv.shutdown()
print('LIFECYCLE OK')
