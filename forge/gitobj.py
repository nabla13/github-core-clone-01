"""Git objects, packs, pkt-lines and reachability — pure stdlib, no git binary.

Objects are stored verbatim (raw bytes, exactly as received or as built), so
commits and tags round-trip byte-exact. The store maps oid (hex) -> (type, raw).
"""
from __future__ import annotations

import hashlib
import struct
import zlib

ZERO_OID = "0" * 40

# pack entry types (git's numbering)
OBJ_COMMIT = 1
OBJ_TREE = 2
OBJ_BLOB = 3
OBJ_TAG = 4
OBJ_OFS_DELTA = 6
OBJ_REF_DELTA = 7

TYPE_NAMES = {OBJ_COMMIT: "commit", OBJ_TREE: "tree", OBJ_BLOB: "blob", OBJ_TAG: "tag"}
TYPE_CODES = {"commit": OBJ_COMMIT, "tree": OBJ_TREE, "blob": OBJ_BLOB, "tag": OBJ_TAG}


def oid_of(obj_type: str, payload: bytes) -> str:
    """The git SHA-1 of an object: sha1("<type> <len>\0" + payload)."""
    h = hashlib.sha1()
    h.update(obj_type.encode("ascii") + b" " + str(len(payload)).encode("ascii") + b"\0")
    h.update(payload)
    return h.hexdigest()


def raw_of(obj_type: str, payload: bytes) -> bytes:
    return obj_type.encode("ascii") + b" " + str(len(payload)).encode("ascii") + b"\0" + payload


def parse_raw(raw: bytes):
    """Split stored raw bytes into (type, payload)."""
    nul = raw.index(b"\0")
    header = raw[:nul].decode("ascii")
    obj_type, size = header.split(" ")
    payload = raw[nul + 1:]
    assert len(payload) == int(size), "stored object size mismatch"
    return obj_type, payload


# ------------------------------------------------------------------ pkt-line

def pkt_line(data: bytes) -> bytes:
    """One pkt-line: 4 hex chars (length incl. the 4) + data. Empty data -> flush (0000)."""
    if not data:
        return b"0000"
    return ("%04x" % (len(data) + 4)).encode("ascii") + data


def pkt_flush() -> bytes:
    return b"0000"


def pkt_delim() -> bytes:
    return b"0001"


def pkt_end() -> bytes:
    return b"0002"


def parse_pkt_lines(data: bytes, pos: int = 0):
    """Parse pkt-lines from data starting at pos.

    Returns (lines, end_pos) where lines is a list of bytes; flush/delim/end are
    represented as None / b'\x01' / b'\x02' sentinels. Raises ValueError on garbage.
    """
    lines = []
    n = len(data)
    while pos < n:
        if pos + 4 > n:
            raise ValueError("truncated pkt-line length")
        size = int(data[pos:pos + 4], 16)
        if size == 0:
            lines.append(None)  # flush
            pos += 4
            continue
        if size == 1:
            lines.append(b"\x01")  # delim
            pos += 4
            continue
        if size == 2:
            lines.append(b"\x02")  # response end
            pos += 4
            continue
        if size < 4 or pos + size > n:
            raise ValueError("bad pkt-line length")
        lines.append(data[pos + 4:pos + size])
        pos += size
    return lines, pos


# ------------------------------------------------------------------ objects

def build_blob(content: bytes) -> tuple[str, bytes]:
    payload = content
    return oid_of("blob", payload), raw_of("blob", payload)


def git_tree_sort_key(name: bytes) -> bytes:
    """Git sorts tree entries by name, with directories compared as name + '/'."""
    return name


def build_tree(entries) -> tuple[str, bytes]:
    """entries: iterable of (mode_bytes, name_bytes, oid_hex). Sorted git-style.

    mode is kept as given (e.g. b'100644', b'40000'); directories sort as name+'/'.
    """
    def key(item):
        mode, name, _oid = item
        return name + b"/" if mode == b"40000" else name

    payload = b"".join(
        mode + b" " + name + b"\0" + bytes.fromhex(oid)
        for mode, name, oid in sorted(entries, key=key)
    )
    return oid_of("tree", payload), raw_of("tree", payload)


def parse_tree(payload: bytes):
    """Parse a tree payload into [(mode_bytes, name_bytes, oid_hex)]."""
    out = []
    pos = 0
    n = len(payload)
    while pos < n:
        sp = payload.index(b" ", pos)
        mode = payload[pos:sp]
        nul = payload.index(b"\0", sp)
        name = payload[sp + 1:nul]
        oid = payload[nul + 1:nul + 21].hex()
        out.append((mode, name, oid))
        pos = nul + 21
    return out


def parse_commit(payload: bytes):
    """Parse a commit payload into (headers dict of lists, message bytes).

    Header continuation lines (multi-line values) are joined with '\n'.
    """
    head, sep, message = payload.partition(b"\n\n")
    headers = {}
    current = None
    for line in head.split(b"\n"):
        if line.startswith(b" ") and current is not None:
            headers[current][-1] += b"\n" + line[1:]
            continue
        key, _, value = line.partition(b" ")
        current = key.decode("ascii")
        headers.setdefault(current, []).append(value)
    return headers, message if sep else b""


def build_commit(tree_oid: str, parents, author: str, committer: str,
                 message: bytes, extra_headers=None) -> tuple[str, bytes]:
    lines = [b"tree " + tree_oid.encode("ascii")]
    for p in parents:
        lines.append(b"parent " + p.encode("ascii"))
    lines.append(b"author " + author.encode("utf-8"))
    lines.append(b"committer " + committer.encode("utf-8"))
    if extra_headers:
        lines.extend(extra_headers)
    payload = b"\n".join(lines) + b"\n\n" + message
    return oid_of("commit", payload), raw_of("commit", payload)


def parse_tag(payload: bytes):
    """Parse an annotated tag payload into (headers, message)."""
    return parse_commit(payload)


def build_tag(obj_oid: str, obj_type: str, tag_name: str, tagger: str,
              message: bytes) -> tuple[str, bytes]:
    payload = (
        b"object " + obj_oid.encode("ascii") + b"\n"
        b"type " + obj_type.encode("ascii") + b"\n"
        b"tag " + tag_name.encode("utf-8") + b"\n"
        b"tagger " + tagger.encode("utf-8") + b"\n\n" + message
    )
    return oid_of("tag", payload), raw_of("tag", payload)


# ------------------------------------------------------------------ packs

def _encode_pack_entry_header(obj_type: int, size: int) -> bytes:
    """Pack entry header: type in bits 4-6, size varint, MSB continuation."""
    out = bytearray()
    first = (obj_type << 4) | (size & 0x0F)
    size >>= 4
    if size:
        first |= 0x80
    out.append(first)
    while size:
        b = size & 0x7F
        size >>= 7
        if size:
            b |= 0x80
        out.append(b)
    return bytes(out)


def build_pack(objects) -> bytes:
    """objects: iterable of (type_str, payload) — plain (undeltified) entries.

    Returns a complete pack v2 with SHA-1 trailer.
    """
    entries = b""
    count = 0
    for obj_type, payload in objects:
        code = TYPE_CODES[obj_type]
        entries += _encode_pack_entry_header(code, len(payload))
        entries += zlib.compress(payload)
        count += 1
    body = b"PACK" + struct.pack(">II", 2, count) + entries
    return body + hashlib.sha1(body).digest()


def _read_varint(data: bytes, pos: int):
    """Read a pack entry's size varint. Returns (type_code, size, new_pos)."""
    b = data[pos]
    pos += 1
    obj_type = (b >> 4) & 0x7
    size = b & 0x0F
    shift = 4
    while b & 0x80:
        b = data[pos]
        pos += 1
        size |= (b & 0x7F) << shift
        shift += 7
    return obj_type, size, pos


def _read_ofs_delta_base(data: bytes, pos: int):
    """Read an OFS_DELTA base offset (negative offset varint). Returns (offset, new_pos)."""
    b = data[pos]
    pos += 1
    offset = b & 0x7F
    while b & 0x80:
        b = data[pos]
        pos += 1
        offset = ((offset + 1) << 7) | (b & 0x7F)
    return offset, pos


def _apply_delta(base: bytes, delta: bytes) -> bytes:
    """Apply a git binary delta to base bytes."""
    def read_varint_d(buf, p):
        v = 0
        shift = 0
        while True:
            b = buf[p]
            p += 1
            v |= (b & 0x7F) << shift
            shift += 7
            if not (b & 0x80):
                break
        return v, p

    src_size, pos = read_varint_d(delta, 0)
    if src_size != len(base):
        raise ValueError("delta base size mismatch")
    tgt_size, pos = read_varint_d(delta, pos)
    out = bytearray()
    while pos < len(delta):
        op = delta[pos]
        pos += 1
        if op & 0x80:
            cp_off = 0
            cp_size = 0
            for i in range(4):
                if op & (1 << i):
                    cp_off |= delta[pos] << (8 * i)
                    pos += 1
            for i in range(3):
                if op & (0x10 << i):
                    cp_size |= delta[pos] << (8 * i)
                    pos += 1
            if cp_size == 0:
                cp_size = 0x10000
            out.extend(base[cp_off:cp_off + cp_size])
        elif op:
            out.extend(delta[pos:pos + op])
            pos += op
        else:
            raise ValueError("delta opcode 0")
    if len(out) != tgt_size:
        raise ValueError("delta result size mismatch")
    return bytes(out)


def parse_pack(data: bytes, store=None):
    """Parse a pack v2 into a list of (oid, type_str, payload) in entry order.

    Verifies the SHA-1 trailer and the declared count. Handles plain objects and
    OFS_DELTA / REF_DELTA entries (deltas resolved against earlier entries, or —
    for thin packs — against `store`, a mapping oid -> (type, payload)).
    Raises ValueError on any inconsistency.
    """
    if len(data) < 12 + 20 or data[:4] != b"PACK":
        raise ValueError("not a packfile")
    version, count = struct.unpack(">II", data[4:12])
    if version != 2:
        raise ValueError(f"unsupported pack version {version}")
    trailer = data[-20:]
    if hashlib.sha1(data[:-20]).digest() != trailer:
        raise ValueError("pack checksum mismatch")
    body = data[12:-20]
    pos = 0
    entries = []  # (oid, type_str, payload)
    by_offset = {}
    for _ in range(count):
        start = pos
        obj_type, size, pos = _read_varint(body, pos)
        if obj_type == OBJ_OFS_DELTA:
            offset, pos = _read_ofs_delta_base(body, pos)
            base_off = start - offset
            if base_off not in by_offset:
                raise ValueError("ofs-delta base not found")
            _b_oid, base_type, base_payload = by_offset[base_off]
            decomp = zlib.decompressobj()
            delta = decomp.decompress(body[pos:])
            consumed = len(body[pos:]) - len(decomp.unused_data)
            pos += consumed
            payload = _apply_delta(base_payload, delta)
            if len(payload) != size:
                raise ValueError("delta size mismatch")
            obj_type_str = base_type
        elif obj_type == OBJ_REF_DELTA:
            base_oid = body[pos:pos + 20].hex()
            pos += 20
            decomp = zlib.decompressobj()
            delta = decomp.decompress(body[pos:])
            consumed = len(body[pos:]) - len(decomp.unused_data)
            pos += consumed
            base_entry = next(((t, p) for o, t, p in entries if o == base_oid), None)
            if base_entry is None and store is not None:
                base_entry = store.get(base_oid)
            if base_entry is None:
                raise ValueError("ref-delta base not in pack or store")
            obj_type_str, base = base_entry
            payload = _apply_delta(base, delta)
            if len(payload) != size:
                raise ValueError("delta size mismatch")
        else:
            obj_type_str = TYPE_NAMES.get(obj_type)
            if obj_type_str is None:
                raise ValueError(f"unknown pack object type {obj_type}")
            decomp = zlib.decompressobj()
            payload = decomp.decompress(body[pos:])
            consumed = len(body[pos:]) - len(decomp.unused_data)
            pos += consumed
            if len(payload) != size:
                raise ValueError("object size mismatch")
        oid = oid_of(obj_type_str, payload)
        entries.append((oid, obj_type_str, payload))
        by_offset[start] = entries[-1]
    return entries


# ------------------------------------------------------------------ reachability

def reachable_oids(store, tips, stop_at=None):
    """All object ids reachable from the given commit/tag tips (following parents,
    trees, blobs, tag targets). `store` maps oid -> (type, payload).

    `stop_at`: a set of oids whose closure is NOT traversed (have-sets); the oids
    themselves are excluded from the result as well.
    """
    stop_at = stop_at or set()
    seen = set()
    stack = list(tips)
    while stack:
        oid = stack.pop()
        if oid in seen or oid in stop_at or oid == ZERO_OID:
            continue
        entry = store.get(oid)
        if entry is None:
            continue
        seen.add(oid)
        obj_type, payload = entry
        if obj_type == "commit":
            headers, _msg = parse_commit(payload)
            tree = headers.get("tree", [b""])[0].decode("ascii")
            if tree:
                stack.append(tree)
            for parent in headers.get("parent", []):
                stack.append(parent.decode("ascii"))
        elif obj_type == "tree":
            for _mode, _name, child in parse_tree(payload):
                stack.append(child)
        elif obj_type == "tag":
            headers, _msg = parse_tag(payload)
            target = headers.get("object", [b""])[0].decode("ascii")
            if target:
                stack.append(target)
    return seen


def commit_ancestors(store, tip, stop_at=None):
    """Commit oids reachable from tip (following parents only), newest-first
    traversal order is not guaranteed; use commit_history for ordering."""
    stop_at = stop_at or set()
    seen = set()
    stack = [tip]
    while stack:
        oid = stack.pop()
        if oid in seen or oid in stop_at or oid == ZERO_OID:
            continue
        entry = store.get(oid)
        if entry is None or entry[0] != "commit":
            continue
        seen.add(oid)
        headers, _msg = parse_commit(entry[1])
        for parent in headers.get("parent", []):
            stack.append(parent.decode("ascii"))
    return seen


def commit_history(store, tip, limit=None):
    """Commits reachable from tip, newest-first by commit date then insertion.

    Walks parents in BFS order by (date desc, discovery order); deterministic.
    """
    if tip == ZERO_OID or tip not in store:
        return []
    # collect ancestors with their dates
    seen = {}
    stack = [tip]
    while stack:
        oid = stack.pop()
        if oid in seen or oid == ZERO_OID:
            continue
        entry = store.get(oid)
        if entry is None or entry[0] != "commit":
            continue
        headers, _msg = parse_commit(entry[1])
        date = b""
        committer = headers.get("committer", [b""])[0]
        # committer line ends with "<epoch> <tz>"; take the epoch
        parts = committer.rsplit(b" ", 2)
        try:
            epoch = int(parts[-2])
        except (ValueError, IndexError):
            epoch = 0
        seen[oid] = epoch
        for parent in headers.get("parent", []):
            stack.append(parent.decode("ascii"))
    ordered = sorted(seen, key=lambda o: (-seen[o], o))
    if limit is not None:
        ordered = ordered[:limit]
    return ordered


def merge_base(store, tip_a, tip_b):
    """A merge base of two commits (the newest common ancestor by date)."""
    if tip_a == tip_b:
        return tip_a
    anc_a = commit_ancestors(store, tip_a)
    anc_b = commit_ancestors(store, tip_b)
    common = anc_a & anc_b
    if not common:
        return None
    # newest by committer date
    def epoch(oid):
        entry = store.get(oid)
        if entry is None:
            return 0
        headers, _m = parse_commit(entry[1])
        committer = headers.get("committer", [b""])[0]
        parts = committer.rsplit(b" ", 2)
        try:
            return int(parts[-2])
        except (ValueError, IndexError):
            return 0
    return max(common, key=lambda o: (epoch(o), o))


def flatten_tree(store, tree_oid, prefix=""):
    """{path: (mode, oid)} for every blob in a tree, recursively."""
    out = {}
    entry = store.get(tree_oid)
    if entry is None or entry[0] != "tree":
        return out
    for mode, name, child in parse_tree(entry[1]):
        path = prefix + name.decode("utf-8", "surrogateescape")
        if mode == b"40000":
            out.update(flatten_tree(store, child, path + "/"))
        else:
            out[path] = (mode, child)
    return out


def build_tree_from_files(store, files):
    """Build nested trees for {path: (mode, oid)}; returns (root_oid, new_objects).

    new_objects: list of (type, payload) for trees created (not blobs).
    Does not write to the store.
    """
    new_objects = []

    def build(prefix, paths):
        # paths: dict relpath -> (mode, oid) under this prefix
        entries = []
        subdirs = {}
        for rel, (mode, oid) in paths.items():
            if "/" in rel:
                head, rest = rel.split("/", 1)
                subdirs.setdefault(head, {})[rest] = (mode, oid)
            else:
                entries.append((mode, rel.encode("utf-8", "surrogateescape"), oid))
        for d, sub in sorted(subdirs.items()):
            sub_oid, _ = build(prefix + d + "/", sub)
            entries.append((b"40000", d.encode("utf-8", "surrogateescape"), sub_oid))
        oid, raw = build_tree(entries)
        _t, payload = parse_raw(raw)
        new_objects.append(("tree", payload))
        return oid, raw

    root_oid, _ = build("", files)
    return root_oid, new_objects
