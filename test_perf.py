"""Performance workload test mirroring the suite's four timed workloads:
push x200, clone x50 (shallow), 2000 issue reads, 200 issue writes."""

import concurrent.futures
import os
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, '/home/agentuser/makefun/github-forge')
from test_driver import jreq, mock_env, start_forge

PORT = 38160
TOKEN = 'tok123'


def sh(cmd, cwd=None, env=None):
    return subprocess.run(cmd, shell=True, cwd=cwd, capture_output=True, text=True,
                          env={**os.environ, **(env or {})})


def main():
    mock = mock_env(PORT)
    proc, port = start_forge(PORT)
    auth = ('x-access-token:' + TOKEN)
    O = '/api/v3'
    jreq(port, 'POST', f'{O}/user/repos', body={'name': 'perf'}, token=TOKEN)

    # seed a repo with 200 files via pushes (push x200 = one commit per push)
    work = tempfile.mkdtemp(prefix='perf-push-')
    url = f'http://{auth}@127.0.0.1:{port}/adaiversen/perf.git'
    assert sh(f'git init -q -b main .', cwd=work).returncode == 0
    sh('git config user.email t@e && git config user.name t', cwd=work)
    t0 = time.time()
    for i in range(200):
        with open(os.path.join(work, f'f{i}.txt'), 'w') as fh:
            fh.write('content %d\n' % i + 'x' * 300)
        sh('git add . && git commit -qm c%d' % i, cwd=work)
        r = sh(f'git push -q {url} main', cwd=work)
        if r.returncode:
            print('push fail', r.stderr[-500:])
            break
    push_t = time.time() - t0
    print(f'push x200: {push_t:.2f}s')

    # clone x50 shallow from 2 workers in parallel batches of 10
    t0 = time.time()
    clones = tempfile.mkdtemp(prefix='perf-clone-')

    def one_clone(i):
        return sh(f'git clone -q --depth 1 {url} {clones}/c{i}').returncode

    with concurrent.futures.ThreadPoolExecutor(10) as ex:
        results = list(ex.map(one_clone, range(50)))
    clone_t = time.time() - t0
    print(f'clone x50 shallow: {clone_t:.2f}s, failures={results.count(1)}')

    # 2000 issue reads (threaded, keep-alive)
    jreq(port, 'POST', f'{O}/repos/adaiversen/perf/issues', body={'title': 't1', 'number': 1}, token=TOKEN)
    t0 = time.time()

    def one_read(i):
        st, body, _ = jreq(port, 'GET', f'{O}/repos/adaiversen/perf/issues/1', token=TOKEN)
        return st

    with concurrent.futures.ThreadPoolExecutor(16) as ex:
        read_codes = list(ex.map(one_read, range(2000)))
    read_t = time.time() - t0
    print(f'2000 issue reads: {read_t:.2f}s, non-200={sum(1 for c in read_codes if c != 200)}')

    # 200 issue writes
    t0 = time.time()

    def one_write(i):
        st, _, _ = jreq(port, 'POST', f'{O}/repos/adaiversen/perf/issues',
                        body={'title': f'issue {i}'}, token=TOKEN)
        return st

    with concurrent.futures.ThreadPoolExecutor(16) as ex:
        write_codes = list(ex.map(one_write, range(200)))
    write_t = time.time() - t0
    print(f'200 issue writes: {write_t:.2f}s, non-201={sum(1 for c in write_codes if c != 201)}')

    total = push_t + clone_t + read_t + write_t
    print(f'TOTAL: {total:.2f}s')
    proc.terminate()


if __name__ == '__main__':
    main()
