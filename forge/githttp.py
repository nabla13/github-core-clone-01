"""Git smart-HTTP handlers — see specs/git-spec.md for the exact contract.

Serves: GET /<owner>/<repo>.git/info/refs?service=...
        POST /<owner>/<repo>.git/git-upload-pack
        POST /<owner>/<repo>.git/git-receive-pack

No auth is enforced on the git surface. Repos are created only via REST.
"""
from __future__ import annotations

from . import gitobj
from .httpd import Response
from .store import ForgeError

ZERO = gitobj.ZERO_OID
AGENT = "forge/1.0"

UPLOAD_AD_CT = "application/x-git-upload-pack-advertisement"
RECEIVE_AD_CT = "application/x-git-receive-pack-advertisement"
UPLOAD_RESULT_CT = "application/x-git-upload-pack-result"
RECEIVE_RESULT_CT = "application/x-git-receive-pack-result"

# v2 capabilities (fetch value must include 'shallow' for depth fetches;
# ls-refs value must include 'unborn' for unborn ls-refs).
V2_CAPS = ["agent=forge/1.0", "ls-refs=unborn", "fetch=shallow filter",
           "object-format=sha1"]
# v0 upload-pack capabilities (first ref line, after NUL).
V0_UPLOAD_CAPS = ("symref=HEAD:refs/heads/main agent=forge/1.0 object-format=sha1 "
                  "side-band-64k thin-pack no-progress ofs-delta multi_ack_detailed shallow")
# v0 receive-pack capabilities.
V0_RECEIVE_CAPS = ("symref=HEAD:refs/heads/main agent=forge/1.0 object-format=sha1 "
                   "report-status side-band-64k delete-refs ofs-delta")

MAX_PKT = 65520
SIDEBAND_CHUNK = 65515  # pack bytes per band-1 packet (payload cap 65516 incl. band byte)


def _pkt(data: bytes) -> bytes:
    return gitobj.pkt_line(data)


def _flush() -> bytes:
    return b"0000"


def _delim() -> bytes:
    return b"0001"


# ------------------------------------------------------------------ ref helpers

def _all_refs(repo):
    """All refs as (refname, oid), sorted: heads first, then tags."""
    heads = sorted(r for r in repo.refs if r.startswith("refs/heads/"))
    tags = sorted(r for r in repo.refs if r.startswith("refs/tags/"))
    other = sorted(r for r in repo.refs
                   if not r.startswith("refs/heads/") and not r.startswith("refs/tags/"))
    return [(r, repo.refs[r]) for r in heads + tags + other]


def _peeled_oid(repo, ref, oid):
    """The commit a tag ref peels to, or None (lightweight tag / non-tag)."""
    entry = repo.objects.get(oid)
    if entry is None or entry[0] != "tag":
        return None
    headers, _msg = gitobj.parse_tag(entry[1])
    target = headers.get("object", [b""])[0].decode("ascii")
    # peel through nested tags
    while True:
        target_entry = repo.objects.get(target)
        if target_entry is None or target_entry[0] != "tag":
            return target
        headers, _msg = gitobj.parse_tag(target_entry[1])
        target = headers.get("object", [b""])[0].decode("ascii")


def _head_oid(repo):
    return repo.refs.get(f"refs/heads/{repo.default_branch}", ZERO)


def _is_ancestor(repo, ancestor, tip):
    """True when `ancestor` is reachable from `tip` (through parents, peeling tags)."""
    if ancestor == tip:
        return True
    if ancestor == ZERO or tip == ZERO:
        return False
    # peel the tip to a commit if it is a tag
    entry = repo.objects.get(tip)
    if entry is not None and entry[0] == "tag":
        headers, _m = gitobj.parse_tag(entry[1])
        tip = headers.get("object", [b""])[0].decode("ascii")
    return ancestor in gitobj.commit_ancestors(repo.objects, tip)


# ------------------------------------------------------------------ advertisements

def _v0_advertisement(repo, service: str, caps: str) -> bytes:
    out = _pkt(f"# service={service}\n".encode("ascii")) + _flush()
    refs = _all_refs(repo)
    if not refs:
        out += _pkt(f"{ZERO} capabilities^{{}}\0{caps}\n".encode("ascii"))
    else:
        first = True
        head_oid = _head_oid(repo)
        lines = [(f"HEAD", head_oid)] + refs
        for ref, oid in lines:
            suffix = f"\0{caps}" if first else ""
            out += _pkt(f"{oid} {ref}{suffix}\n".encode("ascii"))
            first = False
            peeled = _peeled_oid(repo, ref, oid)
            if peeled is not None:
                out += _pkt(f"{peeled} {ref}^{{}}\n".encode("ascii"))
    out += _flush()
    return out


def _v2_advertisement(repo) -> bytes:
    out = _pkt(b"# service=git-upload-pack\n") + _flush()
    out += _pkt(b"version 2\n")
    for cap in V2_CAPS:
        out += _pkt(f"{cap}\n".encode("ascii"))
    out += _flush()
    return out


def info_refs(request, owner, repo_name):
    service = request.query_one("service", "")
    repo = request.store.repos.get((owner, repo_name))
    if repo is None:
        return Response(404, b'{"message":"Repository not found"}',
                        {"Content-Type": "application/json"})
    is_v2 = (request.headers.get("git-protocol", "") == "version=2")
    if service == "git-upload-pack":
        if is_v2:
            body = _v2_advertisement(repo)
        else:
            body = _v0_advertisement(repo, service, V0_UPLOAD_CAPS)
        return Response(200, body, {"Content-Type": UPLOAD_AD_CT})
    if service == "git-receive-pack":
        body = _v0_advertisement(repo, service, V0_RECEIVE_CAPS)
        return Response(200, body, {"Content-Type": RECEIVE_AD_CT})
    return Response(400, b'{"message":"unknown service"}',
                    {"Content-Type": "application/json"})


# ------------------------------------------------------------------ v2 ls-refs

def _parse_pkt_stream(body: bytes):
    """Parse a pkt-line stream into a list of (kind, payload) where kind is
    'data' | 'flush' | 'delim' | 'end'. Raises ValueError on garbage."""
    out = []
    pos = 0
    n = len(body)
    while pos < n:
        if pos + 4 > n:
            raise ValueError("truncated pkt-line")
        size = int(body[pos:pos + 4], 16)
        if size == 0:
            out.append(("flush", b""))
            pos += 4
        elif size == 1:
            out.append(("delim", b""))
            pos += 4
        elif size == 2:
            out.append(("end", b""))
            pos += 4
        elif size < 4 or pos + size > n:
            raise ValueError("bad pkt-line length")
        else:
            out.append(("data", body[pos + 4:pos + size]))
            pos += size
    return out


def _ls_refs(repo, peel: bool, symrefs: bool, unborn: bool, prefixes):
    lines = []
    head_oid = _head_oid(repo)
    refs = _all_refs(repo)
    # HEAD
    include_head = not prefixes or any("HEAD".startswith(p) for p in prefixes)
    if include_head:
        if head_oid == ZERO:
            if unborn:
                line = "unborn HEAD"
                if symrefs:
                    line += " symref-target:refs/heads/" + repo.default_branch
                lines.append(line)
        else:
            line = f"{head_oid} HEAD"
            if symrefs:
                line += " symref-target:refs/heads/" + repo.default_branch
            lines.append(line)
    for ref, oid in refs:
        if prefixes and not any(ref.startswith(p) for p in prefixes):
            continue
        line = f"{oid} {ref}"
        if peel:
            peeled = _peeled_oid(repo, ref, oid)
            if peeled is not None:
                line += f" peeled:{peeled}"
        lines.append(line)
    return b"".join(_pkt((line + "\n").encode("ascii")) for line in lines) + _flush()


def _handle_ls_refs(request, repo):
    try:
        items = _parse_pkt_stream(request.body)
    except ValueError:
        raise ForgeError(400, "malformed pkt-line stream")
    peel = symrefs = unborn = False
    prefixes = []
    seen_delim = False
    for kind, payload in items:
        if kind == "delim":
            seen_delim = True
            continue
        if kind != "data":
            continue
        text = payload.decode("utf-8", "replace").strip()
        if not seen_delim:
            continue  # command=/agent=/object-format= lines
        if text == "peel":
            peel = True
        elif text == "symrefs":
            symrefs = True
        elif text == "unborn":
            unborn = True
        elif text.startswith("ref-prefix "):
            prefixes.append(text[len("ref-prefix "):])
    return Response(200, _ls_refs(repo, peel, symrefs, unborn, prefixes),
                    {"Content-Type": UPLOAD_RESULT_CT})


# ------------------------------------------------------------------ fetch

def _sideband_pack(pack: bytes) -> bytes:
    """Pack bytes as sideband band-1 pkt-lines (chunked <= 65515 payload bytes)."""
    out = b""
    for i in range(0, len(pack), SIDEBAND_CHUNK):
        chunk = pack[i:i + SIDEBAND_CHUNK]
        out += _pkt(b"\x01" + chunk)
    if not pack:
        # a packfile section must carry >= 1 sideband packet; an empty pack is
        # 32 bytes so this branch is unreachable, but keep it safe
        out += _pkt(b"\x01")
    return out


def _select_fetch_objects(repo, wants, haves, client_shallow, depth):
    """(objects_to_send, shallow_list, unshallow_list).

    objects: closure(wants, depth) - closure(haves, stop at client-shallow)
             - closure(client-shallow that stay shallow).
    shallow: boundary commits of this fetch (depth-limited, parents not included).
    unshallow: client-shallow commits that are no longer shallow after this fetch.
    """
    objects = repo.objects
    # -- included commits: walk from each want, at most `depth` generations
    included = {}  # oid -> generation (want = 1)
    stack = [(w, 1) for w in wants]
    while stack:
        oid, gen = stack.pop()
        if oid in included or oid == ZERO:
            continue
        entry = objects.get(oid)
        if entry is None:
            continue
        if entry[0] == "tag":
            headers, _m = gitobj.parse_tag(entry[1])
            target = headers.get("object", [b""])[0].decode("ascii")
            included[oid] = gen
            if target and target not in included:
                stack.append((target, gen))
            continue
        if entry[0] != "commit":
            included[oid] = gen
            continue
        included[oid] = gen
        if depth is not None and gen >= depth:
            continue
        headers, _m = gitobj.parse_commit(entry[1])
        for parent in headers.get("parent", []):
            stack.append((parent.decode("ascii"), gen + 1))
    # -- boundary commits: included commits with a parent not included
    shallow = []
    for oid in included:
        entry = objects.get(oid)
        if entry is None or entry[0] != "commit":
            continue
        headers, _m = gitobj.parse_commit(entry[1])
        parents = [p.decode("ascii") for p in headers.get("parent", [])]
        if parents and any(p not in included for p in parents):
            shallow.append(oid)
    if depth is not None:
        for oid, gen in included.items():
            if gen == depth and oid not in shallow:
                entry = objects.get(oid)
                if entry is not None and entry[0] == "commit":
                    shallow.append(oid)

    def collect(tips, follow):
        """Closure of tips: trees/blobs fully, tag targets always, commit
        parents only when follow(commit_oid) is true."""
        seen = set()
        stack = list(tips)
        while stack:
            oid = stack.pop()
            if oid in seen or oid == ZERO:
                continue
            entry = objects.get(oid)
            if entry is None:
                continue
            seen.add(oid)
            obj_type, payload = entry
            if obj_type == "commit":
                headers, _m = gitobj.parse_commit(payload)
                tree = headers.get("tree", [b""])[0].decode("ascii")
                if tree:
                    stack.append(tree)
                if follow(oid):
                    for parent in headers.get("parent", []):
                        stack.append(parent.decode("ascii"))
            elif obj_type == "tree":
                for _mode, _name, child in gitobj.parse_tree(payload):
                    stack.append(child)
            elif obj_type == "tag":
                headers, _m = gitobj.parse_tag(payload)
                target = headers.get("object", [b""])[0].decode("ascii")
                if target:
                    stack.append(target)
        return seen

    # Follow parents only from commits strictly inside the depth limit:
    # a boundary commit (gen == depth) contributes its tree but not its
    # parents, or a depth-1 clone would carry the parent commit too.
    want_reach = collect(
        list(included),
        lambda oid: oid in included and (depth is None or included[oid] < depth))
    have_reach = collect(haves, lambda oid: oid not in client_shallow)
    # a client-shallow commit stays shallow unless this fetch now includes its
    # parents (it is included and no longer a boundary)
    staying = [oid for oid in client_shallow
               if oid not in included or oid in shallow]
    stay_reach = collect(staying, lambda oid: False) if staying else set()
    send = want_reach - have_reach - stay_reach
    unshallow = [oid for oid in client_shallow
                 if oid in included and oid not in shallow]
    return send, sorted(set(shallow)), unshallow


_PACK_CACHE_MAX = 128  # distinct object sets kept; FIFO eviction
_pack_cache: dict[tuple[int, tuple[str, ...]], bytes] = {}


def _build_fetch_pack(repo, send_oids):
    """A pack v2 of the given objects, deterministically ordered.

    Cached by (repo id, sorted object set): a pack is a pure function of its
    object set (objects are immutable once stored), so concurrent clones of
    the same tip share one build instead of each re-running zlib under the GIL.
    """
    key = (repo.id, tuple(sorted(send_oids)))
    pack = _pack_cache.get(key)
    if pack is not None:
        return pack
    items = []
    for oid in key[1]:
        entry = repo.objects.get(oid)
        if entry is None:
            continue
        items.append(entry)
    pack = gitobj.build_pack(items)
    if len(_pack_cache) >= _PACK_CACHE_MAX:
        _pack_cache.pop(next(iter(_pack_cache)))
    _pack_cache[key] = pack
    return pack


def _fetch_response_v2(repo, wants, haves, client_shallow, depth):
    send, shallow, unshallow = _select_fetch_objects(
        repo, wants, haves, client_shallow, depth)
    out = _pkt(b"acknowledgments\n")
    acked = False
    for have in haves:
        if have in repo.objects:
            out += _pkt(f"ACK {have}\n".encode("ascii"))
            acked = True
    if not acked:
        out += _pkt(b"NAK\n")
    out += _delim()
    if shallow or unshallow:
        out += _pkt(b"shallow-info\n")
        for oid in shallow:
            out += _pkt(f"shallow {oid}\n".encode("ascii"))
        for oid in unshallow:
            out += _pkt(f"unshallow {oid}\n".encode("ascii"))
        out += _delim()
    out += _pkt(b"packfile\n")
    out += _sideband_pack(_build_fetch_pack(repo, send))
    out += _flush()
    return Response(200, out, {"Content-Type": UPLOAD_RESULT_CT})


def _fetch_response_v0(repo, wants, haves, client_shallow, depth):
    send, shallow, unshallow = _select_fetch_objects(
        repo, wants, haves, client_shallow, depth)
    out = b""
    for oid in shallow:
        out += _pkt(f"shallow {oid}\n".encode("ascii"))
    for oid in unshallow:
        out += _pkt(f"unshallow {oid}\n".encode("ascii"))
    acked = False
    for have in haves:
        if have in repo.objects:
            out += _pkt(f"ACK {have}\n".encode("ascii"))
            acked = True
    if not acked:
        out += _pkt(b"NAK\n")
    out += _sideband_pack(_build_fetch_pack(repo, send))
    out += _flush()
    return Response(200, out, {"Content-Type": UPLOAD_RESULT_CT})


def _handle_v2_fetch(request, repo):
    try:
        items = _parse_pkt_stream(request.body)
    except ValueError:
        raise ForgeError(400, "malformed pkt-line stream")
    wants, haves, shallows = [], [], []
    depth = None
    seen_delim = False
    for kind, payload in items:
        if kind == "delim":
            seen_delim = True
            continue
        if kind != "data":
            continue
        text = payload.decode("utf-8", "replace").strip()
        if not seen_delim:
            continue
        if text.startswith("want "):
            wants.append(text[5:].strip())
        elif text.startswith("have "):
            haves.append(text[5:].strip())
        elif text.startswith("shallow "):
            shallows.append(text[8:].strip())
        elif text.startswith("deepen "):
            try:
                depth = int(text[7:].strip())
            except ValueError:
                pass
    if not wants:
        return Response(200, _pkt(b"acknowledgments\n") + _pkt(b"NAK\n") + _delim()
                        + _pkt(b"packfile\n")
                        + _sideband_pack(_build_fetch_pack(repo, set()))
                        + _flush(), {"Content-Type": UPLOAD_RESULT_CT})
    return _fetch_response_v2(repo, wants, haves, shallows, depth)


def _handle_v0_fetch(request, repo):
    try:
        items = _parse_pkt_stream(request.body)
    except ValueError:
        raise ForgeError(400, "malformed pkt-line stream")
    wants, haves, shallows = [], [], []
    depth = None
    for kind, payload in items:
        if kind != "data":
            continue
        text = payload.decode("utf-8", "replace").strip()
        if text.startswith("want "):
            # first want line may carry capabilities after the oid
            rest = text[5:].strip()
            wants.append(rest.split(" ")[0])
        elif text.startswith("have "):
            haves.append(text[5:].strip())
        elif text.startswith("shallow "):
            shallows.append(text[8:].strip())
        elif text.startswith("deepen "):
            try:
                depth = int(text[7:].strip())
            except ValueError:
                pass
    if not wants:
        return _fetch_response_v0(repo, [], [], [], None)
    return _fetch_response_v0(repo, wants, haves, shallows, depth)


def upload_pack(request, owner, repo_name):
    repo = request.store.repos.get((owner, repo_name))
    if repo is None:
        return Response(404, b'{"message":"Repository not found"}',
                        {"Content-Type": "application/json"})
    is_v2 = (request.headers.get("git-protocol", "") == "version=2")
    if is_v2:
        # command=ls-refs or command=fetch (or garbage -> 400, server survives)
        try:
            if len(request.body) < 4:
                raise ForgeError(400, "malformed pkt-line stream")
            text = request.body.decode("utf-8", "replace")
            if "command=ls-refs" in text[:200]:
                return _handle_ls_refs(request, repo)
            if "command=fetch" in text[:200]:
                return _handle_v2_fetch(request, repo)
        except ForgeError:
            raise
        except Exception:
            # Malformed input must never kill the server, but a bug here
            # must not masquerade as "unknown v2 command" either.
            import sys
            import traceback
            traceback.print_exc(file=sys.stderr)
            raise ForgeError(400, "malformed upload-pack request")
        raise ForgeError(400, "unknown v2 command")
    return _handle_v0_fetch(request, repo)


# ------------------------------------------------------------------ receive-pack

def _parse_push_commands(body: bytes):
    """Parse the command pkt-lines up to the flush; return (commands, pack_bytes).

    commands: list of (old, new, ref). Raises ValueError on garbage."""
    commands = []
    pos = 0
    n = len(body)
    while pos < n:
        if pos + 4 > n:
            raise ValueError("truncated pkt-line")
        size = int(body[pos:pos + 4], 16)
        if size == 0:
            pos += 4
            break
        if size in (1, 2):
            pos += 4
            continue
        if size < 4 or pos + size > n:
            raise ValueError("bad pkt-line length")
        line = body[pos + 4:pos + size].decode("utf-8", "replace")
        pos += size
        line = line.rstrip("\n")
        # split off NUL capabilities (first line only, but tolerate anywhere)
        if "\0" in line:
            line = line.split("\0", 1)[0]
        parts = line.split(" ")
        if len(parts) != 3:
            raise ValueError("bad push command line")
        old, new, ref = parts
        if len(old) != 40 or len(new) != 40:
            raise ValueError("bad push command oid")
        commands.append((old, new, ref))
    else:
        raise ValueError("push command list has no flush")
    return commands, body[pos:]


def _validate_closure(repo, new_objects, new_tip):
    """Every object reachable from new_tip must be in store ∪ new_objects."""
    merged = dict(repo.objects)
    merged.update(new_objects)
    if new_tip not in merged:
        return False
    # every object reachable from new_tip must be in store ∪ new_objects
    stack = [new_tip]
    checked = set()
    while stack:
        oid = stack.pop()
        if oid in checked or oid == ZERO:
            continue
        checked.add(oid)
        entry = merged.get(oid)
        if entry is None:
            return False
        obj_type, payload = entry
        if obj_type == "commit":
            headers, _m = gitobj.parse_commit(payload)
            tree = headers.get("tree", [b""])[0].decode("ascii")
            if tree:
                stack.append(tree)
            for parent in headers.get("parent", []):
                stack.append(parent.decode("ascii"))
        elif obj_type == "tree":
            for _mode, _name, child in gitobj.parse_tree(payload):
                stack.append(child)
        elif obj_type == "tag":
            headers, _m = gitobj.parse_tag(payload)
            target = headers.get("object", [b""])[0].decode("ascii")
            if target:
                stack.append(target)
    return True


def _report_sideband(report_lines: list[bytes]) -> bytes:
    """Report-status stream inside band-1 sideband packets + flush."""
    stream = b"".join(_pkt(line) for line in report_lines)
    out = b""
    for i in range(0, len(stream), SIDEBAND_CHUNK):
        out += _pkt(b"\x01" + stream[i:i + SIDEBAND_CHUNK])
    out += _flush()
    return out


def receive_pack(request, owner, repo_name):
    repo = request.store.repos.get((owner, repo_name))
    if repo is None:
        return Response(404, b'{"message":"Repository not found"}',
                        {"Content-Type": "application/json"})
    try:
        commands, pack_bytes = _parse_push_commands(request.body)
    except ValueError:
        return Response(400, b"bad request", {"Content-Type": "text/plain"})
    if not commands:
        return Response(400, b"bad request", {"Content-Type": "text/plain"})
    # -- parse + validate the pack (if any bytes follow the command list)
    new_objects = {}
    if pack_bytes:
        try:
            entries = gitobj.parse_pack(pack_bytes, store=repo.objects)
        except ValueError as err:
            report = _report_sideband([f"unpack {err}\n".encode("ascii")])
            return Response(200, report, {"Content-Type": RECEIVE_RESULT_CT})
        for oid, obj_type, payload in entries:
            new_objects[oid] = (obj_type, payload)
        # closure check for every new tip
        for _old, new, _ref in commands:
            if new != ZERO and not _validate_closure(repo, new_objects, new):
                report = _report_sideband([b"unpack missing objects in pack\n"])
                return Response(200, report, {"Content-Type": RECEIVE_RESULT_CT})
        repo.objects.update(new_objects)
    # -- per-ref rules
    report_lines = [b"unpack ok\n"]
    for old, new, ref in commands:
        current = repo.refs.get(ref)
        if new == ZERO:
            # delete
            if current is None:
                report_lines.append(f"ng {ref} not-found\n".encode("ascii"))
            elif old != ZERO and old != current:
                report_lines.append(f"ng {ref} non-fast-forward\n".encode("ascii"))
            else:
                del repo.refs[ref]
                report_lines.append(f"ok {ref}\n".encode("ascii"))
        elif old == ZERO:
            # create
            if current is not None:
                report_lines.append(f"ng {ref} already-exists\n".encode("ascii"))
            else:
                repo.refs[ref] = new
                report_lines.append(f"ok {ref}\n".encode("ascii"))
        else:
            # update
            if current is None:
                report_lines.append(f"ng {ref} not-found\n".encode("ascii"))
            elif old != current:
                report_lines.append(f"ng {ref} non-fast-forward\n".encode("ascii"))
            elif old == new:
                report_lines.append(f"ok {ref}\n".encode("ascii"))
            elif _is_ancestor(repo, old, new):
                repo.refs[ref] = new
                report_lines.append(f"ok {ref}\n".encode("ascii"))
            else:
                report_lines.append(f"ng {ref} non-fast-forward\n".encode("ascii"))
    return Response(200, _report_sideband(report_lines),
                    {"Content-Type": RECEIVE_RESULT_CT})


def register(router):
    def mutating(handler):
        def wrapped(request, **kwargs):
            with request.store.lock:
                return handler(request, **kwargs)
        return wrapped

    router.add("GET", "/(?P<owner>[^/]+)/(?P<repo_name>[^/]+)\\.git/info/refs", info_refs)
    router.add("POST", "/(?P<owner>[^/]+)/(?P<repo_name>[^/]+)\\.git/git-upload-pack", upload_pack)
    router.add("POST", "/(?P<owner>[^/]+)/(?P<repo_name>[^/]+)\\.git/git-receive-pack", mutating(receive_pack))
