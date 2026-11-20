"""Protocol-level test of the forge's git smart HTTP using a real git binary
for the client side (the forge itself stays pure Python). Covers: clone empty,
clone with commits, push new commits, push tags, non-fast-forward rejection,
shallow clone, incremental fetch, receive-pack report-status."""

import os
import shutil
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, '/home/agentuser/makefun/github-forge')
from test_driver import jreq, mock_env, start_forge

GIT_ENV = dict(os.environ,
               GIT_AUTHOR_NAME='tester', GIT_AUTHOR_EMAIL='t@e.invalid',
               GIT_COMMITTER_NAME='tester', GIT_COMMITTER_EMAIL='t@e.invalid',
               GIT_TERMINAL_PROMPT='0', GIT_CONFIG_GLOBAL='/dev/null')


def run(cmd, cwd):
    result = subprocess.run(cmd, cwd=cwd, env=GIT_ENV, capture_output=True, text=True)
    if result.returncode != 0:
        raise AssertionError(f"{cmd} failed rc={result.returncode}\n{result.stdout}\n{result.stderr}")
    return result.stdout.strip()


def main():
    mock = mock_env(38085)
    proc, port = start_forge(38085)
    base = f'http://127.0.0.1:{port}'
    ok = []
    try:
        st, repo, _ = jreq(port, 'POST', '/api/v3/user/repos', body={'name': 'proto', 'private': False})
        assert st == 201, st
        url = f'{base}/adaiversen/proto.git'

        work = tempfile.mkdtemp()
        clone = os.path.join(work, 'c1')
        run(['git', 'clone', url, clone], work)
        run(['git', 'checkout', '-b', 'main'], clone)
        ok.append('clone empty repo')

        run(['git', 'config', 'user.email', 't@e.invalid'], clone)
        run(['git', 'config', 'user.name', 'tester'], clone)
        for i in range(3):
            with open(os.path.join(clone, f'f{i}.txt'), 'w') as fh:
                fh.write('content %d\n' % i + 'x' * (i * 500))
            run(['git', 'add', '.'], clone)
            run(['git', 'commit', '-m', f'commit {i}'], clone)
        run(['git', 'push', 'origin', 'main'], clone)
        ok.append('push 3 commits to main')

        run(['git', 'tag', 'v1.0'], clone)
        run(['git', 'push', 'origin', 'v1.0'], clone)
        ok.append('push tag')

        # second clone sees the commits
        clone2 = os.path.join(work, 'c2')
        run(['git', 'clone', url, clone2], work)
        head_log = run(['git', 'log', '--oneline'], clone2)
        assert len(head_log.splitlines()) == 3, head_log
        ok.append('second clone has all commits')

        # incremental push from clone1, fetch in clone2
        with open(os.path.join(clone, 'f0.txt'), 'a') as fh:
            fh.write('more\n')
        run(['git', 'commit', '-am', 'amend-ish', ], clone)
        run(['git', 'push', 'origin', 'main'], clone)
        run(['git', 'fetch', 'origin'], clone2)
        run(['git', 'merge', '--ff-only', 'origin/main'], clone2)
        ok.append('incremental push + fetch + ff merge')

        # shallow clone
        shallow = os.path.join(work, 'c3')
        run(['git', 'clone', '--depth', '1', url, shallow], work)
        assert run(['git', 'rev-parse', 'HEAD'], shallow) == run(['git', 'rev-parse', 'HEAD'], clone2)
        ok.append('shallow clone depth=1')

        # non-fast-forward rejection
        run(['git', 'reset', '--hard', 'HEAD~1'], clone)
        with open(os.path.join(clone, 'divergent.txt'), 'w') as fh:
            fh.write('diverge\n')
        run(['git', 'add', '.'], clone)
        run(['git', 'commit', '-m', 'divergent'], clone)
        result = subprocess.run(['git', 'push', 'origin', 'main'], cwd=clone, env=GIT_ENV,
                                capture_output=True, text=True)
        assert result.returncode != 0, 'non-ff push should fail'
        ok.append('non-fast-forward rejected')

        # the forge refuses every non-fast-forward update, force or not (RULES.txt)
        result = subprocess.run(['git', 'push', '--force', 'origin', 'main'], cwd=clone, env=GIT_ENV,
                                capture_output=True, text=True)
        assert result.returncode != 0, 'force push should also be refused (fast-forward only)'
        ok.append('forced push refused (fast-forward only forge)')

        # branch creation via push
        run(['git', 'push', 'origin', 'main:feature-x'], clone)
        branches = jreq(port, 'GET', '/api/v3/repos/adaiversen/proto/branches')[1]
        names = {b['name'] for b in branches}
        assert 'feature-x' in names, names
        ok.append('push creates branch, visible in /branches')

        # delete branch via push
        run(['git', 'push', 'origin', ':feature-x'], clone)
        branches = jreq(port, 'GET', '/api/v3/repos/adaiversen/proto/branches')[1]
        assert 'feature-x' not in {b['name'] for b in branches}
        ok.append('push deletes branch')

        print('ALL OK:')
        for line in ok:
            print(' -', line)
    finally:
        proc.terminate()
        proc.wait(timeout=5)


if __name__ == '__main__':
    main()
