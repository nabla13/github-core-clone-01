"""GraphQL v4 endpoint (POST /api/graphql) — see specs/graphql-spec.md.

A small recursive-descent parser + executor for exactly the documents the
suite sends (gh / github-mcp-server shapes): queries, mutations, variables,
inline enums/objects, aliases, inline fragments, __type introspection, and
batched operations (a JSON array -> an array of responses, in order).

Global ids are opaque and stable: base64 of "Repository:<dbid>",
"Issue:<repo_id>:<number>", "PullRequest:<repo_id>:<number>",
"IssueComment:<comment_id>" — never the plain number.
"""
from __future__ import annotations

import base64
import json

from .httpd import check_auth, json_response
from .store import ForgeError, public_issue, public_pull

# ------------------------------------------------------------------ global ids

def _encode_gid(kind: str, *parts) -> str:
    raw = kind + ":" + ":".join(str(p) for p in parts)
    return base64.urlsafe_b64encode(raw.encode("ascii")).decode("ascii")


def _decode_gid(gid: str):
    try:
        raw = base64.urlsafe_b64decode(gid.encode("ascii")).decode("ascii")
    except Exception:
        return None
    parts = raw.split(":")
    if len(parts) < 2:
        return None
    return parts[0], parts[1:]


def _repo_gid(repo) -> str:
    return _encode_gid("Repository", repo.id)


def _issue_gid(repo, issue) -> str:
    return _encode_gid("Issue", repo.id, issue["number"])


def _pr_gid(repo, pr) -> str:
    return _encode_gid("PullRequest", repo.id, pr["number"])


def _comment_gid(comment) -> str:
    return _encode_gid("IssueComment", comment["id"])


# ------------------------------------------------------------------ lexer

class _Tok:
    __slots__ = ("kind", "value")

    def __init__(self, kind, value):
        self.kind = kind
        self.value = value


def _tokenize(text: str):
    toks = []
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch in " \t\r\n,":
            i += 1
        elif ch == "#":
            while i < n and text[i] != "\n":
                i += 1
        elif ch in "{}()[]:!=$@&|":
            toks.append(_Tok(ch, ch))
            i += 1
        elif ch == "." and text[i:i + 3] == "...":
            toks.append(_Tok("...", "..."))
            i += 3
        elif ch == '"':
            i += 1
            out = []
            while i < n and text[i] != '"':
                if text[i] == "\\" and i + 1 < n:
                    esc = text[i + 1]
                    out.append({"n": "\n", "t": "\t", "r": "\r", '"': '"',
                                "\\": "\\", "/": "/"}.get(esc, esc))
                    i += 2
                else:
                    out.append(text[i])
                    i += 1
            i += 1
            toks.append(_Tok("string", "".join(out)))
        elif ch == "-" or ch.isdigit():
            j = i
            if text[j] == "-":
                j += 1
            while j < n and (text[j].isdigit() or text[j] in ".eE+-"):
                j += 1
            raw = text[i:j]
            try:
                value = int(raw)
            except ValueError:
                value = float(raw)
            toks.append(_Tok("number", value))
            i = j
        elif ch.isalpha() or ch == "_":
            j = i
            while j < n and (text[j].isalnum() or text[j] == "_"):
                j += 1
            toks.append(_Tok("name", text[i:j]))
            i = j
        else:
            raise ValueError(f"unexpected character {ch!r}")
    toks.append(_Tok("eof", None))
    return toks


# ------------------------------------------------------------------ parser

class _Parser:
    def __init__(self, text: str):
        self.toks = _tokenize(text)
        self.pos = 0

    def peek(self):
        return self.toks[self.pos]

    def next(self):
        tok = self.toks[self.pos]
        self.pos += 1
        return tok

    def expect(self, kind):
        tok = self.next()
        if tok.kind != kind:
            raise ValueError(f"expected {kind}, got {tok.kind} {tok.value!r}")
        return tok

    def skip(self, kind):
        if self.peek().kind == kind:
            self.pos += 1
            return True
        return False

    def parse_document(self):
        operations = []
        fragments = {}
        while self.peek().kind != "eof":
            if self.peek().kind == "{":
                operations.append({"kind": "query", "name": None,
                                   "variables": {}, "selection": self.parse_selection()})
            elif self.peek().kind == "name" and self.peek().value in ("query", "mutation"):
                op_kind = self.next().value
                name = None
                if self.peek().kind == "name":
                    name = self.next().value
                variables = self.parse_var_defs() if self.peek().kind == "(" else {}
                operations.append({"kind": op_kind, "name": name,
                                   "variables": variables,
                                   "selection": self.parse_selection()})
            elif self.peek().kind == "name" and self.peek().value == "fragment":
                self.next()
                frag_name = self.expect("name").value
                self.expect("name")  # 'on'
                type_cond = self.expect("name").value
                fragments[frag_name] = {"type": type_cond,
                                        "selection": self.parse_selection()}
            else:
                raise ValueError(f"unexpected token {self.peek().kind} {self.peek().value!r}")
        return operations, fragments

    def parse_var_defs(self):
        self.expect("(")
        defs = {}
        while self.peek().kind != ")":
            self.expect("$")
            name = self.expect("name").value
            self.expect(":")
            defs[name] = self.parse_type()
            if self.peek().kind == "=":
                self.next()
                self.parse_value()
            if not self.skip(","):
                pass
        self.expect(")")
        return defs

    def parse_type(self):
        if self.skip("["):
            inner = self.parse_type()
            self.expect("]")
            typ = "[" + inner + "]"
        else:
            typ = self.expect("name").value
        if self.skip("!"):
            typ += "!"
        return typ

    def parse_selection(self):
        self.expect("{")
        fields = []
        while self.peek().kind != "}":
            if self.peek().kind == "...":
                self.next()
                if self.peek().kind == "name" and self.peek().value == "on":
                    self.next()
                    type_cond = self.expect("name").value
                else:
                    type_cond = None
                fields.append({"fragment": True, "type": type_cond,
                               "selection": self.parse_selection()})
            else:
                name = self.expect("name").value
                alias = None
                if self.peek().kind == ":":
                    self.next()
                    alias = name
                    name = self.expect("name").value
                args = {}
                if self.peek().kind == "(":
                    args = self.parse_args()
                # directives (none in the suite's docs; skip if present)
                while self.peek().kind == "@":
                    self.next()
                    self.expect("name")
                    if self.peek().kind == "(":
                        self.parse_args()
                selection = self.parse_selection() if self.peek().kind == "{" else None
                fields.append({"field": name, "alias": alias or name,
                               "args": args, "selection": selection})
        self.expect("}")
        return fields

    def parse_args(self):
        self.expect("(")
        args = {}
        while self.peek().kind != ")":
            name = self.expect("name").value
            self.expect(":")
            args[name] = self.parse_value()
        self.expect(")")
        return args

    def parse_value(self):
        tok = self.next()
        if tok.kind == "$":
            return ("var", self.expect("name").value)
        if tok.kind == "string":
            return tok.value
        if tok.kind == "number":
            return tok.value
        if tok.kind == "name":
            if tok.value == "true":
                return True
            if tok.value == "false":
                return False
            if tok.value == "null":
                return None
            return ("enum", tok.value)
        if tok.kind == "[":
            values = []
            while self.peek().kind != "]":
                values.append(self.parse_value())
            self.expect("]")
            return values
        if tok.kind == "{":
            obj = {}
            while self.peek().kind != "}":
                name = self.expect("name").value
                self.expect(":")
                obj[name] = self.parse_value()
            self.expect("}")
            return obj
        raise ValueError(f"unexpected value token {tok.kind}")


def _resolve_value(value, variables):
    if isinstance(value, tuple) and value[0] == "var":
        return variables.get(value[1])
    if isinstance(value, tuple) and value[0] == "enum":
        return value[1]
    if isinstance(value, list):
        return [_resolve_value(v, variables) for v in value]
    if isinstance(value, dict):
        return {k: _resolve_value(v, variables) for k, v in value.items()}
    return value


# ------------------------------------------------------------------ schema

FIELD_SETS = {
    "Query": {"viewer", "repository", "node", "__type"},
    "Mutation": {"createIssue", "closeIssue", "createPullRequest",
                 "mergePullRequest", "addComment"},
    "User": {"login"},
    "Repository": {"id", "databaseId", "name", "nameWithOwner", "owner",
                   "defaultBranchRef", "viewerPermission", "hasIssuesEnabled",
                   "issue", "issues", "pullRequests", "pullRequest"},
    "Issue": {"id", "number", "title", "state", "body"},
    "IssueConnection": {"nodes", "pageInfo"},
    "PageInfo": {"hasNextPage", "endCursor", "hasPreviousPage", "startCursor"},
    "PullRequest": {"id", "number", "title", "body", "state", "merged",
                    "headRefName", "baseRefName", "mergeable", "files"},
    "PullRequestConnection": {"nodes", "pageInfo"},
    "PullRequestFileConnection": {"nodes"},
    "PullRequestFile": {"path"},
    "IssueComment": {"id", "body"},
    "Ref": {"name"},
    "AddCommentPayload": {"commentEdge"},
    "AddCommentEdge": {"node"},
    "CreateIssuePayload": {"issue"},
    "CloseIssuePayload": {"issue"},
    "CreatePullRequestPayload": {"pullRequest"},
    "MergePullRequestPayload": {"pullRequest"},
    "__Type": {"name", "fields", "enumValues"},
    "__Field": {"name"},
    "__EnumValue": {"name"},
}

INTROSPECTION_FIELDS = {
    "Issue": ["id", "number", "title", "state", "body"],
    "PullRequest": ["id", "number", "headRefName", "baseRefName", "mergeable",
                    "state", "merged", "files", "title", "body"],
    "Repository": ["id", "databaseId", "name", "nameWithOwner", "owner",
                   "defaultBranchRef", "viewerPermission", "hasIssuesEnabled",
                   "issue", "issues", "pullRequests", "pullRequest"],
}
INTROSPECTION_ENUMS = {
    "SearchType": ["ISSUE", "PULL_REQUEST", "REPOSITORY", "USER", "DISCUSSION",
                   "COMMIT", "MILESTONE", "WIKI", "PACKAGE"],
}


class _Val:
    """A typed value: (typename, python value). Scalars carry typename None."""

    __slots__ = ("typename", "value")

    def __init__(self, typename, value):
        self.typename = typename
        self.value = value


class Executor:
    def __init__(self, store):
        self.store = store
        self.errors = []

    # ------------------------------------------------------------ entry
    def execute(self, operation, variables):
        self.errors = []
        root_type = "Query" if operation["kind"] == "query" else "Mutation"
        data = self._resolve_selection(root_type, None, operation["selection"], variables)
        result = {"data": data}
        if self.errors:
            result["errors"] = [{"message": m} for m in self.errors]
        return result

    # ------------------------------------------------------------ resolution
    def _resolve_selection(self, typename, parent, selection, variables):
        out = {}
        allowed = FIELD_SETS.get(typename, set())
        for item in selection:
            if "fragment" in item:
                if item["type"] is not None and item["type"] != typename:
                    continue
                sub = self._resolve_selection(typename, parent, item["selection"], variables)
                out.update(sub)
                continue
            name = item["field"]
            if name not in allowed:
                self.errors.append(
                    f'Cannot query field "{name}" on type "{typename}".')
                continue
            args = {k: _resolve_value(v, variables) for k, v in item["args"].items()}
            value = self._resolve_field(typename, parent, name, args)
            if item["selection"] is not None:
                value = self._resolve_sub(value, item["selection"], variables)
            out[item["alias"]] = value
        return out

    def _resolve_sub(self, value, selection, variables):
        if value is None:
            return None
        if isinstance(value, list):
            return [self._resolve_sub(v, selection, variables) for v in value]
        if isinstance(value, _Val):
            return self._resolve_selection(value.typename, value.value, selection, variables)
        return value

    # ------------------------------------------------------------ fields
    def _resolve_field(self, typename, parent, name, args):
        store = self.store
        if typename == "Query":
            if name == "viewer":
                return _Val("User", {"login": store.token_owner})
            if name == "repository":
                repo = store.repos.get((args.get("owner"), args.get("name")))
                return _Val("Repository", repo) if repo is not None else None
            if name == "node":
                decoded = _decode_gid(args.get("id") or "")
                if decoded and decoded[0] == "Repository":
                    try:
                        repo_id = int(decoded[1][0])
                    except (ValueError, IndexError):
                        return None
                    repo = store.repos_by_id.get(repo_id)
                    return _Val("Repository", repo) if repo is not None else None
                return None
            if name == "__type":
                type_name = args.get("name")
                return _Val("__Type", {"name": type_name})
        if typename == "User":
            return parent.get(name)
        if typename == "Mutation":
            return self._mutation(name, args)
        if typename == "Repository":
            repo = parent
            if name == "id":
                return _repo_gid(repo)
            if name == "databaseId":
                return repo.id
            if name == "name":
                return repo.name
            if name == "nameWithOwner":
                return repo.full_name
            if name == "owner":
                return _Val("User", {"login": repo.owner})
            if name == "defaultBranchRef":
                tip = repo.refs.get(f"refs/heads/{repo.default_branch}")
                if tip is None:
                    return None
                return _Val("Ref", {"name": repo.default_branch})
            if name == "viewerPermission":
                return "ADMIN"
            if name == "hasIssuesEnabled":
                return True
            if name == "issue":
                number = args.get("number")
                for issue in repo.issues:
                    if issue["number"] == number:
                        return _Val("Issue", (repo, issue))
                return None
            if name == "issues":
                return _Val("IssueConnection", self._issues_connection(repo, args))
            if name == "pullRequests":
                return _Val("PullRequestConnection", self._prs_connection(repo, args))
            if name == "pullRequest":
                number = args.get("number")
                for pr in repo.pulls:
                    if pr["number"] == number:
                        return _Val("PullRequest", (repo, pr))
                return None
        if typename == "Issue":
            repo, issue = parent
            if name == "id":
                return _issue_gid(repo, issue)
            if name == "number":
                return issue["number"]
            if name == "title":
                return issue["title"]
            if name == "state":
                return issue["state"].upper()
            if name == "body":
                return issue["body"]
        if typename == "IssueConnection":
            conn = parent
            if name == "nodes":
                return [_Val("Issue", (conn["repo"], i)) for i in conn["nodes"]]
            if name == "pageInfo":
                return _Val("PageInfo", conn["pageInfo"])
        if typename == "PageInfo":
            return parent.get(name)
        if typename == "PullRequest":
            repo, pr = parent
            if name == "id":
                return _pr_gid(repo, pr)
            if name == "number":
                return pr["number"]
            if name == "title":
                return pr["title"]
            if name == "body":
                return pr["body"]
            if name == "state":
                return "MERGED" if pr["merged"] else pr["state"].upper()
            if name == "merged":
                return bool(pr["merged"])
            if name == "headRefName":
                return pr["head"]["ref"]
            if name == "baseRefName":
                return pr["base"]["ref"]
            if name == "mergeable":
                return self._mergeable(repo, pr)
            if name == "files":
                first = args.get("first") or 10
                files = self.store.pull_files(repo, pr)[:first]
                return _Val("PullRequestFileConnection",
                            {"nodes": [{"path": f["filename"]} for f in files]})
        if typename == "PullRequestConnection":
            conn = parent
            if name == "nodes":
                return [_Val("PullRequest", (conn["repo"], p)) for p in conn["nodes"]]
            if name == "pageInfo":
                return _Val("PageInfo", {"hasNextPage": False, "endCursor": None,
                                         "hasPreviousPage": False, "startCursor": None})
        if typename == "PullRequestFileConnection":
            if name == "nodes":
                return [_Val("PullRequestFile", n) for n in parent["nodes"]]
        if typename == "PullRequestFile":
            return parent.get(name)
        if typename == "Ref":
            return parent.get(name)
        if typename == "IssueComment":
            if name == "id":
                return _comment_gid(parent)
            return parent.get(name)
        if typename in ("AddCommentPayload", "CreateIssuePayload",
                        "CloseIssuePayload", "CreatePullRequestPayload",
                        "MergePullRequestPayload"):
            return parent.get(name)
        if typename == "AddCommentEdge":
            return parent.get(name)
        if typename == "__Type":
            type_name = parent["name"]
            if name == "name":
                return type_name
            if name == "fields":
                fields = INTROSPECTION_FIELDS.get(type_name, [])
                return [_Val("__Field", {"name": f}) for f in fields]
            if name == "enumValues":
                values = INTROSPECTION_ENUMS.get(type_name, [])
                return [_Val("__EnumValue", {"name": v}) for v in values]
        if typename == "__Field":
            return parent.get(name)
        if typename == "__EnumValue":
            return parent.get(name)
        return None

    # ------------------------------------------------------------ helpers
    def _issues_connection(self, repo, args):
        issues = list(repo.issues)
        states = args.get("states")
        if states:
            wanted = {s.upper() for s in states}
            issues = [i for i in issues if i["state"].upper() in wanted]
        order_by = args.get("orderBy") or {}
        field = (order_by.get("field") or "CREATED_AT").upper()
        direction = (order_by.get("direction") or "DESC").upper()
        if field == "CREATED_AT":
            issues.sort(key=lambda i: i["created_seq"], reverse=(direction == "DESC"))
        first = args.get("first")
        after = args.get("after")
        if after:
            decoded = _decode_gid(after)
            if decoded and decoded[0] == "IssueCursor":
                try:
                    seq = int(decoded[1][0])
                except (ValueError, IndexError):
                    seq = None
                if seq is not None:
                    idx = next((k for k, i in enumerate(issues)
                                if i["created_seq"] == seq), None)
                    if idx is not None:
                        issues = issues[idx + 1:]
        total = len(issues)
        if first is not None:
            page = issues[:first]
            has_next = total > first
        else:
            page = issues
            has_next = False
        end_cursor = None
        if page:
            end_cursor = _encode_gid("IssueCursor", page[-1]["created_seq"])
        return {"repo": repo, "nodes": page,
                "pageInfo": {"hasNextPage": has_next, "endCursor": end_cursor,
                             "hasPreviousPage": False, "startCursor": None}}

    def _prs_connection(self, repo, args):
        prs = list(repo.pulls)
        first = args.get("first")
        if first is not None:
            prs = prs[:first]
        return {"repo": repo, "nodes": prs,
                "pageInfo": {"hasNextPage": False, "endCursor": None,
                             "hasPreviousPage": False, "startCursor": None}}

    def _mergeable(self, repo, pr):
        if pr["state"] != "open":
            return "UNKNOWN"
        try:
            # reuse the conflict rule: a conflicting PR reports CONFLICTING
            base_ref = f"refs/heads/{pr['base']['ref']}"
            base_tip = repo.refs.get(base_ref)
            head_tip = repo.refs.get(f"refs/heads/{pr['head']['ref']}")
            if base_tip is None or head_tip is None:
                return "UNKNOWN"
            from . import gitobj
            merge_base = gitobj.merge_base(repo.objects, base_tip, head_tip)
            if merge_base is None:
                return "MERGEABLE"
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
            cur_files = gitobj.flatten_tree(
                repo.objects, headers["tree"][0].decode("ascii"))
            for path in set(head_files) | set(base_files):
                if (head_files.get(path) != base_files.get(path)
                        and cur_files.get(path) != base_files.get(path)
                        and (head_files.get(path) or (None, None))[1]
                        != (cur_files.get(path) or (None, None))[1]):
                    return "CONFLICTING"
            return "MERGEABLE"
        except Exception:
            return "UNKNOWN"

    # ------------------------------------------------------------ mutations
    def _mutation(self, name, args):
        store = self.store
        inp = args.get("input") or {}
        try:
            if name == "createIssue":
                decoded = _decode_gid(inp.get("repositoryId") or "")
                if not decoded or decoded[0] != "Repository":
                    raise ForgeError(422, "Unknown repository")
                repo = store.repos_by_id.get(int(decoded[1][0]))
                if repo is None:
                    raise ForgeError(422, "Unknown repository")
                issue = store.create_issue(repo, inp.get("title") or "",
                                           inp.get("body") or "", None)
                return _Val("CreateIssuePayload",
                            {"issue": _Val("Issue", (repo, issue))})
            if name == "closeIssue":
                decoded = _decode_gid(inp.get("issueId") or "")
                if not decoded or decoded[0] != "Issue":
                    raise ForgeError(422, "Unknown issue")
                repo = store.repos_by_id.get(int(decoded[1][0]))
                issue = None
                if repo is not None:
                    for i in repo.issues:
                        if i["number"] == int(decoded[1][1]):
                            issue = i
                            break
                if issue is None:
                    raise ForgeError(422, "Unknown issue")
                store.patch_issue(repo, issue["number"], "closed")
                return _Val("CloseIssuePayload",
                            {"issue": _Val("Issue", (repo, issue))})
            if name == "createPullRequest":
                decoded = _decode_gid(inp.get("repositoryId") or "")
                if not decoded or decoded[0] != "Repository":
                    raise ForgeError(422, "Unknown repository")
                repo = store.repos_by_id.get(int(decoded[1][0]))
                if repo is None:
                    raise ForgeError(422, "Unknown repository")
                pr = store.create_pull(repo, inp.get("title") or "",
                                       inp.get("body") or "",
                                       inp.get("headRefName") or "",
                                       inp.get("baseRefName") or "")
                return _Val("CreatePullRequestPayload",
                            {"pullRequest": _Val("PullRequest", (repo, pr))})
            if name == "mergePullRequest":
                decoded = _decode_gid(inp.get("pullRequestId") or "")
                if not decoded or decoded[0] != "PullRequest":
                    raise ForgeError(422, "Unknown pull request")
                repo = store.repos_by_id.get(int(decoded[1][0]))
                pr = None
                if repo is not None:
                    for p in repo.pulls:
                        if p["number"] == int(decoded[1][1]):
                            pr = p
                            break
                if pr is None:
                    raise ForgeError(422, "Unknown pull request")
                store.merge_pull(repo, pr, inp.get("mergeMethod") or "SQUASH")
                return _Val("MergePullRequestPayload",
                            {"pullRequest": _Val("PullRequest", (repo, pr))})
            if name == "addComment":
                decoded = _decode_gid(inp.get("subjectId") or "")
                if not decoded or decoded[0] != "Issue":
                    raise ForgeError(422, "Unknown subject")
                repo = store.repos_by_id.get(int(decoded[1][0]))
                issue = None
                if repo is not None:
                    for i in repo.issues:
                        if i["number"] == int(decoded[1][1]):
                            issue = i
                            break
                if issue is None:
                    raise ForgeError(422, "Unknown subject")
                comment = store.create_comment(repo, issue["number"],
                                               inp.get("body") or "")
                return _Val("AddCommentPayload",
                            {"commentEdge": _Val(
                                "AddCommentEdge",
                                {"node": _Val("IssueComment", comment)})})
        except ForgeError as err:
            self.errors.append(err.message)
            return None
        return None


# ------------------------------------------------------------------ endpoint

def _execute_one(store, body):
    """Execute one GraphQL operation object -> response dict."""
    query = body.get("query") or ""
    variables = body.get("variables") or {}
    if not isinstance(variables, dict):
        variables = {}
    try:
        operations, _fragments = _Parser(query).parse_document()
    except ValueError as err:
        return {"data": None, "errors": [{"message": f"GraphQL parse error: {err}"}]}
    if not operations:
        return {"data": None, "errors": [{"message": "No operation in document"}]}
    operation = operations[0]
    # operationName: the suite never sends one; if present, select by name
    op_name = body.get("operationName")
    if op_name:
        for op in operations:
            if op["name"] == op_name:
                operation = op
                break
    executor = Executor(store)
    return executor.execute(operation, variables)


def graphql_endpoint(request, token):
    check_auth(request, token)
    value = request.json_any()
    if value is None:
        return json_response(request.store, 400,
                             {"errors": [{"message": "Request body must be JSON"}]})
    with request.store.lock:
        if isinstance(value, list):
            results = [_execute_one(request.store, item if isinstance(item, dict) else {})
                       for item in value]
            return json_response(request.store, 200, results)
        if isinstance(value, dict):
            return json_response(request.store, 200, _execute_one(request.store, value))
    return json_response(request.store, 400,
                         {"errors": [{"message": "Request body must be an object or array"}]})


def register(router, token):
    router.add("POST", "/api/graphql", lambda request: graphql_endpoint(request, token))
