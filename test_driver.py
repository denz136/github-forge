"""End-to-end test driver: runs the forge against a mock env and exercises
the REST, git, GraphQL and MCP surfaces with a real git client protocol.
Writes the forge's port to /tmp/forge_port.json so tests can drive it."""

import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

FORGE_DIR = '/home/agentuser/makefun/github-forge'
PORT_FILE = '/tmp/forge_port.json'
TOKEN = 'tok123'

state = {'done': False, 'registered': None}


def mock_env(mock_port):
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(('127.0.0.1', mock_port))
    srv.listen(5)

    def serve():
        while True:
            try:
                conn, _ = srv.accept()
            except OSError:
                return
            try:
                data = b''
                while b'\r\n\r\n' not in data:
                    chunk = conn.recv(65536)
                    if not chunk:
                        break
                    data += chunk
                head, _, body = data.partition(b'\r\n\r\n')
                text = head.decode('latin-1')
                if 'POST /register' in text:
                    state['registered'] = json.loads(body.decode())
                    conn.sendall(b'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 14\r\n\r\n{"ok":true}\r\n\r\n')
                elif 'GET /done' in text:
                    payload = json.dumps({'done': state['done']}).encode()
                    conn.sendall(b'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: ' + str(len(payload)).encode() + b'\r\n\r\n' + payload)
                else:
                    conn.sendall(b'HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\n\r\n')
            except Exception:
                pass
            finally:
                try:
                    conn.close()
                except OSError:
                    pass

    threading.Thread(target=serve, daemon=True).start()
    return srv


def start_forge(mock_port):
    env = dict(os.environ)
    env['MAKEFUN_SHOP_TOKEN'] = TOKEN
    env['MAKEFUN_ENV_URL'] = f'http://127.0.0.1:{mock_port}'
    proc = subprocess.Popen([sys.executable, '-P', '-s', 'main.py'], cwd=FORGE_DIR, env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    # wait until the mock saw the register POST
    for _ in range(100):
        if state['registered'] is not None:
            break
        time.sleep(0.05)
    assert state['registered'] is not None, 'forge did not register'
    port = state['registered']['port']
    return proc, port


def req(port, method, path, token=TOKEN, body=None, headers=None, raw_body=None):
    url = f'http://127.0.0.1:{port}{path}'
    data = raw_body if raw_body is not None else (json.dumps(body).encode() if body is not None else None)
    r = urllib.request.Request(url, method=method, data=data)
    if token:
        r.add_header('Authorization', f'Bearer {token}')
    if body is not None:
        r.add_header('Content-Type', 'application/json')
    for key, value in (headers or {}).items():
        r.add_header(key, value)
    try:
        with urllib.request.urlopen(r, timeout=30) as response:
            return response.status, response.read(), dict(response.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read(), dict(e.headers)


def jreq(port, method, path, **kwargs):
    status, payload, headers = req(port, method, path, **kwargs)
    parsed = None
    if payload:
        try:
            parsed = json.loads(payload)
        except ValueError:
            parsed = payload[:200]
    return status, parsed, headers
