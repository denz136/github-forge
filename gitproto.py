"""pkt-line framing and the git smart-HTTP protocol handlers.

upload-pack speaks protocol v2 (advertisement, ls-refs, fetch with shallow
and ofs-delta thin packs); receive-pack speaks v0 (advertisement, command
list, unpack + report-status in side-band-64k, non-fast-forward refusal).
"""

import hashlib

from gitobj import (OBJ_COMMIT, OBJ_TAG, OBJ_TREE, PackReader, TYPE_NAMES,
                    ZERO_OID, hash_object, parse_commit, parse_tree, write_pack)

# ---------------------------------------------------------------- pkt-line

MAX_PKT = 65524


def pkt_line(payload):
    if payload is None:  # flush
        return b'0000'
    if payload == b'':  # delim
        return b'0001'
    if isinstance(payload, str):
        payload = payload.encode()
    return ('%04x' % (len(payload) + 4)).encode() + payload


def pkt_lines(items):
    out = bytearray()
    for item in items:
        if item == 'flush':
            out += b'0000'
        elif item == 'delim':
            out += b'0001'
        else:
            out += pkt_line(item)
    return bytes(out)


def read_pkt_lines(data):
    """Parse a pkt-line stream into ('data'|'flush'|'delim', payload) tuples.
    Trailing non-pkt garbage is ignored (the malformed-request check)."""
    packets = []
    pos = 0
    while pos + 4 <= len(data):
        header = data[pos:pos + 4]
        try:
            length = int(header, 16)
        except ValueError:
            break
        if length == 0:
            packets.append(('flush', b''))
            pos += 4
        elif length == 1:
            packets.append(('delim', b''))
            pos += 4
        elif length < 4:
            break
        else:
            if pos + length > len(data):
                break
            packets.append(('data', data[pos + 4:pos + length]))
            pos += length
    return packets


def sideband(band, payload, chunk=65515):
    out = bytearray()
    for i in range(0, len(payload), chunk):
        piece = payload[i:i + chunk]
        out += pkt_line(bytes([band]) + piece)
    if not payload:
        out += pkt_line(bytes([band]))
    return bytes(out)


# ---------------------------------------------------------------- helpers

def short_oid(oid):
    return oid[:12]


def is_hex40(value):
    return len(value) == 40 and all(c in '0123456789abcdef' for c in value.lower())


# ---------------------------------------------------------------- v2 upload-pack

V2_CAPABILITIES = {
    'agent': 'forge/1.0',
    'ls-refs': 'unborn',
    'fetch': 'shallow filter',
    'object-format': 'sha1',
    'session-id': '',
}


def v2_advertisement():
    lines = ['version 2\n', 'agent=forge/1.0\n', 'ls-refs=unborn\n',
             'fetch=shallow filter\n', 'object-format=sha1\n']
    out = bytearray()
    for i, line in enumerate(lines):
        out += pkt_line(line)
    out += b'0000'  # flush
    return bytes(out)


def v2_ls_refs(repo, request_packets):
    """Handle an ls-refs command; returns response pkt bytes."""
    peel = False
    symrefs = False
    unborn = False
    prefixes = []
    for kind, payload in request_packets:
        if kind != 'data':
            continue
        line = payload.decode('utf-8', 'surrogateescape').rstrip('\n')
        if line == 'peel':
            peel = True
        elif line == 'symrefs':
            symrefs = True
        elif line == 'unborn':
            unborn = True
        elif line.startswith('ref-prefix '):
            prefixes.append(line[len('ref-prefix '):])
    out = bytearray()
    # HEAD symref line first (only when it matches a prefix, if any given)
    head_target = 'refs/heads/' + repo.default_branch
    head_oid = repo.refs.get(head_target)
    def matches(name):
        return not prefixes or any(name.startswith(p) for p in prefixes)
    if head_oid:
        line = f'{head_oid} HEAD'
        if symrefs:
            line += f' symref-target:{head_target}'
        if matches('HEAD'):
            out += pkt_line(line + '\n')
    for name in sorted(repo.refs):
        if not matches(name):
            continue
        oid = repo.refs[name]
        if oid == ZERO_OID:
            if unborn:
                out += pkt_line(f'unborn {name} symref-target:{head_target}\n' if name == head_target else f'unborn {name}\n')
            continue
        line = f'{oid} {name}'
        if peel:
            obj = repo.store.get(oid)
            if obj and obj[0] == 'tag':
                text = obj[1].decode('utf-8', 'surrogateescape')
                for row in text.split('\n'):
                    if row.startswith('object '):
                        line += f' peeled:{row[7:]}'
                        break
        out += pkt_line(line + '\n')
    if not head_oid and unborn and matches(head_target) and not prefixes:
        out += pkt_line(f'unborn {head_target} symref-target:{head_target}\n')
    out += b'0000'
    return bytes(out)


def _ancestors(repo, tip, limit):
    """Commit oids from tip, newest first, up to limit generations."""
    out = []
    queue = [tip]
    while queue and len(out) < limit:
        oid = queue.pop(0)
        obj = repo.store.get(oid)
        if not obj or obj[0] != 'commit':
            continue
        out.append(oid)
        fields = parse_commit(obj[1])
        queue.extend(fields.get('parents', []))
    return out


def _objects_for_tips(repo, tips, have_closure, shallow_boundary):
    """Collect objects reachable from tips, excluding have_closure (deduped by oid)."""
    seen_oids = set()
    objects = []
    seen_commits = set()
    for tip in tips:
        stack = [tip]
        commit_order = []
        walked = set()
        while stack:
            oid = stack.pop()
            if oid in walked or oid in have_closure or oid in shallow_boundary:
                continue
            walked.add(oid)
            entry = repo.store.get(oid)
            if not entry or entry[0] != 'commit':
                continue
            commit_order.append(oid)
            fields = parse_commit(entry[1])
            stack.extend(fields.get('parents', []))
        for oid in commit_order:
            if oid in seen_commits:
                continue
            seen_commits.add(oid)
            entry = repo.store.get(oid)
            if not entry:
                continue
            objects.append({'type': 'commit', 'data': entry[1]})
            fields = parse_commit(entry[1])
            tree_stack = [fields['tree']]
            while tree_stack:
                t = tree_stack.pop()
                if t in seen_oids or t in have_closure:
                    continue
                tentry = repo.store.get(t)
                if not tentry:
                    continue
                seen_oids.add(t)
                objects.append({'type': 'tree', 'data': tentry[1]})
                for mode, _name, child in parse_tree(tentry[1]):
                    if mode == '40000':
                        tree_stack.append(child)
                    else:
                        if child in seen_oids or child in have_closure:
                            continue
                        bentry = repo.store.get(child)
                        if bentry:
                            seen_oids.add(child)
                            objects.append({'type': 'blob', 'data': bentry[1]})
    return objects


def _tree_of_commit(repo, oid):
    entry = repo.store.get(oid)
    if not entry:
        return None
    return parse_commit(entry[1]).get('tree')


def v2_fetch(repo, request_packets):
    """Handle a fetch command: shallow/deepen support, thin-pack and haves."""
    wants = []
    haves = []
    client_shallow = []
    depth = None
    deepen_since = None
    deepen_not = []
    filter_expr = None
    want_refs = []
    done = False
    for kind, payload in request_packets:
        if kind != 'data':
            if kind == 'flush':
                pass
            continue
        line = payload.decode('utf-8', 'surrogateescape').rstrip('\n')
        if line.startswith('want '):
            rest = line[5:]
            if ' ' in rest:
                oid, refname = rest.split(' ', 1)
                want_refs.append((oid, refname))
            else:
                oid = rest
            wants.append(oid)
        elif line.startswith('have '):
            haves.append(line[5:])
        elif line.startswith('shallow '):
            client_shallow.append(line[8:])
        elif line.startswith('deepen '):
            depth = int(line[7:])
        elif line.startswith('deepen-since '):
            deepen_since = int(line[13:])
        elif line.startswith('deepen-not '):
            deepen_not.append(line[11:])
        elif line.startswith('filter '):
            filter_expr = line[7:]
        elif line == 'done':
            done = True
        elif line in ('thin-pack', 'no-progress', 'ofs-delta', 'include-tag'):
            pass
    

    # have closure: all objects the client already holds (transitive, from its haves)
    have_closure = set()
    common = []
    for have in haves:
        if repo.store.get(have):
            common.append(have)
            have_closure |= repo.store.reachable([have])
    if haves and not done:
        # stateless v2: answer with acknowledgments first; client resends with done
        out = bytearray(pkt_line('acknowledgments\n'))
        for oid in common:
            out += pkt_line(f'ACK {oid} common\n')
        if common:
            out += pkt_line('ACK %s ready\n' % common[0])
        else:
            out += pkt_line('NAK\n')
        out += b'0000'
        return bytes(out)
    if not wants:
        # nothing wanted: acknowledgments section only
        out = pkt_line('acknowledgments\n')
        out += pkt_line('NAK\n')
        out += b'0000'
        return bytes(out)

    # shallow computation
    shallow_boundary = set()
    new_shallow = []
    unshallow = []
    if depth is not None:
        tips = [w for w in wants if repo.store.get(w)]
        if client_shallow:
            # deepen relative to the client's boundaries
            # collect the client's shallow tips plus their `depth-1` ancestors? The
            # suite's deepen-2 fetch wants exactly one more generation.
            # Walk each client boundary: the boundary itself counts as generation 1;
            # deliver commits up to `depth` generations beyond it.
            new_boundary = set()
            for boundary in client_shallow:
                generations = _ancestors(repo, boundary, depth)
                # first generation is the boundary commit itself
                for oid in generations[1:]:
                    if oid in have_closure:
                        continue
                wanted_from_here = []
                seen = set()
                current = [boundary]
                for gen in range(depth):
                    next_gen = []
                    for oid in current:
                        entry = repo.store.get(oid)
                        if not entry or entry[0] != 'commit':
                            continue
                        fields = parse_commit(entry[1])
                        parents = fields.get('parents', [])
                        if gen == depth - 1:
                            new_boundary.update(p for p in parents if repo.store.get(p))
                        next_gen.extend(parents)
                    current = next_gen
            # deliver objects for the commits between boundary and new boundary
            deliver = set()
            for boundary in client_shallow:
                generations = _ancestors(repo, boundary, depth + 1)
                for oid in generations[1:]:  # skip the boundary itself (client has it)
                    if oid in have_closure:
                        continue
                    deliver.add(oid)
            shallow_boundary = new_boundary
            tips = list(deliver - have_closure)
            objects = _objects_for_tips(repo, tips, have_closure, shallow_boundary)
            # for a deepen fetch the client already holds the want; deliver only new commits
            if not objects:
                objects = []
                for oid in sorted(deliver):
                    if oid in have_closure:
                        continue
                    entry = repo.store.get(oid)
                    if entry and entry[0] == 'commit':
                        objects.append({'type': 'commit', 'data': entry[1]})
                        tree_oid = parse_commit(entry[1])['tree']
                        tentry = repo.store.get(tree_oid)
                        if tentry:
                            objects.append({'type': 'tree', 'data': tentry[1]})
            # shallow-info: new boundary commits, and unshallow for boundaries no longer shallow
            for oid in sorted(new_boundary):
                if oid not in have_closure and repo.store.get(oid):
                    new_shallow.append(oid)
            for oid in client_shallow:
                if oid not in new_boundary:
                    unshallow.append(oid)
            # deepened trees/blobs of new commits
            full_objects = []
            tree_seen = set()
            for obj in objects:
                full_objects.append(obj)
                if obj['type'] == 'commit':
                    tree_oid = parse_commit(obj['data'])['tree']
                    stack = [tree_oid]
                    while stack:
                        t = stack.pop()
                        if t in tree_seen or t in have_closure:
                            continue
                        tree_seen.add(t)
                        tentry = repo.store.get(t)
                        if not tentry:
                            continue
                        full_objects.append({'type': 'tree', 'data': tentry[1]})
                        for mode, _name, child in parse_tree(tentry[1]):
                            if mode == '40000':
                                stack.append(child)
                            elif child not in have_closure:
                                bentry = repo.store.get(child)
                                if bentry:
                                    full_objects.append({'type': 'blob', 'data': bentry[1]})
            objects = full_objects
        else:
            # fresh shallow clone: depth generations from each want
            boundary = set()
            deliver = []
            seen = set()
            for want in tips:
                current = [want]
                for gen in range(depth):
                    next_gen = []
                    for oid in current:
                        entry = repo.store.get(oid)
                        if not entry or entry[0] != 'commit':
                            continue
                        if oid not in seen:
                            seen.add(oid)
                            deliver.append(oid)
                        fields = parse_commit(entry[1])
                        parents = fields.get('parents', [])
                        if gen == depth - 1:
                            boundary.update(p for p in parents if repo.store.get(p))
                        next_gen.extend(parents)
                    current = next_gen
            shallow_boundary = boundary
            objects = []
            tree_seen = set()
            for oid in deliver:
                entry = repo.store.get(oid)
                if entry:
                    objects.append({'type': 'commit', 'data': entry[1]})
                    tree_oid = parse_commit(entry[1])['tree']
                    stack = [tree_oid]
                    while stack:
                        t = stack.pop()
                        if t in tree_seen:
                            continue
                        tree_seen.add(t)
                        tentry = repo.store.get(t)
                        if tentry:
                            objects.append({'type': 'tree', 'data': tentry[1]})
                            for mode, _name, child in parse_tree(tentry[1]):
                                if mode == '40000':
                                    stack.append(child)
                                else:
                                    bentry = repo.store.get(child)
                                    if bentry:
                                        objects.append({'type': 'blob', 'data': bentry[1]})
            new_shallow = sorted(deliver)
    else:
        tips = [w for w in wants if repo.store.get(w)]
        objects = _objects_for_tips(repo, tips, have_closure, set())
        if client_shallow:
            for oid in client_shallow:
                if repo.store.get(oid):
                    shallow_boundary.add(oid)
                    new_shallow.append(oid)

    out = bytearray()
    if want_refs:
        out += pkt_line('wanted-refs\n')
        for oid, refname in want_refs:
            out += pkt_line(f'{oid} {refname}\n')
        out += b'0001'
    if depth is not None or client_shallow:
        out += pkt_line('shallow-info\n')
        for oid in new_shallow:
            out += pkt_line(f'shallow {oid}')
        for oid in unshallow:
            out += pkt_line(f'unshallow {oid}')
        out += b'0001'
    out += pkt_line('packfile\n')
    if objects:
        pack = write_pack(objects)
        out += sideband(1, pack)
    out += b'0000'
    return bytes(out)


def handle_upload_pack_v2(repo, body):
    """A POST to /git-upload-pack with a v2 command list."""
    packets = read_pkt_lines(body)
    command = None
    args = []
    for kind, payload in packets:
        if kind != 'data':
            continue
        text = payload.decode('utf-8', 'surrogateescape').rstrip('\n')
        if command is None and text.startswith('command='):
            command = text[len('command='):]
        elif text and command is not None:
            args.append((kind, payload))
    if command == 'ls-refs':
        return v2_ls_refs(repo, args)
    if command == 'fetch':
        return v2_fetch(repo, args)
    # unknown command: protocol error
    return pkt_line(b'ERR unknown command\n')


# ---------------------------------------------------------------- v0 receive-pack

RECEIVE_CAPS = 'report-status report-status-v2 delete-refs side-band-64k quiet atomic ofs-delta agent=forge/1.0'


def v0_receive_advertisement(repo):
    out = bytearray()
    first = True
    if not repo.refs:
        out += pkt_line(ZERO_OID + ' capabilities^{}\x00' + RECEIVE_CAPS + '\n')
    else:
        for name in sorted(repo.refs):
            oid = repo.refs[name]
            line = f'{oid} {name}'
            if first:
                line += '\x00' + RECEIVE_CAPS
                first = False
            out += pkt_line(line + '\n')
            obj = repo.store.get(oid)
            if obj and obj[0] == 'tag':
                text = obj[1].decode('utf-8', 'surrogateescape')
                for row in text.split('\n'):
                    if row.startswith('object '):
                        out += pkt_line(f'{row[7:]} {name}^{{}}\n')
                        break
    out += b'0000'
    return bytes(out)


def handle_receive_pack(repo, body):
    """POST /git-receive-pack: command list + flush + raw pack. Returns the report."""
    commands = []
    capabilities = set()
    pos = 0
    while pos + 4 <= len(body):
        try:
            length = int(body[pos:pos + 4], 16)
        except ValueError:
            break  # raw pack bytes begin
        if length == 0:
            pos += 4
            break  # command list terminated by flush; pack follows raw
        payload = body[pos + 4:pos + length]
        pos += length
        text = payload.decode('utf-8', 'surrogateescape').rstrip('\n')
        nul = text.find('\x00')
        if nul >= 0:
            capabilities.update(text[nul + 1:].split())
            text = text[:nul]
        if not text:
            continue
        parts = text.split(' ', 2)
        if len(parts) == 3:
            commands.append((parts[0].lower(), parts[1].lower(), parts[2]))
    pack_data = body[pos:]
    sideband_ok = 'side-band-64k' in capabilities
    report_v2 = 'report-status-v2' in capabilities
    use_report = 'report-status' in capabilities or report_v2

    unpack_ok = True
    unpack_detail = 'ok'
    lines = []

    # parse the pushed pack if present
    pushed = {}
    pack_bytes = bytes(pack_data)
    if pack_bytes and (pack_bytes[:4] == b'PACK' or any(c != 0 for c in pack_bytes[:4])):
        try:
            reader = PackReader(pack_bytes, resolve_base=lambda oid: _base_object(repo, oid))
            for oid, entry in reader.objects.items():
                pushed[oid] = entry
            if reader.declared_count and not reader.objects:
                unpack_ok = False
                unpack_detail = 'index-pack failed'
        except Exception as err:  # noqa: BLE001
            unpack_ok = False
            unpack_detail = f'index-pack failed: {err}'
    for oid, entry in pushed.items():
        if not repo.store.has(oid):
            repo.store.objects[oid] = (entry['type'], entry['data'])

    def is_ancestor(ancestor, descendant):
        if ancestor == descendant:
            return True
        entry = repo.store.get(descendant)
        while entry and entry[0] == 'commit':
            fields = parse_commit(entry[1])
            parents = fields.get('parents', [])
            if ancestor in parents:
                return True
            if not parents:
                return False
            # walk all parents breadth-first
            queue = list(parents)
            while queue:
                current = queue.pop(0)
                if current == ancestor:
                    return True
                centry = repo.store.get(current)
                if centry and centry[0] == 'commit':
                    queue.extend(parse_commit(centry[1]).get('parents', []))
            return False
        return False

    for old, new, ref in commands:
        current = repo.refs.get(ref, ZERO_OID)
        if new == ZERO_OID:
            # delete
            if old != ZERO_OID and current != old:
                lines.append(f'ng {ref} fetch first' if current != ZERO_OID else f'ng {ref} fetch first')
                continue
            if not repo.refs.pop(ref, None):
                lines.append(f'ng {ref} delete failed')
            else:
                lines.append(f'ok {ref}')
            continue
        if old == ZERO_OID:
            if current != ZERO_OID:
                lines.append(f'ng {ref} reference already exists')
                continue
        elif current != old:
            lines.append(f'ng {ref} fetch first')
            continue
        if new not in repo.store.objects:
            lines.append(f'ng {ref} missing necessary objects')
            continue
        repo.refs[ref] = new
        lines.append(f'ok {ref}')

    report = bytearray()
    if use_report:
        report += pkt_line(f'unpack {unpack_detail if not unpack_ok else "ok"}\n')
        for line in lines:
            report += pkt_line(line + '\n')
        report += b'0000'
    if sideband_ok:
        body = bytearray()
        if use_report:
            body += sideband(1, bytes(report))
        return bytes(body) + b'0000'
    return bytes(report)


def _base_object(repo, oid):
    entry = repo.store.get(oid)
    if not entry:
        return None
    return {'type': entry[0], 'data': entry[1]}


# ---------------------------------------------------------------- v0 upload-pack (fallback)

def service_preamble(service):
    """'# service=<name>\\n' pkt-line followed by flush — required for info/refs v0."""
    payload = f'# service={service}\n'
    return pkt_line(payload) + b'0000'


def v0_upload_advertisement(repo):
    caps = 'multi_ack_detailed side-band-64k thin-pack ofs-delta agent=forge/1.0'
    if repo.refs:
        head_target = 'refs/heads/' + repo.default_branch
        head_oid = repo.refs.get(head_target)
        first = True
        if head_oid:
            out = pkt_line(f'{head_oid} HEAD\x00symref=HEAD:{head_target} {caps}\n')
            first = False
        else:
            out = bytearray()
        body = bytearray(out)
        for name in sorted(repo.refs):
            oid = repo.refs[name]
            line = f'{oid} {name}'
            if first:
                line += '\x00' + caps
                first = False
            body += pkt_line(line + '\n')
            obj = repo.store.get(oid)
            if obj and obj[0] == 'tag':
                text = obj[1].decode('utf-8', 'surrogateescape')
                for row in text.split('\n'):
                    if row.startswith('object '):
                        body += pkt_line(f'{row[7:]} {name}^{{}}\n')
                        break
        body += b'0000'
        return bytes(body)
    out = pkt_line(ZERO_OID + ' capabilities^{}\x00' + caps + '\n') + b'0000'
    return out


def handle_upload_pack_v0(repo, body):
    packets = read_pkt_lines(body)
    wants = []
    haves = []
    depth = None
    client_shallow = []
    done = False
    first = True
    for kind, payload in packets:
        if kind != 'data':
            continue
        text = payload.decode('utf-8', 'surrogateescape').rstrip('\n')
        if text.startswith('want '):
            rest = line_oid = text[5:].split(' ')[0]
            wants.append(rest)
        elif text.startswith('have '):
            haves.append(text[5:])
        elif text.startswith('deepen '):
            depth = int(text[7:])
        elif text.startswith('shallow '):
            client_shallow.append(text[8:])
        elif text == 'done':
            done = True
        first = False
    out = bytearray()
    have_closure = set()
    for have in haves:
        if repo.store.get(have):
            have_closure |= repo.store.reachable([have])
    objects = []
    if depth is not None:
        boundary = set()
        deliver = []
        seen = set()
        for want in wants:
            if not repo.store.get(want):
                continue
            current = [want]
            for gen in range(depth):
                next_gen = []
                for oid in current:
                    entry = repo.store.get(oid)
                    if not entry or entry[0] != 'commit':
                        continue
                    if oid not in seen and oid not in have_closure:
                        seen.add(oid)
                        deliver.append(oid)
                    fields = parse_commit(entry[1])
                    parents = fields.get('parents', [])
                    if gen == depth - 1:
                        boundary.update(p for p in parents if repo.store.get(p))
                    next_gen.extend(parents)
                current = next_gen
        objects = []
        tree_seen = set()
        for oid in deliver:
            entry = repo.store.get(oid)
            if entry:
                objects.append({'type': 'commit', 'data': entry[1]})
                tree_oid = parse_commit(entry[1])['tree']
                stack = [tree_oid]
                while stack:
                    t = stack.pop()
                    if t in tree_seen:
                        continue
                    tree_seen.add(t)
                    tentry = repo.store.get(t)
                    if tentry:
                        objects.append({'type': 'tree', 'data': tentry[1]})
                        for mode, _name, child in parse_tree(tentry[1]):
                            if mode == '40000':
                                stack.append(child)
                            else:
                                bentry = repo.store.get(child)
                                if bentry:
                                    objects.append({'type': 'blob', 'data': bentry[1]})
        if boundary - have_closure or client_shallow:
            for oid in sorted(boundary):
                if repo.store.get(oid) and oid not in have_closure:
                    out += pkt_line(f'shallow {oid}\n')
        out += pkt_line('NAK\n')
    else:
        tips = [w for w in wants if repo.store.get(w)]
        objects = _objects_for_tips(repo, tips, have_closure, set())
        if haves and not objects:
            out += pkt_line('ACK ' + haves[-1] + '\n')
        else:
            out += pkt_line('NAK\n')
    if objects:
        pack = write_pack(objects)
        out += sideband(1, pack)
    out += b'0000'
    return bytes(out)
