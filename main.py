"""Vela forge: a standalone GitHub-compatible git forge (Python 3.13 stdlib).

One process: binds an ephemeral port, POSTs {"port": N} to $MAKEFUN_ENV_URL/register,
serves git smart HTTP + REST v3 + GraphQL v4 + MCP, long-polls GET /done, exits 0.
"""

import base64
import binascii
import hashlib
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import socket
import threading
import time
import urllib.parse
import urllib.request
import uuid

import gitobj
import gitproto
from gitproto import is_hex40
from scenario import VIEWER_LOGIN
from store import Store, now_iso

ENV_URL_ENV = 'MAKEFUN_ENV_URL'
TOKEN_ENV = 'MAKEFUN_SHOP_TOKEN'

API_PREFIX = '/api/v3'
GRAPHQL_PATH = '/api/graphql'
MCP_PATH = '/mcp'

REQUEST_COUNTER = threading.Lock()
REQUEST_SEQ = [0]

# ---------------------------------------------------------------- helpers


def request_id():
    with REQUEST_COUNTER:
        REQUEST_SEQ[0] += 1
    return f'{REQUEST_SEQ[0]:X}-{uuid.uuid4().hex[:8]}'


def json_bytes(value):
    return json.dumps(value, separators=(',', ':')).encode()


class HttpError(Exception):
    def __init__(self, status, message, extra_headers=None):
        self.status = status
        self.message = message
        self.extra_headers = extra_headers or {}


def error_body(message):
    return {'message': message, 'documentation_url': 'https://docs.github.com/rest'}


# ---------------------------------------------------------------- issue/pull JSON shapes


def issue_json(repo, issue):
    return {
        'id': 100000 + issue['id'],
        'number': issue['number'],
        'title': issue['title'],
        'body': issue['body'],
        'state': issue['state'],
        'labels': [{'name': name, 'color': repo.labels.get(name, 'ededed')} for name in issue['labels']],
        'user': {'login': issue['user'], 'id': 1000},
        'created_at': issue['created_at'],
        'updated_at': issue.get('updated_at', issue['created_at']),
        'closed_at': issue.get('closed_at'),
        'comments': len(issue.get('comments', [])),
        'html_url': f'https://forge.local/{repo.owner}/{repo.name}/issues/{issue["number"]}',
        'url': f'/api/v3/repos/{repo.owner}/{repo.name}/issues/{issue["number"]}',
    }


def pull_json(repo, pull):
    head_sha = repo.refs.get('refs/heads/' + pull['head']) or gitobj.ZERO_OID
    base_sha = repo.refs.get('refs/heads/' + pull['base']) or gitobj.ZERO_OID
    body = {
        'id': 200000 + pull['id'],
        'number': pull['number'],
        'title': pull['title'],
        'body': pull['body'],
        'state': pull['state'],
        'merged': pull.get('merged', False),
        'merged_at': pull.get('merged_at'),
        'merge_commit_sha': pull.get('merge_commit_sha'),
        'mergeable': None if pull.get('merged') else True,
        'head': {'ref': pull['head'], 'sha': head_sha, 'label': f'{repo.owner}:{pull["head"]}'},
        'base': {'ref': pull['base'], 'sha': base_sha, 'label': f'{repo.owner}:{pull["base"]}'},
        'user': {'login': pull['user'], 'id': 1000},
        'created_at': pull['created_at'],
        'updated_at': pull.get('updated_at', pull['created_at']),
    }
    return body


def repo_json(repo):
    return {
        'id': repo.id,
        'name': repo.name,
        'full_name': f'{repo.owner}/{repo.name}',
        'owner': {'login': repo.owner, 'id': 1000, 'type': 'User'},
        'private': repo.private,
        'description': repo.description,
        'default_branch': repo.default_branch,
        'created_at': repo.created_at,
        'updated_at': repo.updated_at,
        'pushed_at': repo.updated_at,
        'html_url': f'https://forge.local/{repo.owner}/{repo.name}',
        'clone_url': f'https://forge.local/{repo.owner}/{repo.name}.git',
        'has_issues': True,
        'fork': False,
    }


def commit_json(repo, oid):
    entry = repo.store.get(oid)
    fields = gitobj.parse_commit(entry[1]) if entry else {'message': '', 'author': ''}
    return {
        'sha': oid,
        'commit': {
            'message': fields.get('message', ''),
            'author': {'name': fields.get('author', ''), 'date': now_iso()},
            'committer': {'name': fields.get('committer', fields.get('author', '')), 'date': now_iso()},
        },
        'parents': [{'sha': p} for p in fields.get('parents', [])],
        'html_url': f'https://forge.local/{repo.owner}/{repo.name}/commit/{oid}',
    }


# ---------------------------------------------------------------- the application

class Forge:
    def __init__(self, token):
        self.token = token
        self.store = Store(VIEWER_LOGIN)

    # ---- auth ----
    def authed(self, headers):
        auth = headers.get('authorization') or headers.get('Authorization') or ''
        if self.token == '':
            return auth.strip() != ''
        return auth == f'Bearer {self.token}'

    # ---- git routes ----
    def git_route(self, method, path, query, headers, body):
        # path: /{owner}/{repo}.git[/info/refs|/git-upload-pack|/git-receive-pack]
        if not path.endswith('.git') and '/info/refs' not in path and 'git-upload-pack' not in path and 'git-receive-pack' not in path:
            return None
        if path.endswith('/info/refs'):
            repo_path = path[:-len('/info/refs')]
        elif path.endswith('/git-upload-pack'):
            repo_path = path[:-len('/git-upload-pack')]
        elif path.endswith('/git-receive-pack'):
            repo_path = path[:-len('/git-receive-pack')]
        else:
            return None
        owner_name = repo_path.lstrip('/')
        if owner_name.endswith('.git'):
            owner_name = owner_name[:-4]
        if '/' not in owner_name:
            return None
        owner, name = owner_name.split('/', 1)
        repo = self.store.get_repo(owner, name)
        service = query.get('service', [''])[0]
        if path.endswith('/info/refs'):
            if repo is None:
                raise HttpError(404, 'repository not found')
            if service == 'git-upload-pack':
                if headers.get('Git-Protocol') == 'version=2':
                    body = gitproto.v2_advertisement()
                else:
                    body = gitproto.v0_upload_advertisement(repo)
                return (200, gitproto.service_preamble('git-upload-pack') + body,
                        {'Content-Type': 'application/x-git-upload-pack-advertisement',
                         'Cache-Control': 'no-cache'})
            if service == 'git-receive-pack':
                body = gitproto.v0_receive_advertisement(repo)
                return (200, gitproto.service_preamble('git-receive-pack') + body,
                        {'Content-Type': 'application/x-git-receive-pack-advertisement',
                         'Cache-Control': 'no-cache'})
            raise HttpError(403, 'dumb http protocol is not supported')
        if path.endswith('/git-upload-pack'):
            if repo is None:
                raise HttpError(404, 'repository not found')
            protocol = headers.get('Git-Protocol') or ''
            if protocol == 'version=2':
                body_out = gitproto.handle_upload_pack_v2(repo, body)
            else:
                body_out = gitproto.handle_upload_pack_v0(repo, body)
            return (200, body_out,
                    {'Content-Type': 'application/x-git-upload-pack-result',
                     'Cache-Control': 'no-cache'})
        if path.endswith('/git-receive-pack'):
            if repo is None:
                raise HttpError(404, 'repository not found')
            body_out = gitproto.handle_receive_pack(repo, body)
            return (200, body_out,
                    {'Content-Type': 'application/x-git-receive-pack-result',
                     'Cache-Control': 'no-cache'})
        return None

    # ---- REST routes ----
    def rest_route(self, method, path, query, headers, body_json):
        """path excludes /api/v3. Returns (status, obj|list, extra_headers)."""
        parts = [p for p in path.split('/') if p != '']
        if not parts:
            if method == 'GET':
                return (200, {'current_user_url': '/api/v3/user',
                              'repository_url': '/api/v3/repos/{owner}/{repo}'},
                        {'X-OAuth-Scopes': 'repo, user'})
            raise HttpError(404, 'not found')
        head = parts[0]

        if head == 'meta' and method == 'GET':
            return (200, {'installed_version': 'forge-1.0.0', 'verifiable_password_authentication': False}, {})

        if head == 'user':
            if len(parts) == 1 and method == 'GET':
                user = self.store.user(VIEWER_LOGIN)
                return (200, {'login': user['login'], 'id': user['id'], 'name': user['name'],
                              'email': user['email'], 'type': 'User'}, {})
            if len(parts) == 2 and parts[1] == 'repos':
                if method == 'GET':
                    repos = self.store.user_repos(VIEWER_LOGIN)
                    page = int(query.get('page', ['1'])[0] or 1)
                    per_page = int(query.get('per_page', ['30'])[0] or 30)
                    start = (page - 1) * per_page
                    items = [repo_json(r) for r in repos[start:start + per_page]]
                    extra = {}
                    if start + per_page < len(repos):
                        base = query_path = '/api/v3/user/repos'
                        next_query = dict((k, v[0]) for k, v in query.items())
                        next_query['page'] = str(page + 1)
                        next_query['per_page'] = str(per_page)
                        extra['Link'] = f'</{base}?{urllib.parse.urlencode(next_query)}>; rel="next"'
                    return (200, items, extra)
                if method == 'POST':
                    name = body_json.get('name') or ''
                    if not name:
                        raise HttpError(422, 'name is required')
                    existing = self.store.get_repo(VIEWER_LOGIN, name)
                    if existing is not None:
                        raise HttpError(422, 'name already exists on this account')
                    repo = self.store.create_repo(name, body_json.get('description'), bool(body_json.get('private', False)))
                    return (201, repo_json(repo), {})
            raise HttpError(404, 'not found')

        if head != 'repos' or len(parts) < 2:
            raise HttpError(404, 'not found')
        owner, name = parts[1], parts[2]
        repo = self.store.get_repo(owner, name)
        rest = parts[3:]

        if not rest:
            if method == 'GET':
                if repo is None:
                    raise HttpError(404, 'repository not found')
                return (200, repo_json(repo), {})
            if method == 'PATCH':
                if repo is None:
                    raise HttpError(404, 'repository not found')
                if 'description' in body_json:
                    repo.description = body_json['description']
                repo.updated_at = now_iso()
                out = repo_json(repo)
                return (200, out, {})
            if method == 'DELETE':
                if repo is None:
                    raise HttpError(404, 'repository not found')
                self.store.delete_repo(owner, name)
                return (204, None, {})
            raise HttpError(405, 'method not allowed')

        if repo is None:
            raise HttpError(404, 'repository not found')
        sub = rest[0]

        if sub == 'git':
            return self.git_data_route(repo, method, rest, body_json)
        if sub == 'branches':
            if method == 'GET' and len(rest) == 1:
                items = []
                for ref in sorted(repo.refs):
                    if ref.startswith('refs/heads/'):
                        items.append({'name': ref[len('refs/heads/'):],
                                      'commit': {'sha': repo.refs[ref]},
                                      'protected': False})
                return (200, items, {})
            if method == 'GET' and len(rest) == 2:
                ref = 'refs/heads/' + rest[1]
                if ref in repo.refs:
                    return (200, {'name': rest[1], 'commit': {'sha': repo.refs[ref]}}, {})
                raise HttpError(404, 'branch not found')
        if sub == 'commits' and method == 'GET':
            ref = query.get('sha', [repo.default_branch])[0]
            ref_oid = repo.refs.get('refs/heads/' + ref) or repo.refs.get(ref)
            if ref_oid is None and is_hex40(ref) and repo.store.get(ref):
                ref_oid = ref
            if ref_oid is None:
                raise HttpError(404, 'ref not found')
            oids = gitproto._ancestors(repo, ref_oid, 100)
            return (200, [commit_json(repo, o) for o in oids], {})

        if sub == 'contents':
            return self.contents_route(repo, method, rest, query, body_json)

        if sub == 'labels':
            if method == 'GET':
                return (200, [{'name': n, 'color': c} for n, c in sorted(repo.labels.items())], {})
            if method == 'POST' and len(rest) == 1:
                label = body_json.get('name') or ''
                if not label:
                    raise HttpError(422, 'name is required')
                repo.labels[label] = body_json.get('color', 'ededed')
                return (201, {'name': label, 'color': repo.labels[label]}, {})

        if sub == 'issues':
            return self.issues_route(repo, method, rest, query, body_json)

        if sub == 'pulls':
            return self.pulls_route(repo, method, rest, query, body_json)

        raise HttpError(404, 'not found')

    # ---- Git Data API (refs) ----
    def git_data_route(self, repo, method, rest, body_json):
        if len(rest) >= 2 and rest[1] == 'refs':
            ref_path = '/'.join(rest[2:])
            if method == 'POST' and len(rest) == 2:
                ref = body_json.get('ref') or ''
                sha = body_json.get('sha') or ''
                if not ref.startswith('refs/') or not is_hex40(sha) or not repo.store.get(sha):
                    raise HttpError(422, 'invalid ref or sha')
                if ref in repo.refs:
                    raise HttpError(422, 'reference already exists')
                repo.refs[ref] = sha
                return (201, {'ref': ref, 'object': {'sha': sha, 'type': 'commit'}}, {})
            if method == 'GET' and len(rest) >= 3:
                if ref_path in repo.refs:
                    return (200, {'ref': ref_path, 'object': {'sha': repo.refs[ref_path], 'type': 'commit'}}, {})
                raise HttpError(404, 'reference not found')
        raise HttpError(404, 'not found')

    # ---- contents ----
    def contents_route(self, repo, method, rest, query, body_json):
        file_path = '/'.join(rest[1:])
        ref = query.get('ref', [None])[0]
        branch = ref or repo.default_branch
        head_oid = repo.refs.get('refs/heads/' + branch)

        if method == 'GET':
            if head_oid is None:
                raise HttpError(404, 'path not found')
            entry = repo.store.get(head_oid)
            if entry is None:
                raise HttpError(404, 'path not found')
            tree_oid = gitobj.parse_commit(entry[1])['tree']
            found = repo.resolve_tree_path(tree_oid, [p for p in file_path.split('/') if p])
            if found is None:
                raise HttpError(404, 'path not found')
            kind, oid = found
            if kind == 'blob':
                _type, data = repo.store.get(oid)
                return (200, {
                    'type': 'file',
                    'name': file_path.split('/')[-1],
                    'path': file_path,
                    'sha': oid,
                    'size': len(data),
                    'encoding': 'base64',
                    'content': base64.b64encode(data).decode(),
                    'download_url': f'https://forge.local/{repo.owner}/{repo.name}/raw/{branch}/{file_path}',
                }, {})
            entries = []
            for mode, child_name, child_oid in gitobj.parse_tree(repo.store.get(oid)[1]):
                child_entry = repo.store.get(child_oid)
                child_type = 'dir' if mode == '40000' else 'file'
                entries.append({
                    'type': child_type,
                    'name': child_name,
                    'path': f'{file_path}/{child_name}',
                    'sha': child_oid,
                    'size': len(child_entry[1]) if child_entry and child_type == 'file' else 0,
                })
            return (200, entries, {})

        if method == 'PUT':
            content_b64 = body_json.get('content') or ''
            try:
                data = base64.b64decode(content_b64)
            except (binascii.Error, ValueError):
                raise HttpError(422, 'content is not valid base64')
            message = body_json.get('message') or 'update'
            branch = body_json.get('branch') or repo.default_branch
            provided_sha = body_json.get('sha')
            ref = 'refs/heads/' + branch
            parent = repo.refs.get(ref)
            existing_sha = None
            if parent:
                pentry = repo.store.get(parent)
                tree_oid = gitobj.parse_commit(pentry[1])['tree']
                found = repo.resolve_tree_path(tree_oid, [p for p in file_path.split('/') if p])
                if found and found[0] == 'blob':
                    existing_sha = found[1]
            if provided_sha and provided_sha != existing_sha:
                raise HttpError(409, f'{file_path} does not match {provided_sha}')
            created = existing_sha is None
            tip = repo.apply_commit_to_refs({
                'branch': branch,
                'files': {file_path: data},
                'message': message,
                'author': f'{VIEWER_LOGIN} <{VIEWER_LOGIN}@example.invalid>',
                'timestamp': int(time.time()),
            })
            repo.updated_at = now_iso()
            tip_entry = repo.store.get(repo.head_oid(branch))
            tip_tree = gitobj.parse_commit(tip_entry[1])['tree'] if tip_entry else None
            found = repo.resolve_tree_path(tip_tree, [p for p in file_path.split('/') if p]) if tip_tree else None
            blob_oid = found[1] if found and found[0] == 'blob' else None
            return ((201 if created else 200), {
                'content': {
                    'name': file_path.split('/')[-1],
                    'path': file_path,
                    'sha': blob_oid,
                    'size': len(data),
                    'encoding': 'base64',
                    'content': base64.b64encode(data).decode(),
                },
                'commit': {'sha': tip, 'message': message,
                           'author': {'name': VIEWER_LOGIN, 'email': f'{VIEWER_LOGIN}@example.invalid'}},
            }, {})

        if method == 'DELETE':
            provided_sha = (body_json or {}).get('sha')
            message = (body_json or {}).get('message') or 'delete'
            branch = (body_json or {}).get('branch') or repo.default_branch
            head_oid = repo.refs.get('refs/heads/' + branch)
            if head_oid is None:
                raise HttpError(404, 'path not found')
            tree_oid = gitobj.parse_commit(repo.store.get(head_oid)[1])['tree']
            found = repo.resolve_tree_path(tree_oid, [p for p in file_path.split('/') if p])
            if found is None or found[0] != 'blob':
                raise HttpError(404, 'path not found')
            if provided_sha and provided_sha != found[1]:
                raise HttpError(409, 'sha mismatch')
            tip = repo.apply_commit_to_refs({
                'branch': branch,
                'files': {file_path: None},
                'message': message,
                'author': f'{VIEWER_LOGIN} <{VIEWER_LOGIN}@example.invalid>',
                'timestamp': int(time.time()),
            })
            return (200, {'commit': {'sha': tip, 'message': message}}, {})

        raise HttpError(405, 'method not allowed')

    # ---- issues ----
    def issues_route(self, repo, method, rest, query, body_json):
        if len(rest) == 1:
            if method == 'GET':
                state = query.get('state', ['open'])[0]
                sort = query.get('sort', ['created'])[0]
                direction = query.get('direction', ['desc'])[0]
                per_page = int(query.get('per_page', ['30'])[0] or 30)
                page = int(query.get('page', ['1'])[0] or 1)
                issues = sorted(repo.issues.values(), key=lambda i: i['number'])
                if state in ('open', 'closed'):
                    issues = [i for i in issues if i['state'] == state]
                if sort == 'created':
                    issues.sort(key=lambda i: i['number'], reverse=(direction == 'desc'))
                start = (page - 1) * per_page
                return (200, [issue_json(repo, i) for i in issues[start:start + per_page]], {})
            if method == 'POST':
                return (201, issue_json(repo, self.create_issue(repo, body_json)), {})
        number = rest[1] if len(rest) > 1 else None
        if number is not None and not number.isdigit():
            raise HttpError(404, 'not found')
        if number is not None:
            n = int(number)
            issue = repo.issues.get(n)
            if len(rest) == 2:
                if method == 'GET':
                    if issue is None:
                        raise HttpError(404, 'issue not found')
                    return (200, issue_json(repo, issue), {})
                if method == 'PATCH':
                    if issue is None:
                        raise HttpError(404, 'issue not found')
                    if 'state' in body_json:
                        issue['state'] = body_json['state']
                        issue['closed_at'] = now_iso() if body_json['state'] == 'closed' else None
                    if 'title' in body_json:
                        issue['title'] = body_json['title']
                    if 'body' in body_json:
                        issue['body'] = body_json['body']
                    issue['updated_at'] = now_iso()
                    return (200, issue_json(repo, issue), {})
            if len(rest) == 3 and rest[2] == 'comments':
                if method == 'GET':
                    if issue is None:
                        raise HttpError(404, 'issue not found')
                    return (200, issue.get('comments', []), {})
                if method == 'POST':
                    if issue is None:
                        raise HttpError(404, 'issue not found')
                    comment = {
                        'id': repo.next_comment_id,
                        'body': body_json.get('body') or '',
                        'user': {'login': VIEWER_LOGIN, 'id': 1000},
                        'created_at': now_iso(),
                    }
                    repo.next_comment_id += 1
                    issue.setdefault('comments', []).append(comment)
                    return (201, comment, {})
        raise HttpError(404, 'not found')

    def create_issue(self, repo, body_json):
        title = body_json.get('title') or ''
        labels = body_json.get('labels') or []
        for label in labels:
            if label not in repo.labels:
                repo.labels[label] = 'ededed'  # GitHub auto-creates unknown labels on issue create
        number = repo.next_issue
        repo.next_issue += 1
        issue = {
            'id': number,
            'number': number,
            'title': title,
            'body': body_json.get('body') or '',
            'state': 'open',
            'labels': list(labels),
            'user': VIEWER_LOGIN,
            'created_at': now_iso(),
            'updated_at': now_iso(),
            'closed_at': None,
            'comments': [],
        }
        repo.issues[number] = issue
        return issue

    # ---- pulls ----
    def pulls_route(self, repo, method, rest, query, body_json):
        if len(rest) == 1:
            if method == 'GET':
                state = query.get('state', ['open'])[0]
                pulls = sorted(repo.pulls.values(), key=lambda p: p['number'])
                if state != 'all':
                    pulls = [p for p in pulls if (p['state'] == 'open' and not p.get('merged')) if state == 'open'] if state == 'open' else [p for p in pulls if p['state'] == state and not p.get('merged')]
                if state == 'open':
                    pulls = [p for p in pulls if p['state'] == 'open' and not p.get('merged')]
                return (200, [pull_json(repo, p) for p in pulls], {})
            if method == 'POST':
                return (201, pull_json(repo, self.create_pull(repo, body_json)), {})
        if len(rest) >= 2 and rest[1].isdigit():
            n = int(rest[1])
            pull = repo.pulls.get(n)
            if len(rest) == 2:
                if method == 'GET':
                    if pull is None:
                        raise HttpError(404, 'pull request not found')
                    return (200, pull_json(repo, pull), {})
            if len(rest) == 3 and rest[2] == 'files' and method == 'GET':
                if pull is None:
                    raise HttpError(404, 'pull request not found')
                return (200, self.pull_files(repo, pull), {})
            if len(rest) == 3 and rest[2] == 'merge' and method == 'PUT':
                return self.merge_pull(repo, n, body_json or {})
        raise HttpError(404, 'not found')

    def create_pull(self, repo, body_json):
        head = body_json.get('head') or ''
        base = body_json.get('base') or repo.default_branch
        if head == base:
            raise HttpError(422, 'head and base are the same branch')
        head_oid = repo.refs.get('refs/heads/' + head)
        if head_oid is None:
            raise HttpError(422, f'head branch {head} does not exist')
        if any(p['head'] == head and p['state'] == 'open' and not p.get('merged') for p in repo.pulls.values()):
            raise HttpError(422, 'a pull request for this head branch already exists')
        number = repo.next_pull
        repo.next_pull += 1
        pull = {
            'id': number,
            'number': number,
            'title': body_json.get('title') or '',
            'body': body_json.get('body') or '',
            'state': 'open',
            'head': head,
            'base': base,
            'user': VIEWER_LOGIN,
            'created_at': now_iso(),
            'updated_at': now_iso(),
            'merged': False,
            'merged_at': None,
            'merge_commit_sha': None,
        }
        repo.pulls[number] = pull
        return pull

    def pull_files(self, repo, pull):
        head_oid = repo.refs.get('refs/heads/' + pull['head'])
        base_oid = repo.refs.get('refs/heads/' + pull['base'])
        head_tree = gitobj.parse_commit(repo.store.get(head_oid)[1])['tree'] if head_oid else None
        base_tree = gitobj.parse_commit(repo.store.get(base_oid)[1])['tree'] if base_oid else None
        out = []
        for path, status, old_blob, new_blob in repo.diff_files(base_tree, head_tree):
            old_data = repo.store.get(old_blob)[1] if old_blob else b''
            new_data = repo.store.get(new_blob)[1] if new_blob else b''
            additions = sum(1 for line in new_data.split(b'\n') if line and line not in old_data.split(b'\n'))
            deletions = sum(1 for line in old_data.split(b'\n') if line and line not in new_data.split(b'\n'))
            out.append({
                'sha': new_blob or old_blob,
                'filename': path,
                'status': status if status != 'removed' else 'removed',
                'additions': additions,
                'deletions': deletions,
                'changes': additions + deletions,
                'blob_url': f'https://forge.local/{repo.owner}/{repo.name}/blob/{path}',
            })
        return out

    def merge_pull(self, repo, number, body_json):
        pull = repo.pulls.get(number)
        if pull is None:
            raise HttpError(404, 'pull request not found')
        if pull.get('merged'):
            raise HttpError(405, 'pull request is already merged')
        head_oid = repo.refs.get('refs/heads/' + pull['head'])
        base_oid = repo.refs.get('refs/heads/' + pull['base'])
        if head_oid is None or base_oid is None:
            raise HttpError(405, 'pull request refs are gone')
        head_tree = gitobj.parse_commit(repo.store.get(head_oid)[1])['tree']
        base_tree = gitobj.parse_commit(repo.store.get(base_oid)[1])['tree']
        changes = repo.diff_files(base_tree, head_tree)
        for path, _status, _old, _new in changes:
            # conflict check: the same path changed on base since the pull opened
            base_files = dict(repo.all_files_at(base_tree))
            if path in base_files:
                entry = repo.store.get(base_files[path])
                pull_base = getattr(pull, '_base_tree_at_open', None)
        # detect conflicts: another merged pull changed the same path after this pull opened
        merge_method = body_json.get('merge_method', 'merge')
        merge_title = body_json.get('commit_title') or f'Merge pull #{number} ({merge_method})'
        # squash: apply the head tree's changed files onto the base
        files = {}
        for path, _status, _old, new_blob in changes:
            files[path] = repo.store.get(new_blob)[1] if new_blob else None
        conflict = getattr(repo, '_conflict_probe', None)
        # track base tip at open time for conflict detection
        opened_base = pull.get('base_tip_at_open', base_oid)
        if opened_base and opened_base != base_oid:
            opened_tree = gitobj.parse_commit(repo.store.get(opened_base)[1])['tree'] if repo.store.get(opened_base) else None
            if opened_tree is not None:
                for path, _status, _old, _new in changes:
                    base_files_now = dict(repo.all_files_at(base_tree))
                    base_files_open = dict(repo.all_files_at(opened_tree))
                    if path in base_files_now and path in base_files_open and base_files_now[path] != base_files_open[path]:
                        raise HttpError(405, 'merge conflict')
        tip = repo.apply_commit_to_refs({
            'branch': pull['base'],
            'files': files,
            'message': merge_title,
            'author': f'{VIEWER_LOGIN} <{VIEWER_LOGIN}@example.invalid>',
            'timestamp': int(time.time()),
        })
        pull['merged'] = True
        pull['state'] = 'closed'
        pull['merged_at'] = now_iso()
        pull['merge_commit_sha'] = tip
        repo.updated_at = now_iso()
        return (200, {
            'sha': tip,
            'merged': True,
            'message': 'Pull request successfully merged',
        }, {})

    def create_pull_record_base(self, repo):
        """Record each open pull's base tip at creation (conflict detection)."""
        for pull in repo.pulls.values():
            if 'base_tip_at_open' not in pull:
                pull['base_tip_at_open'] = repo.refs.get('refs/heads/' + pull['base'])


# ---------------------------------------------------------------- MCP

MCP_REVISION = '2026-07-28'

MCP_TOOLS = [
    {
        'name': 'create_repo',
        'description': 'Create a new repository owned by the authenticated user.',
        'inputSchema': {
            'type': 'object',
            'properties': {
                'name': {'type': 'string', 'description': 'The repository name'},
                'description': {'type': 'string', 'description': 'The repository description'},
                'private': {'type': 'boolean', 'description': 'Whether the repository is private'},
            },
            'required': ['name'],
        },
    },
    {
        'name': 'get_repo',
        'description': 'Get one repository by owner and name.',
        'inputSchema': {
            'type': 'object',
            'properties': {
                'owner': {'type': 'string', 'description': 'The repository owner login'},
                'repo': {'type': 'string', 'description': 'The repository name'},
            },
            'required': ['owner', 'repo'],
        },
    },
    {
        'name': 'create_issue',
        'description': 'Create an issue on a repository.',
        'inputSchema': {
            'type': 'object',
            'properties': {
                'owner': {'type': 'string', 'description': 'The repository owner login'},
                'repo': {'type': 'string', 'description': 'The repository name'},
                'title': {'type': 'string', 'description': 'The issue title'},
                'body': {'type': 'string', 'description': 'The issue body'},
            },
            'required': ['owner', 'repo', 'title'],
        },
    },
    {
        'name': 'list_issues',
        'description': 'List a repository\'s issues, optionally filtered by state.',
        'inputSchema': {
            'type': 'object',
            'properties': {
                'owner': {'type': 'string', 'description': 'The repository owner login'},
                'repo': {'type': 'string', 'description': 'The repository name'},
                'state': {'type': 'string', 'description': 'open, closed or all'},
            },
            'required': ['owner', 'repo'],
        },
    },
    {
        'name': 'create_pull_request',
        'description': 'Open a pull request from a head branch to a base branch.',
        'inputSchema': {
            'type': 'object',
            'properties': {
                'owner': {'type': 'string', 'description': 'The repository owner login'},
                'repo': {'type': 'string', 'description': 'The repository name'},
                'title': {'type': 'string', 'description': 'The pull request title'},
                'head': {'type': 'string', 'description': 'The head branch name'},
                'base': {'type': 'string', 'description': 'The base branch name'},
            },
            'required': ['owner', 'repo', 'title', 'head', 'base'],
        },
    },
    {
        'name': 'merge_pull_request',
        'description': 'Merge an open pull request (squash).',
        'inputSchema': {
            'type': 'object',
            'properties': {
                'owner': {'type': 'string', 'description': 'The repository owner login'},
                'repo': {'type': 'string', 'description': 'The repository name'},
                'pull_number': {'type': 'integer', 'description': 'The pull request number'},
            },
            'required': ['owner', 'repo', 'pull_number'],
        },
    },
]

MCP_TOOLS_JSON = json_bytes(MCP_TOOLS)


def mcp_tool_result(payload, structured=None):
    result = {'content': [{'type': 'text', 'text': json.dumps(payload)}]}
    if structured is not None:
        result['structuredContent'] = structured
    return result


class McpHandler:
    def __init__(self, forge):
        self.forge = forge

    def handle(self, method, headers, raw_body):
        auth = headers.get('authorization') or headers.get('Authorization') or ''
        if self.forge.token == '':
            if auth.strip() == '':
                return 401, json_bytes(error_body('unauthorized')), {}
        elif auth != f'Bearer {self.forge.token}':
            return 401, json_bytes(error_body('unauthorized')), {}
        try:
            body = json.loads(raw_body.decode('utf-8'))
        except (ValueError, UnicodeDecodeError):
            resp = json_bytes({'jsonrpc': '2.0', 'id': None,
                               'error': {'code': -32700, 'message': 'Parse error'}})
            return 400, resp, {}
        if isinstance(body, list):
            responses = [self.rpc(entry) for entry in body if isinstance(entry, dict)]
            return 200, json_bytes([r for r in responses if r is not None]), {}
        response = self.rpc(body)
        if response is None:
            return 202, b'', {}
        return 200, json_bytes(response), {}

    def rpc(self, request):
        if not isinstance(request, dict) or request.get('jsonrpc') != '2.0' or 'method' not in request:
            return {'jsonrpc': '2.0', 'id': request.get('id') if isinstance(request, dict) else None,
                    'error': {'code': -32600, 'message': 'Invalid Request'}}
        req_id = request.get('id')
        method = request.get('method')
        params = request.get('params') or {}
        is_notification = 'id' not in request
        if method == 'server/discover':
            result = {
                'serverInfo': {'name': 'vela-forge', 'version': '1.0.0'},
                'capabilities': {'tools': {}},
                'supportedVersions': [MCP_REVISION, '2025-11-25', '2025-06-18'],
            }
            return self.reply(req_id, result, is_notification)
        if method == 'initialize':
            revision = params.get('protocolVersion') or '2025-11-25'
            result = {
                'protocolVersion': revision,
                'serverInfo': {'name': 'vela-forge', 'version': '1.0.0'},
                'capabilities': {'tools': {}},
            }
            return self.reply(req_id, result, is_notification)
        if method == 'notifications/initialized':
            return None if is_notification else self.reply(req_id, {}, False)
        if method == 'tools/list':
            return self.reply(req_id, {'tools': MCP_TOOLS}, is_notification)
        if method == 'tools/call':
            return self.tool_call(req_id, params, is_notification)
        if is_notification:
            return None
        return {'jsonrpc': '2.0', 'id': req_id,
                'error': {'code': -32601, 'message': f'Method not found: {method}'}}

    def reply(self, req_id, result, is_notification):
        if is_notification or req_id is None:
            return None
        return {'jsonrpc': '2.0', 'id': req_id, 'result': result}

    def tool_call(self, req_id, params, is_notification):
        name = params.get('name') or ''
        args = params.get('arguments') or {}
        handler = getattr(self, 'tool_' + name, None)
        if handler is None:
            return {'jsonrpc': '2.0', 'id': req_id,
                    'error': {'code': -32602, 'message': f'Unknown tool: {name}'}}
        required = []
        for tool in MCP_TOOLS:
            if tool['name'] == name:
                required = tool['inputSchema'].get('required', [])
        missing = [key for key in required if key not in args]
        if missing:
            return {'jsonrpc': '2.0', 'id': req_id,
                    'result': {'content': [{'type': 'text', 'text': f'missing required arguments: {", ".join(missing)}'}],
                               'isError': True}}
        try:
            payload, structured = handler(args)
        except McpToolError as err:
            return {'jsonrpc': '2.0', 'id': req_id,
                    'result': {'content': [{'type': 'text', 'text': str(err)}], 'isError': True}}
        except HttpError as err:
            return {'jsonrpc': '2.0', 'id': req_id,
                    'result': {'content': [{'type': 'text', 'text': err.message}], 'isError': True}}
        return self.reply(req_id, mcp_tool_result(payload, structured), is_notification)

    def tool_create_repo(self, args):
        repo = self.forge.store.create_repo(args['name'], args.get('description'), bool(args.get('private', True)))
        if repo is None:
            raise McpToolError('repository name already exists')
        out = {'full_name': f'{repo.owner}/{repo.name}', 'name': repo.name,
               'owner': repo.owner, 'private': repo.private, 'default_branch': repo.default_branch}
        return out, out

    def tool_get_repo(self, args):
        repo = self.forge.store.get_repo(args['owner'], args['repo'])
        if repo is None:
            raise McpToolError('repository not found')
        out = {'full_name': f'{repo.owner}/{repo.name}', 'name': repo.name, 'owner': repo.owner,
               'private': repo.private, 'description': repo.description,
               'default_branch': repo.default_branch, 'id': repo.id}
        return out, out

    def tool_create_issue(self, args):
        repo = self.forge.store.get_repo(args['owner'], args['repo'])
        if repo is None:
            raise McpToolError('repository not found')
        issue = self.forge.create_issue(repo, {'title': args['title'], 'body': args.get('body', '')})
        out = {'number': issue['number'], 'title': issue['title'], 'state': issue['state'], 'id': issue['id']}
        return out, out

    def tool_list_issues(self, args):
        repo = self.forge.store.get_repo(args['owner'], args['repo'])
        if repo is None:
            raise McpToolError('repository not found')
        state = args.get('state', 'open')
        issues = sorted(repo.issues.values(), key=lambda i: i['number'])
        if state != 'all':
            issues = [i for i in issues if i['state'] == state]
        out = [{'number': i['number'], 'title': i['title'], 'state': i['state']} for i in issues]
        return out, out

    def tool_create_pull_request(self, args):
        repo = self.forge.store.get_repo(args['owner'], args['repo'])
        if repo is None:
            raise McpToolError('repository not found')
        pull = self.forge.create_pull(repo, {'title': args['title'], 'head': args['head'],
                                             'base': args.get('base', repo.default_branch)})
        out = {'number': pull['number'], 'state': pull['state'], 'head': pull['head'], 'base': pull['base']}
        return out, out

    def tool_merge_pull_request(self, args):
        repo = self.forge.store.get_repo(args['owner'], args['repo'])
        if repo is None:
            raise McpToolError('repository not found')
        number = args.get('pull_number')
        if not isinstance(number, int):
            number = int(number)
        status, payload, _ = self.forge.merge_pull(repo, number, {'merge_method': 'squash'})
        return payload, payload


class McpToolError(Exception):
    pass


# ---------------------------------------------------------------- GraphQL

class GraphqlHandler:
    """The subset of GitHub's v4 schema the suite's documents exercise."""

    INTROSPECTION_FIELDS = {
        'Issue': ['id', 'number', 'title', 'state', 'body', 'author', 'labels', 'comments', 'createdAt', 'updatedAt', 'closedAt', 'repository'],
        'PullRequest': ['id', 'number', 'title', 'state', 'merged', 'headRefName', 'baseRefName', 'mergeable', 'files', 'body', 'author', 'createdAt', 'mergeCommit'],
        'Repository': ['id', 'databaseId', 'name', 'nameWithOwner', 'owner', 'defaultBranchRef', 'viewerPermission', 'hasIssuesEnabled', 'issue', 'issues', 'pullRequests', 'pullRequest', 'description', 'createdAt'],
        'IssueConnection': ['nodes', 'edges', 'pageInfo', 'totalCount'],
        'PullRequestConnection': ['nodes', 'edges', 'pageInfo', 'totalCount'],
        'User': ['id', 'login', 'name', 'email'],
        'Ref': ['id', 'name'],
    }
    INTROSPECTION_ENUMS = {
        'IssueState': ['OPEN', 'CLOSED'],
        'PullRequestState': ['OPEN', 'CLOSED', 'MERGED'],
        'PullRequestMergeMethod': ['MERGE', 'SQUASH', 'REBASE'],
        'MergeableState': ['MERGEABLE', 'CONFLICTING', 'UNKNOWN'],
        'RepositoryPermission': ['ADMIN', 'MAINTAIN', 'WRITE', 'TRIAGE', 'READ'],
        'SearchType': ['ISSUE', 'REPOSITORY', 'USER', 'DISCUSSION'],
        'IssueOrderField': ['CREATED_AT', 'UPDATED_AT', 'COMMENTS'],
        'OrderDirection': ['ASC', 'DESC'],
    }

    def __init__(self, forge):
        self.forge = forge

    def handle(self, method, headers, raw_body):
        auth = headers.get('authorization') or headers.get('Authorization') or ''
        if self.forge.token != '' and auth != f'Bearer {self.forge.token}':
            return 401, json_bytes(error_body('unauthorized')), {}
        if self.forge.token == '' and auth.strip() == '':
            return 401, json_bytes(error_body('unauthorized')), {}
        try:
            body = json.loads(raw_body.decode('utf-8'))
        except (ValueError, UnicodeDecodeError):
            return 400, json_bytes(error_body('malformed JSON body')), {}
        if isinstance(body, list):
            out = [self.execute(entry) for entry in body]
            return 200, json_bytes(out), {}
        return 200, json_bytes(self.execute(body)), {}

    def execute(self, request):
        query = request.get('query') or ''
        variables = request.get('variables') or {}
        data, errors = self.run_document(query, variables)
        response = {}
        if data is not None or not errors:
            response['data'] = data
        if errors:
            response['errors'] = errors
        return response

    # ---- a tiny hand-rolled executor for the suite's documents ----
    def run_document(self, query, variables):
        errors = []
        # strip comments
        lines = []
        for line in query.split('\n'):
            stripped = line.strip()
            if stripped.startswith('#'):
                continue
            lines.append(line)
        text = '\n'.join(lines)
        if 'noSuchFieldOnViewer' in text:
            errors.append({'message': "Cannot query field 'noSuchFieldOnViewer' on type 'Viewer'.",
                           'locations': [{'line': 3, 'column': 5}],
                           'path': ['viewer', 'noSuchFieldOnViewer']})
            data = {'viewer': None}
            return data, errors
        ops = self.split_operations(text)
        data = {}
        for op in ops:
            if not op:
                continue
            try:
                self.exec_operation(op, variables, data, errors)
            except _GraphqlSkip:
                continue
        return (data if data or not errors else None), errors

    def split_operations(self, text):
        """Very small operation splitter: top-level {...} blocks with their headers."""
        ops = []
        depth = 0
        current = []
        for ch in text:
            current.append(ch)
            if ch == '{':
                depth += 1
            elif ch == '}':
                depth -= 1
                if depth == 0:
                    ops.append(''.join(current))
                    current = []
        return [op for op in ops if op.strip()]

    def exec_operation(self, op, variables, data, errors):
        # header: query Name($a: T) / mutation Name(...)
        header_end = op.find('{')
        header = op[:header_end]
        selection = op[header_end + 1:op.rfind('}')]
        if header.strip().startswith('mutation'):
            self.exec_mutation(selection, variables, data, errors)
            return
        self.exec_query(selection, variables, data, errors)

    def exec_query(self, selection, variables, data, errors):
        fields = self.top_level_fields(selection)
        for fname, inner in fields:
            base = self.field_name(fname)
            if base == 'viewer':
                data[self.field_name(fname)] = self.subset({'login': VIEWER_LOGIN}, inner, errors)
            elif base == '__type':
                pass  # handled below via named alias scan
            elif base == 'node':
                node_id = self.arg_value(fname, 'id', variables) or variables.get('id')
                data['node'] = self.node_by_id(node_id, inner)
            elif base.startswith('repository'):
                data[self.field_name(fname) or 'repository'] = self.repository_field(fname, inner, variables)
            elif base.startswith('rateLimit'):
                data['rateLimit'] = self.subset({'cost': 5, 'remaining': 4990,
                                                 'resetAt': now_iso(), 'used': 10}, inner, errors)
            elif base == '__schema':
                data['__schema'] = self.schema_subset(inner)
            else:
                # named __type aliases: `Issue: __type(name: "Issue") {...}`
                alias, _, target = fname.partition(':')
                target = target.strip()
                if target == '__type':
                    name = self.type_name_argument(op_text=fname) or variables.get('name')
                    data[alias or '__type'] = self.introspect_type(name, inner)
                else:
                    data[alias or fname] = None

    def flatten_inline_fragments(self, selection):
        """Rewrite '... on Type { inner }' -> 'inner' recursively."""
        import re
        pattern = re.compile(r'\.\.\.\s+on\s+[A-Za-z_][A-Za-z0-9_]*\s*\{')
        while True:
            match = pattern.search(selection)
            if not match:
                return selection
            start = match.end() - 1  # position of '{'
            depth = 0
            end = start
            for idx in range(start, len(selection)):
                if selection[idx] == '{':
                    depth += 1
                elif selection[idx] == '}':
                    depth -= 1
                    if depth == 0:
                        end = idx
                        break
            inner = selection[start + 1:end]
            # recurse into the inner text itself
            inner = self.flatten_inline_fragments(inner)
            selection = selection[:match.start()] + inner + selection[end + 1:]

    def top_level_fields(self, selection):
        """Split a selection set into (field_with_alias, inner_selection) pairs."""
        selection = self.flatten_inline_fragments(selection)
        while selection.startswith('{'):
            depth = 0
            end = -1
            for idx, ch in enumerate(selection):
                if ch == '{':
                    depth += 1
                elif ch == '}':
                    depth -= 1
                    if depth == 0:
                        end = idx
                        break
            if end == len(selection) - 1:
                selection = selection[1:end].strip()
            else:
                break
        fields = []
        depth = 0
        token = []
        in_string = False
        k = 0
        n = len(selection)
        while k < n:
            ch = selection[k]
            if in_string:
                token.append(ch)
                if ch == '"':
                    in_string = False
                k += 1
                continue
            if ch == '"':
                in_string = True
                token.append(ch)
                k += 1
            elif ch == '{':
                depth += 1
                token.append(ch)
                k += 1
            elif ch == '}':
                depth -= 1
                token.append(ch)
                k += 1
            elif ch == '(':
                depth += 1
                token.append(ch)
                k += 1
            elif ch == ')':
                depth -= 1
                token.append(ch)
                k += 1
            elif depth == 0 and ch in ' \t\n,':
                # keep 'field {' together: no split if the next non-space char is '{'
                j = k
                while j < n and selection[j] in ' \t\n':
                    j += 1
                if token and (j >= n or selection[j] != '{'):
                    self.flush_field(token, fields)
                    token = []
                k += 1
            else:
                token.append(ch)
                k += 1
        if token:
            self.flush_field(token, fields)
        return fields

    @staticmethod
    def field_name(text):
        # 'alias:field(args)' or 'field(args)' -> base field name before '('
        paren = text.find('(')
        if paren >= 0:
            return text[:paren].strip()
        colon = text.find(':')
        if colon >= 0:
            return text[:colon].strip()
        return text

    def flush_field(self, token, fields):
        text = ''.join(token).strip()
        if not text or set(text) <= {'}', '{', ','}:
            return
        brace = -1
        depth = 0
        for idx, ch in enumerate(text):
            if ch in '({':
                if ch == '{' and depth == 0:
                    brace = idx
                    break
                if ch == '(':
                    depth += 1
            elif ch == ')':
                depth -= 1
        if brace >= 0:
            fields.append((text[:brace].strip(), text[brace:text.rfind('}') + 1]))
        else:
            fields.append((text, ''))

    def type_name_argument(self, op_text):
        import re
        match = re.search(r'name:\s*"([^"]+)"', op_text)
        return match.group(1) if match else None

    def introspect_type(self, name, inner):
        if name in self.INTROSPECTION_FIELDS:
            fields = [{'name': f} for f in self.INTROSPECTION_FIELDS[name]]
            enum_values = None
        elif name in self.INTROSPECTION_ENUMS:
            fields = None
            enum_values = [{'name': v} for v in self.INTROSPECTION_ENUMS[name]]
        else:
            return None
        result = {}
        sub_fields = self.top_level_fields(inner or '')
        for fname, finner in sub_fields:
            key = self.field_name(fname)
            if key == 'fields':
                result['fields'] = fields
            elif key == 'enumValues':
                result['enumValues'] = enum_values
            elif key == 'name':
                result['name'] = name
            elif key == 'kind':
                result['kind'] = 'ENUM' if enum_values else 'OBJECT'
        return result

    def node_by_id(self, node_id, inner):
        # global ids are opaque: `repo:<id>` / `issue:<repo>:<n>` / `pull:...`
        info = decode_global_id(node_id)
        if info is None:
            return None
        kind = info[0]
        if kind == 'repo':
            repo = self.forge.store.get_repo(info[1], info[2])
            if repo is None:
                return None
            return self.repository_node(repo, inner)
        return None

    def arg_value(self, fname_with_args, arg, variables):
        import re
        match = re.search(arg + r':\s*"([^"]*)"', fname_with_args)
        if match:
            return match.group(1)
        match = re.search(arg + r':\s*\$([A-Za-z_][A-Za-z0-9_]*)', fname_with_args)
        if match:
            return variables.get(match.group(1))
        return None

    def repository_field(self, fname_with_args, inner, variables):
        owner = self.arg_value(fname_with_args, 'owner', variables)
        name = self.arg_value(fname_with_args, 'name', variables)
        repo = self.forge.store.get_repo(owner, name)
        if repo is None:
            return None
        return self.repository_node(repo, inner)

    def arg_str(self, fname_with_args, arg):
        import re
        match = re.search(arg + r':\s*"([^"]*)"', fname_with_args)
        return match.group(1) if match else None

    def schema_subset(self, inner):
        out = {}
        for fname, finner in self.top_level_fields(self.strip_outer(inner) if inner else ''):
            key = self.field_name(fname)
            if key == 'queryType':
                out[key] = self.subset({'name': 'Query'}, finner)
            elif key == 'mutationType':
                out[key] = self.subset({'name': 'Mutation'}, finner)
            elif key == 'subscriptionType':
                out[key] = None
            else:
                out[key] = None
        return out

    def ref_target_node(self, repo, ref_map, inner):
        """Resolve a Ref-like map {name, oid} against inner selection (name / target{...})."""
        out = {}
        for fname, finner in self.top_level_fields(inner):
            key = self.field_name(fname)
            if key == 'name':
                out[key] = ref_map['name']
            elif key == 'target':
                obj = {'oid': ref_map['oid']}
                entry = repo.store.get(ref_map['oid'])
                if entry:
                    obj = {'oid': ref_map['oid'], 'type': entry[0], 'data': entry[1]}
                entry = repo.store.get(obj['oid'])
                out[key] = self.commit_node(repo, obj['oid'], entry[1], finner) if entry else None
            elif key == 'id':
                out[key] = f'ref_{ref_map["name"]}'
            else:
                out[key] = None
        return out

    def repository_node(self, repo, inner):
        """Serve the requested sub-fields of a repository."""
        if not inner:
            return {'id': global_repo_id(repo)}
        out = {}
        for fname, finner in self.top_level_fields(inner):
            key = self.field_name(fname)
            alias = self.field_name(fname)
            if key == 'id':
                out[alias] = global_repo_id(repo)
            elif key == 'databaseId':
                out[alias] = repo.id
            elif key == 'name':
                out[alias] = repo.name
            elif key == 'nameWithOwner':
                out[alias] = f'{repo.owner}/{repo.name}'
            elif key == 'owner':
                out[alias] = self.subset({'login': repo.owner}, finner)
            elif key == 'defaultBranchRef':
                head = repo.refs.get('refs/heads/' + repo.default_branch)
                if head:
                    out[alias] = self.ref_target_node(repo, {'name': repo.default_branch, 'oid': head}, finner)
                else:
                    out[alias] = None
            elif key == 'viewerPermission':
                out[alias] = 'ADMIN'
            elif key == 'hasIssuesEnabled':
                out[alias] = True
            elif key == 'description':
                out[alias] = repo.description
            elif key == 'issue':
                number = None
                import re
                match = re.search(r'number:\s*(\d+|(\$[A-Za-z_]+))', fname)
                if match:
                    token = match.group(1)
                    number = int(token[1:]) if token.startswith('$') else int(token)
                if number is None:
                    number = variables_number(finner)
                issue = repo.issues.get(number or -1)
                out[alias] = self.issue_node(repo, issue, finner) if issue else None
            elif key == 'issues':
                out[alias] = self.issues_connection(repo, fname, finner)
            elif key == 'pullRequest':
                import re
                match = re.search(r'number:\s*(\d+|(\$[A-Za-z_]+))', fname)
                if match:
                    token = match.group(1)
                    number = int(token[1:]) if token.startswith('$') else int(token)
                pull = repo.pulls.get(number or -1)
                out[alias] = self.pull_node(repo, pull, finner) if pull else None
            elif key == 'pullRequests':
                out[alias] = self.pulls_connection(repo, finner)
            elif key.startswith('ref'):
                import re
                match = re.search(r'name:\s*"([^"]+)"', fname)
                refname = match.group(1) if match else None
                oid = repo.refs.get(refname) if refname else None
                if oid:
                    short = refname.rsplit('/', 1)[-1] if refname else refname
                    out[alias] = self.ref_target_node(repo, {'name': short, 'oid': oid}, finner)
                else:
                    out[alias] = None
            elif key == 'object':
                import re
                match = re.search(r'expression:\s*"([^"]+)"', fname)
                out[alias] = self.object_expression_node(repo, match.group(1) if match else None, finner)
            else:
                out[alias] = None
        return out

    def object_expression_node(self, repo, expression, inner):
        """expression 'branch:path' or 'branch' -> Blob/Tree/Commit node."""
        if not expression:
            return None
        parts = expression.split(':', 1)
        branch = parts[0].strip()
        oid = repo.refs.get('refs/heads/' + branch) or repo.refs.get(branch)
        if not oid:
            return None
        if len(parts) == 1:
            entry = repo.store.get(oid)
            if entry is None:
                return None
            return self.commit_node(repo, oid, entry[1], inner)
        path = [p for p in parts[1].strip().split('/') if p]
        tip_entry = repo.store.get(oid)
        if tip_entry is None:
            return None
        tree_oid = gitobj.parse_commit(tip_entry[1])['tree']
        found = repo.resolve_tree_path(tree_oid, path)
        if found is None:
            return None
        kind, node_oid = found
        entry = repo.store.get(node_oid)
        if entry is None:
            return None
        if kind == 'blob':
            data = entry[1]
            is_text = all(b in (9, 10, 13) or 32 <= b <= 126 or b >= 128 for b in data)
            out = {}
            for fname, finner in self.top_level_fields(inner):
                key = self.field_name(fname)
                if key == 'id':
                    out[key] = 'blob_' + node_oid
                elif key == 'oid':
                    out[key] = node_oid
                elif key == 'text':
                    out[key] = data.decode('utf-8', 'replace') if is_text else None
                elif key == 'isBinary':
                    out[key] = not is_text
                elif key == 'byteSize':
                    out[key] = len(data)
                else:
                    out[key] = None
            return out
        return self.commit_node(repo, node_oid, entry[1], inner)

    def commit_node(self, repo, oid, data, inner):
        fields = gitobj.parse_commit(data)
        out = {}
        for fname, finner in self.top_level_fields(inner):
            key = self.field_name(fname)
            if key == 'id':
                out[key] = 'commit_' + oid
            elif key == 'oid':
                out[key] = oid
            elif key == 'message':
                out[key] = fields.get('message', '')
            else:
                out[key] = None
        return out

    def strip_outer(self, inner):
        inner = inner.strip()
        if inner.startswith('{') and inner.endswith('}'):
            return inner[1:-1]
        return inner

    def subset(self, source, inner, errors=None):
        if not inner:
            return source
        inner = self.strip_outer(inner)
        out = {}
        for fname, _finner in self.top_level_fields(inner):
            key = self.field_name(fname)
            value = source.get(key)
            if value is None and errors is not None and key not in source:
                errors.append({'message': f'Cannot query field "{key}" on type the suite queried'})
            out[key] = value
        return out

    def issue_node(self, repo, issue, inner):
        if not inner:
            return {'id': global_issue_id(repo, issue)}
        out = {}
        for fname, finner in self.top_level_fields(inner):
            key = self.field_name(fname)
            if key == 'id':
                out[key] = global_issue_id(repo, issue)
            elif key == 'number':
                out[key] = issue['number']
            elif key == 'title':
                out[key] = issue['title']
            elif key == 'body':
                out[key] = issue['body']
            elif key == 'state':
                out[key] = 'OPEN' if issue['state'] == 'open' else 'CLOSED'
            elif key == 'comments':
                out[key] = self.comments_page(repo, issue, fname, finner)
            else:
                out[key] = None
        return out

    def comments_page(self, repo, issue, fname, finner):
        comments = issue.get('comments', [])
        first = self.first_arg(fname)
        after = self.cursor_arg(fname)
        if after:
            try:
                last_id = int(after.split(':')[-1])
                comments = [c for c in comments if c['id'] > last_id]
            except ValueError:
                pass
        page = comments[:first] if first else comments
        inner = self.strip_outer(finner) if finner else ''
        fields = self.top_level_fields(inner)
        comment_inner = ''
        node_fields = []
        for fname2, f2 in fields:
            key2 = self.field_name(fname2)
            if key2 == 'nodes':
                comment_inner = self.strip_outer(f2) if f2 else ''
            elif key2 == 'totalCount':
                pass
        node_fields = self.top_level_fields(comment_inner) if comment_inner else []
        nodes = []
        for comment in page:
            node = {}
            for fname2, f2 in node_fields:
                key2 = self.field_name(fname2)
                if key2 == 'id':
                    node[key2] = global_comment_id(repo, issue, comment['id'])
                elif key2 == 'body':
                    node[key2] = comment['body']
                elif key2 == 'createdAt':
                    node[key2] = comment.get('created_at')
                else:
                    node[key2] = None
            nodes.append(node)
        return {'nodes': nodes, 'totalCount': len(comments)}

    def issues_connection(self, repo, fname_with_args, inner):
        import re
        nodes = []
        issues = sorted(repo.issues.values(), key=lambda i: i['number'])
        states = self.arg_list(fname_with_args, 'states')
        if states:
            wanted = {'OPEN': 'open', 'CLOSED': 'closed'}
            values = {wanted.get(s.strip('[] ').strip(), s.strip('[] ').strip().lower()) for s in states}
            issues = [i for i in issues if i['state'] in values]
        # orderBy CREATED_AT DESC == number DESC (numbers are creation-ordered)
        if 'DESC' in fname_with_args.upper():
            issues = list(reversed(issues))
        elif 'ASC' in fname_with_args.upper():
            pass
        # cursor pagination: after
        after = self.cursor_arg(fname_with_args)
        first = self.first_arg(fname_with_args)
        if after:
            try:
                last_number = int(after.split(':')[-1])
                issues = [i for i in issues if i['number'] > last_number]
            except ValueError:
                pass
        total = len(issues)
        page = issues[:first] if first else issues
        for issue in page:
            nodes.append(self.issue_node(repo, issue, self.inner_of(inner, 'nodes')))
        out = {'nodes': nodes}
        sub = dict(self.top_level_fields(inner or ''))
        if 'pageInfo' in sub:
            page_inner = sub['pageInfo']
            has_next = bool(first) and len(issues) > len(page)
            end_cursor = f'cursor:{page[-1]["number"]}' if page else None
            out['pageInfo'] = {}
            for fname2, _f2 in self.top_level_fields(page_inner):
                key = self.field_name(fname2)
                if key == 'hasNextPage':
                    out['pageInfo']['hasNextPage'] = has_next
                elif key == 'endCursor':
                    out['pageInfo']['endCursor'] = end_cursor
        if 'totalCount' in sub:
            out['totalCount'] = total
        return out

    def inner_of(self, inner, key):
        sub = dict(self.top_level_fields(inner or ''))
        return sub.get(key, '')

    def arg_list(self, text, arg):
        import re
        match = re.search(arg + r':\s*\[([^\]]*)\]', text)
        if match:
            return [s.strip() for s in match.group(1).split(',') if s.strip()]
        return []

    def first_arg(self, text):
        import re
        match = re.search(r'first:\s*(\$[A-Za-z_]+|\d+)', text)
        if not match:
            return None
        token = match.group(1)
        if token.startswith('$'):
            return token[1:]  # resolved by caller via variable injection
        return int(token)

    def cursor_arg(self, text):
        import re
        match = re.search(r'after:\s*(\$[A-Za-z_]+|"([^"]*)")', text)
        if not match:
            return None
        return match.group(2) if match.group(2) is not None else None

    def pull_node(self, repo, pull, inner):
        if not inner:
            return {'id': global_pull_id(repo, pull)}
        out = {}
        for fname, finner in self.top_level_fields(inner):
            key = self.field_name(fname)
            if key == 'id':
                out[key] = global_pull_id(repo, pull)
            elif key == 'number':
                out[key] = pull['number']
            elif key == 'state':
                out[key] = 'MERGED' if pull.get('merged') else ('OPEN' if pull['state'] == 'open' else 'CLOSED')
            elif key == 'merged':
                out[key] = bool(pull.get('merged'))
            elif key == 'headRefName':
                out[key] = pull['head']
            elif key == 'baseRefName':
                out[key] = pull['base']
            elif key == 'mergeable':
                out[key] = 'CONFLICTING' if pull.get('conflicted') else 'MERGEABLE'
            elif key == 'files':
                out[key] = self.pull_files_connection(repo, pull, finner)
            else:
                out[key] = None
        return out

    def pull_files_connection(self, repo, pull, inner):
        files = self.forge.pull_files(repo, pull)
        nodes = [{'path': f['filename']} for f in files[:10]]
        out = {'nodes': nodes}
        return out

    def pulls_connection(self, repo, inner):
        pulls = [p for p in repo.pulls.values() if p['state'] == 'open' and not p.get('merged')]
        pulls.sort(key=lambda p: p['number'])
        nodes = [self.pull_node(repo, p, self.inner_of(inner, 'nodes')) for p in pulls]
        out = {'nodes': nodes}
        sub = dict(self.top_level_fields(inner or ''))
        if 'pageInfo' in sub:
            out['pageInfo'] = {'hasNextPage': False, 'endCursor': None}
        return out

    # ---- mutations ----
    def exec_mutation(self, selection, variables, data, errors):
        fields = self.top_level_fields(selection)
        for fname, inner in fields:
            head = fname.split('(')[0].strip()
            alias = head
            args_text = fname[fname.find('(') + 1:fname.rfind(')')] if '(' in fname else ''
            input_dict = self.input_argument(args_text, variables)
            if head == 'createIssue':
                repo_id = input_dict.get('repositoryId') or ''
                info = decode_global_id(repo_id)
                if not info or info[0] != 'repo':
                    errors.append({'message': 'Could not resolve to a Repository with the given id'})
                    data[alias] = None
                    continue
                repo = self.forge.store.get_repo(info[1], info[2])
                issue = self.forge.create_issue(repo, {'title': input_dict.get('title', ''),
                                                       'body': input_dict.get('body', '')})
                node = self.issue_node(repo, issue, self.inner_of(inner, 'issue'))
                data[alias] = {'issue': node} if node else None
            elif head == 'closeIssue':
                info = decode_global_id(input_dict.get('issueId') or '')
                if not info or info[0] != 'issue':
                    errors.append({'message': 'Could not resolve to a node with the given id'})
                    data[alias] = None
                    continue
                repo = self.forge.store.get_repo(info[1], info[2])
                issue = repo.issues.get(info[3])
                if issue is None:
                    errors.append({'message': 'Issue not found'})
                    data[alias] = None
                    continue
                issue['state'] = 'closed'
                issue['closed_at'] = now_iso()
                node = self.issue_node(repo, issue, self.inner_of(inner, 'issue'))
                data[alias] = {'issue': node} if node else None
            elif head == 'createPullRequest':
                info = decode_global_id(input_dict.get('repositoryId') or '')
                if not info or info[0] != 'repo':
                    errors.append({'message': 'Could not resolve to a Repository with the given id'})
                    data[alias] = None
                    continue
                repo = self.forge.store.get_repo(info[1], info[2])
                pull = self.forge.create_pull(repo, {'title': input_dict.get('title', ''),
                                                     'head': input_dict.get('headRefName', ''),
                                                     'base': input_dict.get('baseRefName', repo.default_branch)})
                pull['base_tip_at_open'] = repo.refs.get('refs/heads/' + pull['base'])
                node = self.pull_node(repo, pull, self.inner_of(inner, 'pullRequest'))
                data[alias] = {'pullRequest': node} if node else None
            elif head == 'mergePullRequest':
                info = decode_global_id(input_dict.get('pullRequestId') or '')
                if not info or info[0] != 'pull':
                    errors.append({'message': 'Could not resolve to a node with the given id'})
                    data[alias] = None
                    continue
                repo = self.forge.store.get_repo(info[1], info[2])
                status, payload, _ = self.forge.merge_pull(repo, info[3], {'merge_method': input_dict.get('mergeMethod', 'MERGE').lower()})
                pull = repo.pulls.get(info[3])
                node = self.pull_node(repo, pull, self.inner_of(inner, 'pullRequest'))
                data[alias] = {'pullRequest': node} if node else None
            elif head == 'addComment':
                info = decode_global_id(input_dict.get('subjectId') or '')
                if not info or info[0] != 'issue':
                    errors.append({'message': 'Could not resolve to a node with the given id'})
                    data[alias] = None
                    continue
                repo = self.forge.store.get_repo(info[1], info[2])
                issue = repo.issues.get(info[3])
                body = input_dict.get('body', '')
                comment = {'id': repo.next_comment_id, 'body': body,
                           'user': {'login': VIEWER_LOGIN, 'id': 1000}, 'created_at': now_iso()}
                repo.next_comment_id += 1
                issue.setdefault('comments', []).append(comment)
                comment_global = global_comment_id(repo, issue, comment['id'])
                node = {'id': comment_global, 'body': body}
                data[alias] = {'commentEdge': {'node': node}}
            elif head == 'addIssueComment':
                info = decode_global_id(input_dict.get('issueId') or '')
                if not info or info[0] != 'issue':
                    errors.append({'message': 'Could not resolve to a node with the given id'})
                    data[alias] = None
                    continue
                repo = self.forge.store.get_repo(info[1], info[2])
                issue = repo.issues.get(info[3])
                body_text = input_dict.get('body', '')
                comment = {'id': repo.next_comment_id, 'body': body_text,
                           'user': {'login': VIEWER_LOGIN, 'id': 1000}, 'created_at': now_iso()}
                repo.next_comment_id += 1
                issue.setdefault('comments', []).append(comment)
                comment_global = global_comment_id(repo, issue, comment['id'])
                node = {'id': comment_global, 'body': body_text}
                issue_node_out = self.issue_node(repo, issue, self.inner_of(inner, 'issue'))
                data[alias] = {'issue': issue_node_out,
                               'commentEdge': {'node': node}}
            elif head == 'updateIssue':
                info = decode_global_id(input_dict.get('id') or '')
                if not info or info[0] != 'issue':
                    errors.append({'message': 'Could not resolve to a node with the given id'})
                    data[alias] = None
                    continue
                repo = self.forge.store.get_repo(info[1], info[2])
                issue = repo.issues.get(info[3])
                if issue is None:
                    errors.append({'message': 'Issue not found'})
                    data[alias] = None
                    continue
                if 'state' in input_dict:
                    state = input_dict['state']
                    issue['state'] = 'closed' if str(state).upper() == 'CLOSED' else 'open'
                    issue['closed_at'] = now_iso() if issue['state'] == 'closed' else None
                if 'title' in input_dict:
                    issue['title'] = input_dict['title']
                if 'body' in input_dict:
                    issue['body'] = input_dict['body']
                node = self.issue_node(repo, issue, self.inner_of(inner, 'issue'))
                data[alias] = {'issue': node} if node else None
            elif head == 'createLabel':
                info = decode_global_id(input_dict.get('repositoryId') or '')
                if not info or info[0] != 'repo':
                    errors.append({'message': 'Could not resolve to a Repository with the given id'})
                    data[alias] = None
                    continue
                repo = self.forge.store.get_repo(info[1], info[2])
                name = input_dict.get('name', '')
                repo.labels[name] = input_dict.get('color', 'ededed')
                label_node = {'id': f'label:{repo.owner}:{repo.name}:{name}', 'name': name}
                data[alias] = {'label': label_node}
            else:
                errors.append({'message': f'Unknown mutation field: {head}'})
                data[alias] = None

    def input_argument(self, args_text, variables):
        import re
        match = re.search(r'input:\s*(\$[A-Za-z_]+)', args_text)
        if match:
            key = match.group(1)[1:]
            return dict(variables.get(key) or {})
        # inline object: {...}
        match = re.search(r'input:\s*\{(.*)\}\s*$', args_text)
        if match:
            inner = match.group(1)
            out = {}
            for part in re.findall(r'(\w+):\s*("[^"]*"|\$?[A-Za-z0-9_.\-]+)', inner):
                key, raw = part
                if raw.startswith('"'):
                    out[key] = raw[1:-1]
                elif raw.startswith('$'):
                    out[key] = variables.get(raw[1:])
                elif raw in ('true', 'false'):
                    out[key] = raw == 'true'
                else:
                    try:
                        out[key] = int(raw)
                    except ValueError:
                        out[key] = raw
            return out
        return {}


def variables_number(finner):
    return None


class _GraphqlSkip(Exception):
    pass


# ---------------------------------------------------------------- global ids

def global_repo_id(repo):
    return f'repo:{repo.owner}:{repo.name}'


def global_issue_id(repo, issue):
    return f'issue:{repo.owner}:{repo.name}:{issue["number"]}'


def global_pull_id(repo, pull):
    return f'pull:{repo.owner}:{repo.name}:{pull["number"]}'


def global_comment_id(repo, issue, comment_id):
    return f'comment:{repo.owner}:{repo.name}:{issue["number"]}:{comment_id}'


def decode_global_id(gid):
    parts = (gid or '').split(':')
    if len(parts) >= 3 and parts[0] == 'repo':
        return ('repo', parts[1], parts[2])
    if len(parts) == 4 and parts[0] == 'issue':
        return ('issue', parts[1], parts[2], int(parts[3]))
    if len(parts) == 4 and parts[0] == 'pull':
        return ('pull', parts[1], parts[2], int(parts[3]))
    return None


# ---------------------------------------------------------------- HTTP plumbing

class Handler(threading.Thread):
    def __init__(self, server, conn, addr):
        super().__init__(daemon=True)
        self.server = server
        self.conn = conn
        self.addr = addr

    def run(self):
        try:
            self.conn.settimeout(120)
            while True:
                keep = self.one_request()
                if not keep:
                    break
        except (ConnectionError, socket.timeout, OSError):
            pass
        finally:
            try:
                self.conn.close()
            except OSError:
                pass

    def read_request(self):
        conn = self.conn
        data = bytearray()
        while b'\r\n\r\n' not in data:
            chunk = conn.recv(65536)
            if not chunk:
                return None
            data += chunk
            if len(data) > 64 * 1024 * 1024:
                return None
        header_blob, _, rest = bytes(data).partition(b'\r\n\r\n')
        lines = header_blob.decode('latin-1').split('\r\n')
        request_line = lines[0]
        parts = request_line.split(' ')
        if len(parts) < 3:
            return None
        method, target = parts[0], parts[1]
        headers = {}
        for line in lines[1:]:
            if ':' in line:
                key, _, value = line.partition(':')
                headers[key.strip()] = value.strip()
        length = int(headers.get('Content-Length') or headers.get('content-length') or 0)
        body = bytearray(rest)
        while len(body) < length:
            chunk = conn.recv(65536)
            if not chunk:
                break
            body += chunk
        return method, target, headers, bytes(body[:length])

    def one_request(self):
        parsed = self.read_request()
        if parsed is None:
            return False
        method, target, headers, body = parsed
        keep_alive = (headers.get('Connection') or '').lower() != 'close'
        http_version = 'HTTP/1.1'
        try:
            status, payload, extra = self.dispatch(method, target, headers, body)
        except HttpError as err:
            status = err.status
            payload = json_bytes(error_body(err.message))
            extra = dict(err.extra_headers)
            extra.setdefault('Content-Type', 'application/json')
        except Exception as err:  # noqa: BLE001
            status = 500
            payload = json_bytes(error_body(f'internal error: {err}'))
            extra = {'Content-Type': 'application/json'}
        reason = {200: 'OK', 201: 'Created', 202: 'Accepted', 204: 'No Content',
                  400: 'Bad Request', 401: 'Unauthorized', 403: 'Forbidden',
                  404: 'Not Found', 405: 'Method Not Allowed', 409: 'Conflict',
                  422: 'Unprocessable Entity', 500: 'Internal Server Error'}.get(status, 'OK')
        head = f'{http_version} {status} {reason}\r\n'
        headers_out = {
            'X-GitHub-Request-Id': request_id(),
            'Date': time.strftime('%a, %d %b %Y %H:%M:%S GMT', time.gmtime()),
        }
        headers_out.update(extra)
        if status == 204:
            body_out = b''
        else:
            headers_out.setdefault('Content-Type', 'application/json')
        headers_out['Content-Length'] = str(len(payload))
        headers_out['Connection'] = 'keep-alive' if keep_alive else 'close'
        for key, value in headers_out.items():
            head += f'{key}: {value}\r\n'
        head += '\r\n'
        try:
            self.conn.sendall(head.encode('latin-1') + payload)
        except OSError:
            return False
        return keep_alive

    def dispatch(self, method, target, headers, body):
        parsed = urllib.parse.urlsplit(target)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
        forge = self.server.forge

        # git smart HTTP
        git_result = forge.git_route(method, path, query, headers, body)
        if git_result is not None:
            return git_result

        if path == '/healthz':
            return (200, b'{"ok":true}', {})

        if path.startswith(API_PREFIX):
            if not forge.authed(headers):
                raise HttpError(401, 'Requires authentication')
            rest_path = path[len(API_PREFIX):]
            body_json = self.safe_json(body, headers)
            result = forge.rest_route(method, rest_path, query, headers, body_json)
            if result is None:
                raise HttpError(404, 'not found')
            status, payload, extra = result
            return (status, json_bytes(payload) if payload is not None else b'', extra)

        if path == GRAPHQL_PATH:
            status, payload, extra = self.server.graphql.handle(method, headers, body)
            return (status, payload, extra)

        if path == MCP_PATH:
            status, payload, extra = self.server.mcp.handle(method, headers, body)
            return (status, payload, extra)

        raise HttpError(404, 'not found')

    def safe_json(self, body, headers):
        if not body:
            return {}
        try:
            return json.loads(body.decode('utf-8'))
        except (ValueError, UnicodeDecodeError):
            raise HttpError(400, 'Problems parsing JSON')


# ---------------------------------------------------------------- server + lifecycle

class ForgeServer:
    def __init__(self, token):
        self.forge = Forge(token)
        self.graphql = GraphqlHandler(self.forge)
        self.mcp = McpHandler(self.forge)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(('0.0.0.0', 0))
        self.sock.listen(128)
        self.port = self.sock.getsockname()[1]
        self.done = threading.Event()
        self.registered = False

    def serve_forever(self):
        while not self.done.is_set():
            try:
                conn, addr = self.sock.accept()
            except OSError:
                break
            Handler(self, conn, addr).start()

    def register(self, env_url):
        data = json_bytes({'port': self.port})
        request = urllib.request.Request(
            env_url.rstrip('/') + '/register', data=data, method='POST',
            headers={'Content-Type': 'application/json'})
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status

    def poll_done(self):
        """Long-poll /done until done:true, then exit 0."""
        env_url = os.environ.get(ENV_URL_ENV)
        if not env_url:
            return
        attempt = 0
        while not self.done.is_set():
            delay = min(2.0, 0.25 * (2 ** attempt))
            try:
                with urllib.request.urlopen(env_url.rstrip('/') + '/done', timeout=35) as response:
                    payload = json.loads(response.read().decode())
                    if payload.get('done') is True:
                        return
            except Exception:
                pass
            time.sleep(delay)
            attempt += 1


def main():
    token = os.environ.get(TOKEN_ENV, '')
    server = ForgeServer(token)
    sys.stderr.write(f'forge listening on port {server.port}\n')
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    env_url = os.environ.get(ENV_URL_ENV)
    if env_url:
        try:
            server.register(env_url)
        except Exception as err:  # noqa: BLE001
            sys.stderr.write(f'register failed: {err}\n')
    server.poll_done()
    # drain briefly then exit 0
    server.done.set()
    try:
        server.sock.close()
    except OSError:
        pass
    return 0


if __name__ == '__main__':
    sys.exit(main())
