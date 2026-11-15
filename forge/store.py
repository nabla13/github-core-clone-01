"""The forge's in-memory store: repos, git objects, refs, issues, PRs, labels.

One process, one store, one RLock. Everything deterministic: timestamps come
from a tick counter seeded at SCENARIO_EPOCH_SEC, ids from counters, request
ids from a counter — so two runs on the same request sequence produce
byte-identical state (the determinism check replays a trial and compares the
state.json export).

The store starts EMPTY. The scenario (wire.ts) is used only for identity
(the token names the viewer) and for deterministic names/content the checks
send; no scenario repo is pre-seeded — every check creates what it needs.
"""
from __future__ import annotations

import base64
import difflib
import threading
from datetime import datetime, timezone

from . import gitobj
from .scenario import SCENARIO_EPOCH_SEC, build_scenario

ZERO = gitobj.ZERO_OID


def _iso(epoch: int) -> str:
    dt = datetime.fromtimestamp(epoch, tz=timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def _git_ident(login: str, epoch: int) -> str:
    return f"{login} <{login}@example.invalid> {epoch} +0000"


class ForgeError(Exception):
    """An error that maps to an HTTP status + {"message": ...} body."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


class Repo:
    __slots__ = (
        "id", "owner", "name", "description", "private", "default_branch",
        "objects", "refs", "labels", "issues", "pulls", "next_issue_number",
        "next_pull_number", "created_seq",
    )

    def __init__(self, repo_id: int, owner: str, name: str, private: bool,
                 description: str, default_branch: str, created_seq: int):
        self.id = repo_id
        self.owner = owner
        self.name = name
        self.description = description
        self.private = private
        self.default_branch = default_branch
        self.objects = {}          # oid -> (type, payload)
        self.refs = {}             # refname -> oid
        self.labels = {}           # name -> color
        self.issues = []           # list[dict]
        self.pulls = []            # list[dict]
        self.next_issue_number = 1
        self.next_pull_number = 1
        self.created_seq = created_seq

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.name}"

    def head_ref(self) -> str:
        return f"refs/heads/{self.default_branch}"

    def head_oid(self) -> str:
        return self.refs.get(self.head_ref(), ZERO)

    def public_json(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "full_name": self.full_name,
            "owner": {"login": self.owner, "id": 1000},
            "private": self.private,
            "description": self.description,
            "default_branch": self.default_branch,
        }


class Store:
    def __init__(self, token_owner: str):
        self.lock = threading.RLock()
        self.scenario = build_scenario()
        self.token_owner = token_owner  # login the bearer token names
        self.repos = {}                # (owner, name) -> Repo
        self.repos_by_id = {}          # id -> Repo
        self._next_repo_id = 1
        self._next_global_id = 1       # stable numeric ids for issues/comments
        self._tick = 0                 # deterministic clock
        self._req_counter = 0
        self._seq = 0                  # creation ordering counter

    # ------------------------------------------------------------ determinism
    def tick(self) -> int:
        """Next deterministic epoch second (SCENARIO_EPOCH_SEC + tick)."""
        self._tick += 1
        return SCENARIO_EPOCH_SEC + self._tick

    def next_seq(self) -> int:
        self._seq += 1
        return self._seq

    def next_request_id(self) -> str:
        self._req_counter += 1
        return f"req-{self._req_counter:08d}"

    def next_global_id(self) -> int:
        value = self._next_global_id
        self._next_global_id += 1
        return value

    def now_iso(self) -> str:
        return _iso(self.tick())

    # ------------------------------------------------------------ repos
    def create_repo(self, name: str, private: bool, description: str):
        owner = self.token_owner
        if not name or not isinstance(name, str):
            raise ForgeError(422, "name is required")
        key = (owner, name)
        if key in self.repos:
            raise ForgeError(422, "Repository already exists")
        repo = Repo(self._next_repo_id, owner, name, bool(private),
                    description or "", "main", self.next_seq())
        self._next_repo_id += 1
        self.repos[key] = repo
        self.repos_by_id[repo.id] = repo
        return repo

    def get_repo(self, owner: str, name: str):
        repo = self.repos.get((owner, name))
        if repo is None:
            raise ForgeError(404, "Repository not found")
        return repo

    def delete_repo(self, owner: str, name: str):
        repo = self.get_repo(owner, name)
        del self.repos[(owner, name)]
        del self.repos_by_id[repo.id]

    def list_repos(self, owner: str):
        """All repos owned by `owner`, in deterministic insertion order."""
        return [r for r in self.repos.values() if r.owner == owner]

    # ------------------------------------------------------------ git objects
    def put_object(self, repo: Repo, obj_type: str, payload: bytes) -> str:
        oid = gitobj.oid_of(obj_type, payload)
        repo.objects.setdefault(oid, (obj_type, payload))
        return oid

    def get_object(self, repo: Repo, oid: str):
        return repo.objects.get(oid)

    # ------------------------------------------------------------ refs/branches
    def create_ref(self, repo: Repo, ref: str, sha: str):
        if not ref.startswith("refs/heads/"):
            raise ForgeError(422, "only refs/heads/* refs can be created")
        if ref in repo.refs:
            raise ForgeError(422, "Reference already exists")
        if sha not in repo.objects:
            raise ForgeError(422, "Object does not exist")
        repo.refs[ref] = sha
        return {"ref": ref, "object": {"sha": sha}}

    def list_branches(self, repo: Repo):
        out = []
        for ref in sorted(repo.refs):
            if ref.startswith("refs/heads/"):
                out.append({"name": ref[len("refs/heads/"):],
                            "commit": {"sha": repo.refs[ref]}})
        return out

    def branch_tip(self, repo: Repo, branch: str):
        return repo.refs.get(f"refs/heads/{branch}")

    # ------------------------------------------------------------ commits
    def commit_history(self, repo: Repo, tip: str):
        return gitobj.commit_history(repo.objects, tip)

    def list_commits(self, repo: Repo, ref: str | None):
        if ref is None:
            tip = repo.head_oid()
        else:
            tip = repo.refs.get(f"refs/heads/{ref}")
            if tip is None:
                # ref may be a full sha or another ref name
                tip = ref if ref in repo.objects else None
            if tip is None:
                raise ForgeError(404, "No such ref")
        if tip == ZERO:
            return []
        out = []
        for oid in self.commit_history(repo, tip):
            _t, payload = repo.objects[oid]
            headers, message = gitobj.parse_commit(payload)
            out.append({"sha": oid, "commit": {"message": message.decode("utf-8", "replace")}})
        return out

    def make_commit(self, repo: Repo, files: dict, message: str, branch: str,
                    parent: str | None):
        """Create a commit on `branch` whose tree is the parent tree with
        `files` applied ({path: bytes} or {path: None} to delete).

        Returns (commit_oid, tree_oid). Does not move the ref (caller does).
        """
        epoch = self.tick()
        ident = _git_ident(self.token_owner, epoch)
        parent_tree = {}
        if parent and parent != ZERO:
            _t, payload = repo.objects[parent]
            headers, _m = gitobj.parse_commit(payload)
            tree_oid = headers["tree"][0].decode("ascii")
            parent_tree = gitobj.flatten_tree(repo.objects, tree_oid)
        new_files = dict(parent_tree)
        for path, content in files.items():
            if content is None:
                new_files.pop(path, None)
            else:
                blob_oid = self.put_object(repo, "blob", content)
                new_files[path] = (b"100644", blob_oid)
        tree_oid, new_objs = gitobj.build_tree_from_files(repo.objects, new_files)
        for obj_type, payload in new_objs:
            self.put_object(repo, obj_type, payload)
        parents = [] if not parent or parent == ZERO else [parent]
        commit_oid, raw = gitobj.build_commit(
            tree_oid, parents, ident, ident, message.encode("utf-8"))
        _t, payload = gitobj.parse_raw(raw)
        self.put_object(repo, "commit", payload)
        return commit_oid, tree_oid

    # ------------------------------------------------------------ contents
    def put_contents(self, repo: Repo, path: str, message: str, content_b64: str,
                     branch: str | None, sha: str | None):
        try:
            content = base64.b64decode(content_b64, validate=False)
        except Exception:
            raise ForgeError(400, "content is not valid base64")
        branch = branch or repo.default_branch
        ref = f"refs/heads/{branch}"
        tip = repo.refs.get(ref, ZERO)
        existing = None
        if tip != ZERO:
            _t, payload = repo.objects[tip]
            headers, _m = gitobj.parse_commit(payload)
            tree_oid = headers["tree"][0].decode("ascii")
            flat = gitobj.flatten_tree(repo.objects, tree_oid)
            existing = flat.get(path)
        if existing is None:
            if sha is not None:
                raise ForgeError(422, "file does not exist")
            status = 201
        else:
            if sha is not None and sha.lower() != existing[1].lower():
                raise ForgeError(409, "sha does not match the current file")
            status = 200
        commit_oid, _tree = self.make_commit(
            repo, {path: content}, message, branch, tip if tip != ZERO else None)
        repo.refs[ref] = commit_oid
        blob_oid = gitobj.oid_of("blob", content)
        return status, {
            "content": {"sha": blob_oid, "encoding": "base64"},
            "commit": {"sha": commit_oid},
        }

    def get_contents(self, repo: Repo, path: str, ref: str | None):
        tip = repo.head_oid()
        if ref is not None:
            tip = repo.refs.get(f"refs/heads/{ref}")
            if tip is None:
                tip = ref if ref in repo.objects else None
            if tip is None:
                raise ForgeError(404, "No such ref")
        if tip == ZERO:
            raise ForgeError(404, "Not found")
        _t, payload = repo.objects[tip]
        headers, _m = gitobj.parse_commit(payload)
        tree_oid = headers["tree"][0].decode("ascii")
        flat = gitobj.flatten_tree(repo.objects, tree_oid)
        # exact file?
        if path in flat:
            mode, blob_oid = flat[path]
            _bt, blob = repo.objects[blob_oid]
            return {
                "type": "file",
                "encoding": "base64",
                "content": base64.b64encode(blob).decode("ascii"),
                "sha": blob_oid,
                "size": len(blob),
                "name": path.rsplit("/", 1)[-1],
                "path": path,
            }
        # directory listing?
        prefix = path.rstrip("/") + "/"
        children = {}
        for p, (mode, oid) in flat.items():
            if p.startswith(prefix):
                rest = p[len(prefix):]
                first = rest.split("/", 1)[0]
                if "/" in rest:
                    children[first] = {"type": "dir", "name": first,
                                       "path": prefix + first, "sha": oid}
                else:
                    children[first] = {"type": "file", "name": first,
                                       "path": p, "sha": oid}
        if children:
            return [children[k] for k in sorted(children)]
        raise ForgeError(404, "Not found")

    def delete_contents(self, repo: Repo, path: str, message: str,
                        branch: str | None, sha: str | None):
        branch = branch or repo.default_branch
        ref = f"refs/heads/{branch}"
        tip = repo.refs.get(ref, ZERO)
        if tip == ZERO:
            raise ForgeError(404, "Not found")
        _t, payload = repo.objects[tip]
        headers, _m = gitobj.parse_commit(payload)
        tree_oid = headers["tree"][0].decode("ascii")
        flat = gitobj.flatten_tree(repo.objects, tree_oid)
        existing = flat.get(path)
        if existing is None:
            raise ForgeError(404, "Not found")
        if sha is not None and sha.lower() != existing[1].lower():
            raise ForgeError(409, "sha does not match the current file")
        commit_oid, _tree = self.make_commit(
            repo, {path: None}, message, branch, tip)
        repo.refs[ref] = commit_oid
        return {"commit": {"sha": commit_oid}}

    # ------------------------------------------------------------ labels
    def create_label(self, repo: Repo, name: str, color: str):
        repo.labels[name] = color or ""
        return {"name": name, "color": repo.labels[name]}

    # ------------------------------------------------------------ issues
    def create_issue(self, repo: Repo, title: str, body: str, labels):
        labels = labels or []
        for name in labels:
            if name not in repo.labels:
                raise ForgeError(422, f"Label does not exist: {name}")
        issue = {
            "id": self.next_global_id(),
            "number": repo.next_issue_number,
            "title": title,
            "body": body or "",
            "state": "open",
            "labels": [{"name": n} for n in labels],
            "created_seq": self.next_seq(),
            "created_at": self.now_iso(),
            "comments": [],
        }
        repo.next_issue_number += 1
        repo.issues.append(issue)
        return issue

    def list_issues(self, repo: Repo, state: str | None = None,
                    sort: str | None = None, direction: str | None = None):
        issues = list(repo.issues)
        if state is None:
            issues = [i for i in issues if i["state"] == "open"]
        elif state != "all":
            issues = [i for i in issues if i["state"] == state]
        if (sort or "created") == "created":
            reverse = (direction or "desc") == "desc"
            issues.sort(key=lambda i: i["created_seq"], reverse=reverse)
        return issues

    def get_issue(self, repo: Repo, number: int):
        for issue in repo.issues:
            if issue["number"] == number:
                return issue
        raise ForgeError(404, "Issue not found")

    def patch_issue(self, repo: Repo, number: int, state: str | None):
        issue = self.get_issue(repo, number)
        if state is not None:
            if state not in ("open", "closed"):
                raise ForgeError(422, "Invalid state")
            issue["state"] = state
        return issue

    def create_comment(self, repo: Repo, number: int, body: str):
        issue = self.get_issue(repo, number)
        comment = {"id": self.next_global_id(), "body": body or ""}
        issue["comments"].append(comment)
        return comment

    # ------------------------------------------------------------ pulls
    def create_pull(self, repo: Repo, title: str, body: str, head: str, base: str):
        head_tip = repo.refs.get(f"refs/heads/{head}")
        if head_tip is None:
            raise ForgeError(422, "Head branch does not exist")
        base_tip = repo.refs.get(f"refs/heads/{base}")
        if base_tip is None:
            raise ForgeError(422, "Base branch does not exist")
        for pr in repo.pulls:
            if pr["head"]["ref"] == head and pr["state"] == "open":
                raise ForgeError(422, "A pull request already exists for this head")
        pr = {
            "id": self.next_global_id(),
            "number": repo.next_pull_number,
            "title": title,
            "body": body or "",
            "state": "open",
            "head": {"ref": head, "sha": head_tip},
            "base": {"ref": base, "sha": base_tip},
            "merged": False,
            "merged_at": None,
            "merge_commit_sha": None,
            "created_seq": self.next_seq(),
        }
        repo.next_pull_number += 1
        repo.pulls.append(pr)
        return pr

    def get_pull(self, repo: Repo, number: int):
        for pr in repo.pulls:
            if pr["number"] == number:
                return pr
        raise ForgeError(404, "Pull request not found")

    def list_pulls(self, repo: Repo, state: str | None):
        if state is None or state == "open":
            return [p for p in repo.pulls if p["state"] == "open"]
        if state == "all":
            return list(repo.pulls)
        if state == "closed":
            return [p for p in repo.pulls if p["state"] == "closed"]
        raise ForgeError(422, "Invalid state")

    def pull_files(self, repo: Repo, pr: dict):
        """Diff merge-base -> head: [{filename, status, additions, deletions, changes}]."""
        base_tip = pr["base"]["sha"]
        head_tip = pr["head"]["sha"]
        merge_base = gitobj.merge_base(repo.objects, base_tip, head_tip)
        if merge_base is None:
            base_files = {}
        else:
            _t, payload = repo.objects[merge_base]
            headers, _m = gitobj.parse_commit(payload)
            base_files = gitobj.flatten_tree(
                repo.objects, headers["tree"][0].decode("ascii"))
        _t, payload = repo.objects[head_tip]
        headers, _m = gitobj.parse_commit(payload)
        head_files = gitobj.flatten_tree(
            repo.objects, headers["tree"][0].decode("ascii"))
        out = []
        for path in sorted(set(base_files) | set(head_files)):
            b = base_files.get(path)
            h = head_files.get(path)
            if b is not None and h is not None and b[1] == h[1]:
                continue
            if b is None:
                status = "added"
                old = b""
                _bt, new = repo.objects[h[1]]
            elif h is None:
                status = "removed"
                _bt, old = repo.objects[b[1]]
                new = b""
            else:
                status = "modified"
                _bt, old = repo.objects[b[1]]
                _bt, new = repo.objects[h[1]]
            old_lines = old.decode("utf-8", "replace").splitlines()
            new_lines = new.decode("utf-8", "replace").splitlines()
            sm = difflib.SequenceMatcher(a=old_lines, b=new_lines, autojunk=False)
            additions = deletions = 0
            for tag, _i1, _i2, j1, j2 in sm.get_opcodes():
                if tag in ("insert", "replace"):
                    additions += j2 - j1
                if tag in ("delete", "replace"):
                    deletions += _i2 - _i1
            out.append({
                "filename": path,
                "status": status,
                "additions": additions,
                "deletions": deletions,
                "changes": additions + deletions,
            })
        return out

    def merge_pull(self, repo: Repo, pr: dict, merge_method: str | None):
        if pr["state"] != "open":
            raise ForgeError(405, "Pull request is already closed")
        method = (merge_method or "squash").lower()
        if method != "squash":
            raise ForgeError(422, "Only squash merge is supported")
        base_ref = f"refs/heads/{pr['base']['ref']}"
        base_tip = repo.refs.get(base_ref)
        if base_tip is None:
            raise ForgeError(404, "Base branch not found")
        head_tip = repo.refs.get(f"refs/heads/{pr['head']['ref']}")
        if head_tip is None:
            raise ForgeError(422, "Head branch no longer exists")
        # conflict: a file changed head-vs-merge-base AND changed base-vs-merge-base
        # with different content.
        merge_base = gitobj.merge_base(repo.objects, base_tip, head_tip)
        if merge_base is None:
            base_files = {}
        else:
            _t, payload = repo.objects[merge_base]
            headers, _m = gitobj.parse_commit(payload)
            base_files = gitobj.flatten_tree(
                repo.objects, headers["tree"][0].decode("ascii"))
        _t, payload = repo.objects[head_tip]
        headers, _m = gitobj.parse_commit(payload)
        head_files = gitobj.flatten_tree(
            repo.objects, headers["tree"][0].decode("ascii"))
        _t, payload = repo.objects[base_tip]
        headers, _m = gitobj.parse_commit(payload)
        cur_base_files = gitobj.flatten_tree(
            repo.objects, headers["tree"][0].decode("ascii"))
        for path in set(head_files) | set(base_files):
            head_changed = head_files.get(path) != base_files.get(path)
            base_changed = cur_base_files.get(path) != base_files.get(path)
            if head_changed and base_changed:
                head_blob = head_files.get(path)
                base_blob = cur_base_files.get(path)
                if (head_blob or (None, None))[1] != (base_blob or (None, None))[1]:
                    raise ForgeError(405, "Merge conflict")
        # squash: one commit on base, parent = base tip, tree = base tree + head changes
        changes = {}
        for path, (mode, oid) in head_files.items():
            if base_files.get(path) != (mode, oid):
                _bt, blob = repo.objects[oid]
                changes[path] = blob
        for path in base_files:
            if path not in head_files:
                changes[path] = None
        message = f"Merge pull request #{pr['number']} from {pr['head']['ref']}"
        commit_oid, _tree = self.make_commit(
            repo, changes, message, pr["base"]["ref"], base_tip)
        repo.refs[base_ref] = commit_oid
        pr["state"] = "closed"
        pr["merged"] = True
        pr["merged_at"] = self.now_iso()
        pr["merge_commit_sha"] = commit_oid
        return {"sha": commit_oid, "merged": True,
                "message": f"Pull request #{pr['number']} merged"}


def public_issue(issue: dict) -> dict:
    return {
        "id": issue["id"],
        "number": issue["number"],
        "title": issue["title"],
        "body": issue["body"],
        "state": issue["state"],
        "labels": [{"name": l["name"]} for l in issue["labels"]],
    }


def public_pull(pr: dict) -> dict:
    out = {
        "id": pr["id"],
        "number": pr["number"],
        "title": pr["title"],
        "body": pr["body"],
        "state": pr["state"],
        "head": dict(pr["head"]),
        "base": dict(pr["base"]),
    }
    if pr["merged"]:
        out["merged"] = True
        out["merged_at"] = pr["merged_at"]
        out["merge_commit_sha"] = pr["merge_commit_sha"]
    else:
        out["merged"] = False
    return out
