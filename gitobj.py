"""Git object model: hashing, tree/commit/tag parsing, packfile read and write.

Pure-Python subset of git's object formats the suite exercises: SHA-1 oids,
packfiles (version 2) with ofs-delta and ref-delta resolution, and a pack
writer that emits ofs-delta-compressed objects so clones see real deltas.
"""

import hashlib
import struct
import zlib

ZERO_OID = '0' * 40

OBJ_COMMIT = 1
OBJ_TREE = 2
OBJ_BLOB = 3
OBJ_TAG = 4
OBJ_OFS_DELTA = 6
OBJ_REF_DELTA = 7

TYPE_NAMES = {1: 'commit', 2: 'tree', 3: 'blob', 4: 'tag'}
TYPE_NUM = {'commit': OBJ_COMMIT, 'tree': OBJ_TREE, 'blob': OBJ_BLOB, 'tag': OBJ_TAG}


def hash_object(obj_type, data):
    header = f'{obj_type} {len(data)}'.encode() + b'\x00'
    return hashlib.sha1(header + data).hexdigest()


# ---------------------------------------------------------------- object parsing

def parse_tree(data):
    """Return a list of (mode, name, oid) entries."""
    entries = []
    i = 0
    while i < len(data):
        space = data.index(b' ', i)
        mode = data[i:space].decode()
        nul = data.index(b'\x00', space)
        name = data[space + 1:nul].decode('utf-8', 'surrogateescape')
        oid = data[nul + 1:nul + 21].hex()
        entries.append((mode, name, oid))
        i = nul + 21
    return entries


def encode_tree_entry(mode, name, oid):
    return f'{mode} {name}'.encode('utf-8', 'surrogateescape') + b'\x00' + bytes.fromhex(oid)


def build_tree(entries):
    return b''.join(encode_tree_entry(m, n, o) for m, n, o in entries)


def parse_commit(data):
    fields = {'parents': []}
    text = data.decode('utf-8', 'surrogateescape')
    head, _, body = text.partition('\n\n')
    for line in head.split('\n'):
        if line.startswith('tree '):
            fields['tree'] = line[5:]
        elif line.startswith('parent '):
            fields['parents'].append(line[7:])
        elif line.startswith('author '):
            fields['author'] = line[7:]
        elif line.startswith('committer '):
            fields['committer'] = line[10:]
    fields['message'] = body
    return fields


def build_commit(tree, parents, message, ident, timestamp, timezone='+0000'):
    lines = [f'tree {tree}']
    for parent in parents:
        lines.append(f'parent {parent}')
    lines.append(f'author {ident} {timestamp} {timezone}')
    lines.append(f'committer {ident} {timestamp} {timezone}')
    return ('\n'.join(lines) + '\n\n' + message).encode('utf-8', 'surrogateescape')


def build_tag_object(tag_name, target_oid, target_type, message, tagger, timestamp):
    lines = [
        f'object {target_oid}',
        f'type {target_type}',
        f'tag {tag_name}',
        f'tagger {tagger} {timestamp} +0000',
    ]
    return ('\n'.join(lines) + '\n\n' + message).encode()


# ---------------------------------------------------------------- packfile reading

class PackReader:
    """Parse a packfile (v2), resolving ofs- and ref-deltas to full objects."""

    def __init__(self, data, resolve_base=None):
        self.data = data
        self.resolve_base = resolve_base or (lambda oid: None)
        self.version = 0
        self.declared_count = 0
        self.entries: list = []
        self.objects: dict = {}
        self.delta_count = 0
        self._parse()

    def _read_header(self, pos):
        byte = self.data[pos]
        pos += 1
        obj_type = (byte >> 4) & 7
        size = byte & 15
        shift = 4
        while byte & 0x80:
            byte = self.data[pos]
            pos += 1
            size |= (byte & 0x7f) << shift
            shift += 7
        return obj_type, size, pos

    def _inflate(self, pos, expected_size):
        decomp = zlib.decompressobj()
        out = bytearray()
        chunk = 65536
        start = pos
        consumed = 0
        while not decomp.eof and start + consumed < len(self.data):
            piece = self.data[start + consumed:start + consumed + chunk]
            out += decomp.decompress(piece)
            consumed += len(piece)
            if decomp.eof:
                consumed -= len(decomp.unused_data)
        return bytes(out), start + consumed

    def _parse(self):
        data = self.data
        if len(data) < 32 or data[:4] != b'PACK':
            raise ValueError('bad pack signature')
        self.version = struct.unpack('>I', data[4:8])[0]
        self.declared_count = struct.unpack('>I', data[8:12])[0]
        stored = []          # (type_num, payload, base_ref) per entry
        index_by_offset = {}
        pos = 12
        for entry_index in range(self.declared_count):
            entry_start = pos
            index_by_offset[entry_start] = entry_index
            obj_type, _size, pos = self._read_header(pos)
            if obj_type == OBJ_OFS_DELTA:
                byte = data[pos]
                pos += 1
                distance = byte & 0x7f
                while byte & 0x80:
                    byte = data[pos]
                    pos += 1
                    distance = ((distance + 1) << 7) | (byte & 0x7f)
                base_offset = entry_start - distance
                payload, pos = self._inflate(pos, _size)
                stored.append((obj_type, payload, index_by_offset.get(base_offset)))
                self.delta_count += 1
            elif obj_type == OBJ_REF_DELTA:
                base_oid = data[pos:pos + 20].hex()
                pos += 20
                payload, pos = self._inflate(pos, _size)
                stored.append((obj_type, payload, base_oid))
                self.delta_count += 1
            else:
                payload, pos = self._inflate(pos, _size)
                stored.append((obj_type, payload, None))
        resolved = [None] * len(stored)
        done = [False] * len(stored)
        changed = True
        while changed:
            changed = False
            for index, (obj_type, payload, base_ref) in enumerate(stored):
                if done[index]:
                    continue
                if obj_type in TYPE_NAMES:
                    resolved[index] = (TYPE_NAMES[obj_type], payload)
                    done[index] = True
                    changed = True
                elif obj_type == OBJ_OFS_DELTA and base_ref is not None and done[base_ref]:
                    base_type, base_data = resolved[base_ref]
                    resolved[index] = (base_type, apply_delta(base_data, payload))
                    done[index] = True
                    changed = True
                elif obj_type == OBJ_REF_DELTA:
                    base = self.objects.get(base_ref)
                    if base is None:
                        probe = self.resolve_base(base_ref)
                        if probe is not None:
                            base = probe
                    if base is not None:
                        resolved[index] = (base['type'], apply_delta(base['data'], payload))
                        done[index] = True
                        changed = True
        for index in range(len(stored)):
            if resolved[index] is not None:
                name, payload = resolved[index]
                oid = hash_object(name, payload)
                self.objects[oid] = {'type': name, 'data': payload}
                self.entries.append({'oid': oid, 'type': name, 'data': payload})


def apply_delta(base, delta):
    pos = 0

    def varint():
        nonlocal pos
        result = 0
        shift = 0
        while True:
            b = delta[pos]
            pos += 1
            result |= (b & 0x7f) << shift
            shift += 7
            if not (b & 0x80):
                return result

    src_size = varint()
    tgt_size = varint()
    if src_size != len(base):
        raise ValueError('delta base size mismatch')
    out = bytearray()
    while pos < len(delta):
        opcode = delta[pos]
        pos += 1
        if opcode & 0x80:
            cp_off = 0
            cp_size = 0
            for i in range(4):
                if opcode & (1 << i):
                    cp_off |= delta[pos] << (8 * i)
                    pos += 1
            for i in range(3):
                if opcode & (1 << (4 + i)):
                    cp_size |= delta[pos] << (8 * i)
                    pos += 1
            if cp_size == 0:
                cp_size = 0x10000
            out += base[cp_off:cp_off + cp_size]
        elif opcode:
            out += delta[pos:pos + opcode]
            pos += opcode
        else:
            raise ValueError('bad delta opcode 0')
    if len(out) != tgt_size:
        raise ValueError('delta target size mismatch')
    return bytes(out)


# ---------------------------------------------------------------- packfile writing

def encode_obj_header(obj_type, size):
    byte = (obj_type << 4) | (size & 15)
    size >>= 4
    out = bytearray()
    while size:
        out.append(byte | 0x80)
        byte = size & 0x7f
        size >>= 7
    out.append(byte)
    return bytes(out)


def encode_ofs_distance(distance):
    chunks = [distance & 0x7f]
    distance >>= 7
    while distance:
        distance -= 1
        chunks.append((distance & 0x7f) | 0x80)
        distance >>= 7
    return bytes(reversed(chunks))


def _write_varint(out, value):
    while True:
        b = value & 0x7f
        value >>= 7
        out.append(b | (0x80 if value else 0))
        if not value:
            return


def _copy_instruction(off, size):
    op = 0x80
    enc = bytearray()
    for i in range(4):
        if (off >> (8 * i)) & 0xff:
            op |= 1 << i
            enc.append((off >> (8 * i)) & 0xff)
    for i in range(3):
        if (size >> (8 * i)) & 0xff:
            op |= 1 << (4 + i)
            enc.append((size >> (8 * i)) & 0xff)
    return bytes([op]) + bytes(enc)


def _delta_instructions(base, target):
    """Prefix-copy delta: copy the common prefix, emit the differing tail as
    literals (split when over 127 bytes). Cheap and effective for the suite's
    append-mostly revision files."""
    out = bytearray()
    _write_varint(out, len(base))
    _write_varint(out, len(target))
    common = 0
    n = min(len(base), len(target))
    while common < n and base[common] == target[common]:
        common += 1
    if common >= 16:
        off = 0
        while off < common:
            chunk = min(0xffff, common - off)
            out += _copy_instruction(off, chunk)
            off += chunk
        pos = common
        while pos < len(target):
            chunk = min(127, len(target) - pos)
            out.append(chunk)
            out += target[pos:pos + chunk]
            pos += chunk
    else:
        pos = 0
        while pos < len(target):
            chunk = min(127, len(target) - pos)
            out.append(chunk)
            out += target[pos:pos + chunk]
            pos += chunk
    return bytes(out)


def write_pack(objects, use_deltas=True):
    """objects: list of {'type': str, 'data': bytes}; returns the pack bytes.

    Each object is tried against the last full object of the same type; the
    delta is kept when smaller than the literal encoding."""
    entries = []  # (type_num, data, base_index or None, is_full)
    last_full_by_type = {}
    for index, obj in enumerate(objects):
        obj_type = TYPE_NUM[obj['type']]
        data = obj['data']
        base_index = None
        if use_deltas:
            candidate = last_full_by_type.get(obj_type)
            if candidate is not None:
                base_data = objects[candidate]['data']
                delta = _delta_instructions(base_data, data)
                if len(delta) < len(data):
                    entries.append([OBJ_OFS_DELTA, delta, candidate, False])
                    continue
        entries.append([obj_type, data, None, True])
        last_full_by_type[obj_type] = index
    out = bytearray()
    out += b'PACK' + struct.pack('>II', 2, len(entries))
    offset_of_index = []
    for index, (obj_type, data, base_index, is_full) in enumerate(entries):
        offset_of_index.append(len(out))
        if is_full:
            out += encode_obj_header(obj_type, len(data))
            out += zlib.compress(data, 1)
        else:
            distance = offset_of_index[index] - offset_of_index[base_index]
            out += encode_obj_header(OBJ_OFS_DELTA, len(data))
            out += encode_ofs_distance(distance)
            out += zlib.compress(data, 1)
    out += hashlib.sha1(bytes(out)).digest()
    return bytes(out)
