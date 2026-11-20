"""The forge's in-memory store: repositories, git objects, refs, issues, pulls.

Thread-safe under a single global lock (the suite's workloads are modest and
the GIL makes coarse locking cheaper than fine-grained structures).
"""

import threading
import time
import uuid

from gitobj import (ZERO_OID, build_commit, build_tag_object, build_tree,
                    encode_tree_entry, hash_object, parse_commit, parse_tree)


def now_iso(ts=None):
    dt = time.gmtime(ts if ts is not None else time.time())
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', dt)


class GitObjectStore:
    """oid -> (type, data) with a type index and ref-delta fallback lookup."""

    def __init__(self):
        self.objects = {}

    def put(self, obj_type, data):
        oid = hash_object(obj_type, data)
        if oid not in self.objects:
            self.objects[oid] = (obj_type, data)
        return oid

    def get(self, oid):
        return self.objects.get(oid)

    def has(self, oid):
        return oid in self.objects

    def reachable(self, tips):
        seen = set()
        queue = deque([t for t in tips if t and t != ZERO_OID])
        while queue:
            oid = queue.popleft()
            if oid in seen or oid not in self.objects:
                continue
            seen.add(oid)
            obj_type, data = self.objects[oid]
            if obj_type == 'commit':
                fields = parse_commit(data)
                queue.append(fields.get('tree'))
                queue.extend(fields.get('parents', []))
            elif obj_type == 'tree':
                for _mode, _name, entry_oid in parse_tree(data):
                    queue.append(entry_oid)
            elif obj_type == 'tag':
                text = data.decode('utf-8', 'surrogateescape')
                for line in text.split('\n'):
                    if line.startswith('object '):
                        queue.append(line[7:])
        return seen


from collections import deque  # noqa: E402  (used above)


class Repository:
    def __init__(self, repo_id, owner, name, description, private, default_branch='main'):
        self.id = repo_id
        self.owner = owner
        self.name = name
        self.description = description
        self.private = private
        self.default_branch = default_branch
        self.created_at = now_iso()
        self.updated_at = self.created_at
        self.refs = {}          # full ref name -> oid
        self.store = GitObjectStore()
        self.labels = {}        # name -> color
        self.issues = {}        # number -> issue dict (ordered by insertion)
        self.next_issue = 1
        self.pulls = {}         # number -> pull dict
        self.next_pull = 1
        self.next_comment_id = 1
        self.lock = threading.RLock()

    # -- commit helpers -------------------------------------------------
    def tree_from_files(self, files):
        """files: list of (path, bytes). Builds nested trees; returns root oid."""
        root = {}
        for path, data in files:
            parts = path.split('/')
            node = root
            for part in parts[:-1]:
                node = node.setdefault(part, {})
            node[parts[-1]] = data
        tree_cache = {}

        def build(node):
            key = id(node)
            if key in tree_cache:
                return tree_cache[key]
            entries = []
            for name, value in node.items():
                if isinstance(value, dict):
                    sub_oid = build(value)
                    entries.append(('40000', name, sub_oid))
                else:
                    blob_oid = self.store.put('blob', value)
                    entries.append(('100644', name, blob_oid))
            entries.sort(key=lambda e: (e[1] + ('/' if e[0] == '40000' else ''),))
            tree_data = build_tree(entries)
            oid = self.store.put('tree', tree_data)
            tree_cache[key] = oid
            return oid

        return build(root)

    def commit_files(self, files, message, author, timestamp, parents):
        tree_oid = self.tree_from_files(files)
        ident = f'{author} {timestamp} +0000'
        data = build_commit(tree_oid, parents, message, ident, timestamp)
        oid = self.store.put('commit', data)
        return oid

    def head_oid(self, branch=None):
        return self.refs.get('refs/heads/' + (branch or self.default_branch))

    def all_files_at(self, tree_oid, prefix=''):
        """Yield (path, blob_oid) recursively under tree_oid."""
        out = []
        entry = self.store.get(tree_oid)
        if entry is None:
            return out
        for mode, name, oid in parse_tree(entry[1]):
            if mode == '40000':
                out.extend(self.all_files_at(oid, prefix + name + '/'))
            else:
                out.append((prefix + name, oid))
        return out

    def resolve_tree_path(self, tree_oid, segments):
        """Walk path segments; returns ('tree'|'blob', oid) or None."""
        current = tree_oid
        for i, segment in enumerate(segments):
            entry = self.store.get(current)
            if entry is None:
                return None
            last = i == len(segments) - 1
            for mode, name, oid in parse_tree(entry[1]):
                if name == segment:
                    if last:
                        return ('tree' if mode == '40000' else 'blob', oid)
                    current = oid
                    break
            else:
                return None
        return ('tree', current)

    def label_names(self, issue):
        return [entry['name'] for entry in issue.get('labels', [])]

    def apply_commit_to_refs(self, files_by_branch):
        """Merge a file map into a branch's tree without rewriting history:
        reads the branch tip's files, overlays new bytes, commits."""
        branch = files_by_branch['branch']
        files = files_by_branch['files']
        message = files_by_branch['message']
        ref = 'refs/heads/' + branch
        parent = self.refs.get(ref)
        existing = {}
        if parent:
            parent_obj = self.store.get(parent)
            if parent_obj is not None:
                fields = parse_commit(parent_obj[1])
                existing = dict((path, self.store.get(oid)[1]) for path, oid in self.all_files_at(fields['tree']))
        for path, data in files.items():
            if data is None:
                existing.pop(path, None)
            else:
                existing[path] = data
        items = list(existing.items())
        tip = self.commit_files(items, message, files_by_branch['author'], files_by_branch['timestamp'],
                                [parent] if parent else [])
        self.refs[ref] = tip
        return tip

    def diff_files(self, base_tree, head_tree):
        """Changed paths between two trees: list of (path, status, old_blob, new_blob)."""
        base_files = dict(self.all_files_at(base_tree)) if base_tree else {}
        head_files = dict(self.all_files_at(head_tree)) if head_tree else {}
        changes = []
        for path in sorted(set(base_files) | set(head_files)):
            if path not in base_files:
                changes.append((path, 'added', None, head_files[path]))
            elif path not in head_files:
                changes.append((path, 'removed', base_files[path], None))
            elif base_files[path] != head_files[path]:
                changes.append((path, 'modified', base_files[path], head_files[path]))
        return changes


class Store:
    def __init__(self, viewer_login):
        self.viewer = viewer_login
        self.lock = threading.RLock()
        self.repos = {}          # (owner, name) -> Repository
        self.next_repo_id = 5000
        self.users = {}          # login -> user record

    def user(self, login):
        with self.lock:
            if login not in self.users:
                self.users[login] = {
                    'login': login, 'id': 1000 + len(self.users),
                    'name': login, 'email': f'{login}@example.invalid',
                    'created_at': now_iso(),
                }
            return self.users[login]

    def create_repo(self, name, description, private, owner=None):
        owner = owner or self.viewer
        with self.lock:
            key = (owner, name)
            if key in self.repos:
                return None
            repo = Repository(self.next_repo_id, owner, name, description or '', private)
            self.next_repo_id += 1
            self.repos[key] = repo
            return repo

    def get_repo(self, owner, name):
        return self.repos.get((owner, name))

    def delete_repo(self, owner, name):
        with self.lock:
            return self.repos.pop((owner, name), None) is not None

    def user_repos(self, owner):
        return [repo for (o, _n), repo in sorted(self.repos.items()) if o == owner]
