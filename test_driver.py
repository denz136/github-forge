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


# ---------------------------------------------------------------- git smart HTTP client

def pkt_line(payload):
    if payload is None:
        return b'0000'
    if isinstance(payload, str):
        payload = payload.encode()
    return ('%04x' % (len(payload) + 4)).encode() + payload


def read_pkt_lines(data):
    out = []
    i = 0
    while i + 4 <= len(data):
        n = int(data[i:i + 4], 16)
        if n == 0:
            out.append(('flush', b''))
            i += 4
        elif n == 1:
            out.append(('delim', b''))
            i += 4
        else:
            out.append(('data', data[i + 4:i + n]))
            i += n
    return out


def zlib_decompress_stream(data, offset):
    import zlib
    d = zlib.decompressobj()
    out = d.decompress(data[offset:])
    return out, len(data[offset:]) - len(d.unused_data)


def git_http(port, owner, repo, service, body=None):
    headers = {'Authorization': f'Bearer {TOKEN}',
               'Content-Type': f'application/x-{service}-request',
               'Accept': f'application/x-{service}-result',
               'Git-Protocol': 'version=2'}
    return req(port, 'POST', f'/{owner}/{repo}.git/{service}', raw_body=body or b'', headers=headers)


def parse_pack_objects(pack):
    """Return list of (type_name, data) from a packfile (no deltas expected here)."""
    import zlib
    assert pack[:4] == b'PACK'
    count = int.from_bytes(pack[8:12], 'big')
    types = {1: 'commit', 2: 'tree', 3: 'blob', 4: 'tag'}
    objs = []
    i = 12
    for _ in range(count):
        b = pack[i]; i += 1
        t = (b >> 4) & 7
        while b & 0x80:
            b = pack[i]; i += 1
        if t == 6:  # ofs-delta
            b = pack[i]; i += 1
            while b & 0x80:
                b = pack[i]; i += 1
        elif t == 7:  # ref-delta: 20-byte base oid
            i += 20
        data, used = zlib_decompress_stream(pack, i)
        i += used
        objs.append((types.get(t, f'type{t}'), data))
    return objs


def build_commit(tree_oid, parents, message, ident, ts):
    import hashlib
    lines = ['tree ' + tree_oid]
    for p in parents:
        lines.append('parent ' + p)
    lines.append(f'author {ident} {ts} +0000')
    lines.append(f'committer {ident} {ts} +0000')
    body = '\n'.join(lines) + '\n\n' + message + '\n'
    store = b'commit ' + str(len(body)).encode() + b'\x00' + body.encode()
    return hashlib.sha1(store).hexdigest(), store


def build_tree(entries):
    import hashlib
    # entries: list of (mode, name, oid); mode '100644' or '40000'
    buf = b''
    for mode, name, oid in sorted(entries, key=lambda e: e[1]):
        buf += mode.encode() + b' ' + name.encode() + b'\x00' + bytes.fromhex(oid)
    header = f'tree {len(buf)}\x00'.encode()
    return hashlib.sha1(header + buf).hexdigest(), header + buf


def build_blob(data):
    import hashlib
    header = f'blob {len(data)}\x00'.encode()
    return hashlib.sha1(header + data).hexdigest(), header + data


def push_pack(port, owner, repo, objects, ref_updates):
    """objects: list of full object stores (header+body). ref_updates: [(old, new, ref)]."""
    import zlib, struct, hashlib
    # build pack
    entries = []
    for store in objects:
        nul = store.index(b'\x00')
        otype = store[:store.index(b' ')].decode()
        data = store[nul + 1:]
        entries.append((otype, data))
    pack = b'PACK' + b'\x00\x00\x00\x02' + len(entries).to_bytes(4, 'big')
    for otype, data in entries:
        tnum = {'commit': 1, 'tree': 2, 'blob': 3, 'tag': 4}[otype]
        b = (tnum << 4) | (len(data) & 15)
        n = len(data) >> 4
        head = b''
        while n:
            head += bytes([b | 0x80])
            b = n & 0x7f
            n >>= 7
        head += bytes([b])
        pack += head + zlib.compress(data)
    pack += hashlib.sha1(pack).digest()
    ZERO = '0' * 40
    cmds = b''.join(pkt_line(f'{old} {new} {ref}') for old, new, ref in ref_updates)
    body = pkt_line(f'{ZERO} {cmds.decode()}') if False else cmds + pkt_line('')
    # report-status + side-band-64k capabilities
    body = cmds
    first = True
    out = bytearray()
    for old, new, ref in ref_updates:
        line = f'{old} {new} {ref}'
        if first:
            line += '\x00report-status side-band-64k agent=driver/1.0'
            first = False
        out += pkt_line(line + '\n')
    out += pkt_line('')
    # packfile pkt
    payload = out + pack
    resp = git_http(port, owner, repo, 'git-receive-pack', body=bytes(payload))
    return resp


def unpack_sideband(data):
    pack = b''
    report = b''
    for kind, payload in read_pkt_lines(data):
        if kind != 'data':
            continue
        if payload[:1] == b'\x01':
            pack += payload[1:]
        elif payload[:1] == b'\x02':
            report += payload[1:]
        elif payload[:1] == b'\x03':
            raise AssertionError('sideband error: ' + payload[1:].decode(errors='replace'))
    return pack, report


def scenario_checks(port):
    """The driver's own conformance pass: REST, git, GraphQL, MCP."""
    import base64
    ok = []
    O = '/api/v3'
    VIEWER = 'adaiversen'

    # REST root + meta
    st, root, hdrs = jreq(port, 'GET', O + '/')
    assert st == 200 and hdrs.get('X-OAuth-Scopes'), (st, dict(hdrs))
    ok.append('api root + X-OAuth-Scopes')
    st, meta, _ = jreq(port, 'GET', O + '/meta')
    assert st == 200 and meta.get('installed_version')
    ok.append('meta installed_version')
    st, body, _ = jreq(port, 'GET', O + '/user/repos', token=None)
    assert st == 401
    ok.append('unauthenticated 401')

    st, repo, _ = jreq(port, 'POST', O + '/user/repos', body={'name': 'driver', 'description': 'd'})
    assert st == 201 and repo['full_name'] == VIEWER + '/driver', (st, repo)
    ok.append('create repo 201')
    st, _, _ = jreq(port, 'GET', O + '/repos/someoneelse/driver')
    assert st == 404
    ok.append('foreign owner 404')

    st, lbl, _ = jreq(port, 'POST', O + f'/repos/{VIEWER}/driver/labels', body={'name': 'bug'})
    assert st == 201
    st, issue, _ = jreq(port, 'POST', O + f'/repos/{VIEWER}/driver/issues',
                        body={'title': 't1', 'labels': ['bug']})
    assert st == 201 and issue['state'] == 'open'
    st, bad, _ = jreq(port, 'POST', O + f'/repos/{VIEWER}/driver/issues',
                      body={'title': 't2', 'labels': ['nope']})
    assert st == 422
    ok.append('issues + label rule 422')

    # GraphQL
    q = 'query { repository(owner: "%s", name: "driver") { id nameWithOwner issues(first: 10) { nodes { number } } } }' % VIEWER
    st, res, _ = jreq(port, 'POST', '/api/graphql', body={'query': q, 'variables': {}})
    assert st == 200 and res['data']['repository']['nameWithOwner'] == VIEWER + '/driver'
    gid = res['data']['repository']['id']
    st, res, _ = jreq(port, 'POST', '/api/graphql', body={'query': 'query($id: ID!){ node(id: $id) { id ... on Repository { name } } }', 'variables': {'id': gid}})
    assert st == 200 and res['data']['node']['name'] == 'driver'
    st, res, _ = jreq(port, 'POST', '/api/graphql', body={'query': 'query Issue_fields { Issue: __type(name: "Issue") { fields { name } } }', 'variables': {}})
    assert len(res['data']['Issue']['fields']) >= 5
    ok.append('graphql repository/node/introspection')

    # MCP
    st, res, _ = jreq(port, 'POST', '/mcp', body={'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'})
    tools = res['result']['tools']
    names = {t['name'].split('__')[-1].lower() for t in tools}
    for want in ('create_repo', 'get_repo', 'create_issue', 'list_issues', 'create_pull_request', 'merge_pull_request'):
        assert want in names, (want, names)
    st, res, _ = jreq(port, 'POST', '/mcp', body={'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call',
                                                  'params': {'name': 'create_issue', 'arguments': {'owner': VIEWER, 'repo': 'driver', 'title': 'via mcp'}}})
    assert res['result'].get('isError') is not True
    st, lst, _ = jreq(port, 'GET', O + f'/repos/{VIEWER}/driver/issues')
    assert any(i['title'] == 'via mcp' for i in lst)
    ok.append('mcp tools/list + tools/call shares store')

    # git: push a commit over receive-pack, then ls-refs over v2
    blob_oid, blob_store = build_blob(b'hello driver\n')
    tree_oid, tree_store = build_tree([('100644', 'file.txt', blob_oid)])
    commit_oid, commit_store = build_commit(tree_oid, [], 'first', 'T <t@e.invalid>', 1700000000)
    ZERO = '0' * 40
    resp = push_pack(port, VIEWER, 'driver',
                     [blob_store, tree_store, commit_store],
                     [(ZERO, commit_oid, 'refs/heads/main')])
    assert resp[0] == 200, resp[0]
    report = resp[1]
    try:
        _, report = unpack_sideband(report)
        if b'unpack' not in report:
            report = resp[1]
    except Exception:
        pass
    assert b'unpack ok' in report, report[:300]
    ok.append('git push accepted with report-status')

    # ls-refs v2
    body = pkt_line('command=ls-refs\n') + pkt_line('version=2\n') + pkt_line('agent=driver/1.0\n') + pkt_line('')
    body += pkt_line('symrefs\n') + pkt_line('peel\n') + pkt_line('')
    st, raw, hdrs = git_http(port, VIEWER, 'driver', 'git-upload-pack', body=body)
    assert st == 200, st
    lines = [p.decode(errors='replace').strip() for k, p in read_pkt_lines(raw) if k == 'data' and p.strip()]
    assert any(line.startswith(commit_oid) and 'refs/heads/main' in line for line in lines), lines
    assert any('HEAD' in line and 'refs/heads/main' in line for line in lines), lines
    ok.append('git v2 ls-refs advertises pushed tip')

    # unknown repo 404 on info/refs, server stays up
    st, _, _ = req(port, 'GET', '/%s/nope.git/info/refs?service=git-upload-pack' % VIEWER,
                   headers={'Git-Protocol': 'version=2'})
    assert st == 404
    st, _, _ = jreq(port, 'GET', O + '/')
    assert st == 200
    ok.append('git unknown repo 404, server survives')

    return ok


def main():
    mock_port = 18099
    srv = mock_env(mock_port)
    proc, port = start_forge(mock_port)
    with open(PORT_FILE, 'w') as fh:
        json.dump({'port': port}, fh)
    try:
        ok = scenario_checks(port)
    finally:
        state['done'] = True
        # forge polls /done and exits 0
        try:
            rc = proc.wait(timeout=60)
        except subprocess.TimeoutExpired:
            proc.kill()
            rc = proc.returncode
        try:
            srv.close()
        except OSError:
            pass
    for line in ok:
        print(' -', line)
    assert rc == 0, f'forge exited {rc}, expected 0 after done'
    print(f'DRIVER OK ({len(ok)} checks), forge exit code 0')


if __name__ == '__main__':
    main()
