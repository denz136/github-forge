"""REST v3 surface test: repos, contents, issues, pulls, pagination, errors."""

import base64
import sys

sys.path.insert(0, '/home/agentuser/makefun/github-forge')
from test_driver import jreq, mock_env, start_forge


def main():
    mock = mock_env(38130)
    proc, port = start_forge(38130)
    O = '/api/v3'
    ok = []
    try:
        st, repo, _ = jreq(port, 'POST', f'{O}/user/repos', body={'name': 'rest', 'private': True, 'description': 'd'})
        assert st == 201 and repo['private'] is True and repo['name'] == 'rest'
        ok.append('create repo (private flag round-trips)')

        st, got, _ = jreq(port, 'GET', f'{O}/repos/adaiversen/rest')
        assert st == 200 and got['default_branch'] == 'main'
        ok.append('GET repo round-trip')

        st, _, _ = jreq(port, 'GET', f'{O}/repos/adaiversen/missing')
        assert st == 404
        ok.append('GET missing repo 404')

        st, patched, _ = jreq(port, 'PATCH', f'{O}/repos/adaiversen/rest', body={'description': 'new-desc'})
        assert st == 200 and patched['description'] == 'new-desc'
        ok.append('PATCH description')

        # contents create
        content = base64.b64encode(b'hello file\n').decode()
        st, put, _ = jreq(port, 'PUT', f'{O}/repos/adaiversen/rest/contents/docs/readme.md',
                          body={'message': 'add readme', 'content': content, 'branch': 'main'})
        assert st in (200, 201), (st, put)
        assert put['content']['sha'] and put['commit']['sha']
        file_sha = put['content']['sha']
        ok.append('PUT contents creates file + commit')

        st, get, _ = jreq(port, 'GET', f'{O}/repos/adaiversen/rest/contents/docs/readme.md')
        assert st == 200
        assert base64.b64decode(get['content']) == b'hello file\n'
        assert get['sha'] == file_sha
        ok.append('GET contents returns base64 + sha')

        # stale sha -> 409
        st, _, _ = jreq(port, 'PUT', f'{O}/repos/adaiversen/rest/contents/docs/readme.md',
                        body={'message': 'x', 'content': base64.b64encode(b'v2').decode(), 'sha': 'deadbeef' * 5})
        assert st == 409, st
        ok.append('stale sha -> 409')

        # update with correct sha
        st, upd, _ = jreq(port, 'PUT', f'{O}/repos/adaiversen/rest/contents/docs/readme.md',
                          body={'message': 'v2', 'content': base64.b64encode(b'v2\n').decode(), 'sha': file_sha})
        assert st == 200, (st, upd)
        ok.append('update contents with correct sha')

        # directory listing
        st, listing, _ = jreq(port, 'GET', f'{O}/repos/adaiversen/rest/contents/docs')
        assert st == 200 and isinstance(listing, list) and listing[0]['name'] == 'readme.md'
        ok.append('directory listing')

        # delete file
        st, _, _ = jreq(port, 'DELETE', f'{O}/repos/adaiversen/rest/contents/docs/readme.md',
                        body={'message': 'rm', 'sha': upd['content']['sha']})
        assert st == 200, st
        st, _, _ = jreq(port, 'GET', f'{O}/repos/adaiversen/rest/contents/docs/readme.md')
        assert st == 404
        ok.append('DELETE contents + subsequent 404')

        # issues
        st, issue, _ = jreq(port, 'POST', f'{O}/repos/adaiversen/rest/issues',
                            body={'title': 'bug one', 'body': 'b', 'labels': ['P1']})
        assert st == 201 and issue['number'] == 1 and issue['state'] == 'open'
        num = issue['number']
        ok.append('create issue')

        st, got, _ = jreq(port, 'GET', f'{O}/repos/adaiversen/rest/issues/{num}')
        assert st == 200 and got['title'] == 'bug one'
        ok.append('GET issue')

        st, lst, h = jreq(port, 'GET', f'{O}/repos/adaiversen/rest/issues')
        assert st == 200 and len(lst) == 1
        ok.append('list issues')

        st, patched, _ = jreq(port, 'PATCH', f'{O}/repos/adaiversen/rest/issues/{num}', body={'state': 'closed'})
        assert st == 200 and patched['state'] == 'closed' and patched['closed_at']
        ok.append('close issue')

        st, comment, _ = jreq(port, 'POST', f'{O}/repos/adaiversen/rest/issues/{num}/comments', body={'body': 'note'})
        assert st == 201
        ok.append('issue comment')

        # pulls
        st, put, _ = jreq(port, 'PUT', f'{O}/repos/adaiversen/rest/contents/base.txt',
                          body={'message': 'base', 'content': base64.b64encode(b'base\n').decode()})
        assert st in (200, 201)
        sha = put['commit']['sha']
        st, ref, _ = jreq(port, 'POST', f'{O}/repos/adaiversen/rest/git/refs',
                          body={'ref': 'refs/heads/feature', 'sha': sha})
        assert st == 201, (st, ref)
        ok.append('create branch via /git/refs')

        st, brs, _ = jreq(port, 'GET', f'{O}/repos/adaiversen/rest/branches')
        assert {b['name'] for b in brs} >= {'main', 'feature'}
        ok.append('list branches')

        st, pr, _ = jreq(port, 'POST', f'{O}/repos/adaiversen/rest/pulls',
                         body={'title': 'pr one', 'head': 'feature', 'base': 'main', 'body': ''})
        assert st == 201 and pr['number'] >= 1 and pr['state'] == 'open'
        prnum = pr['number']
        ok.append('create pull')

        st, prget, _ = jreq(port, 'GET', f'{O}/repos/adaiversen/rest/pulls/{prnum}')
        assert st == 200 and prget['head']['ref'] == 'feature' and prget['base']['ref'] == 'main'
        ok.append('GET pull')

        st, files, _ = jreq(port, 'GET', f'{O}/repos/adaiversen/rest/pulls/{prnum}/files')
        assert st == 200 and isinstance(files, list)
        ok.append('GET pull files')

        st, merged, _ = jreq(port, 'PUT', f'{O}/repos/adaiversen/rest/pulls/{prnum}/merge',
                             body={'commit_title': 'merge it'})
        assert st == 200 and merged.get('merged') is True, (st, merged)
        ok.append('merge pull')

        st, prclosed, _ = jreq(port, 'GET', f'{O}/repos/adaiversen/rest/pulls/{prnum}')
        assert prclosed['state'] in ('closed',) and prclosed['merged'] is True
        ok.append('pull shows merged state')

        st, commits, _ = jreq(port, 'GET', f'{O}/repos/adaiversen/rest/commits?sha=feature')
        assert st == 200 and len(commits) >= 1
        ok.append('GET /commits?sha=')

        # foreign token behaves as 401/404 for another owner
        st, _, _ = jreq(port, 'GET', f'{O}/repos/someoneelse/rest')
        assert st == 404, st
        ok.append('foreign owner repo 404')

        # delete repo
        st, _, _ = jreq(port, 'DELETE', f'{O}/repos/adaiversen/rest')
        assert st == 204, st
        st, _, _ = jreq(port, 'GET', f'{O}/repos/adaiversen/rest')
        assert st == 404
        ok.append('DELETE repo + 404 after')

        print('REST OK:')
        for line in ok:
            print(' -', line)
    finally:
        proc.terminate()
        proc.wait(timeout=5)


if __name__ == '__main__':
    main()
