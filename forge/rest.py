"""REST v3 handlers (/api/v3) — see specs/rest-spec.md for the exact contract."""
from __future__ import annotations

import json

from .httpd import check_auth, json_response
from .store import ForgeError, public_issue, public_pull

API = "/api/v3"


def _int_param(request, name, default):
    raw = request.query_one(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default



def _num(value: str) -> int:
    """Parse a path number; non-numeric input is a 404, never a 500."""
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ForgeError(404, "Not Found")


def _repo_json(request, repo):
    return json_response(request.store, 200, repo.public_json())


def _paginate(request, items, total_hint=None):
    """Slice items by ?per_page=&page= and build a GitHub-style Link header."""
    per_page = _int_param(request, "per_page", 30)
    page = _int_param(request, "page", 1)
    start = (page - 1) * per_page
    page_items = items[start:start + per_page]
    total = len(items)
    last_page = max(1, (total + per_page - 1) // per_page)
    origin = f"http://{request.host}" if request.host else ""
    links = []
    if page < last_page:
        links.append(f'<{origin}{request.path}?per_page={per_page}&page={page + 1}>; rel="next"')
    if last_page > 1:
        links.append(f'<{origin}{request.path}?per_page={per_page}&page={last_page}>; rel="last"')
    headers = {}
    if links:
        headers["Link"] = ", ".join(links)
    return page_items, headers


def register(router, token):
    def auth(request):
        check_auth(request, token)

    def mutating(handler):
        """Mutating handlers hold the store lock; reads stay lock-free so
        the concurrent-clone benchmark is not serialized."""
        def wrapped(request, **kwargs):
            with request.store.lock:
                return handler(request, **kwargs)
        return wrapped

    # ------------------------------------------------------------ root / meta
    def root(request):
        auth(request)
        return json_response(request.store, 200,
                             {"current_user_url": f"{API}/user",
                              "rate_limit_url": f"{API}/rate_limit"},
                             {"X-Oauth-Scopes": "repo, read:org"})

    def meta(request):
        auth(request)
        return json_response(request.store, 200, {"installed_version": "1.0.0"})

    def get_user(request):
        auth(request)
        return json_response(request.store, 200,
                             {"login": request.store.token_owner, "id": 1000})

    # ------------------------------------------------------------ repos
    def create_repo(request):
        auth(request)
        body = request.json()
        repo = request.store.create_repo(body.get("name") or "",
                                         body.get("private", False),
                                         body.get("description") or "")
        return json_response(request.store, 201, repo.public_json())

    def list_user_repos(request):
        auth(request)
        repos = request.store.list_repos(request.store.token_owner)
        page, headers = _paginate(request, repos)
        return json_response(request.store, 200,
                             [r.public_json() for r in page], headers)

    def get_repo(request, owner, repo_name):
        auth(request)
        repo = request.store.get_repo(owner, repo_name)
        return json_response(request.store, 200, repo.public_json())

    def patch_repo(request, owner, repo_name):
        auth(request)
        body = request.json()
        repo = request.store.get_repo(owner, repo_name)
        if "description" in body:
            repo.description = body["description"] or ""
        return json_response(request.store, 200, repo.public_json())

    def delete_repo(request, owner, repo_name):
        auth(request)
        request.store.delete_repo(owner, repo_name)
        from .httpd import Response
        return Response(204, b"",
                        {"X-GitHub-Request-Id": request.store.next_request_id()})

    # ------------------------------------------------------------ refs/branches
    def create_ref(request, owner, repo_name):
        auth(request)
        body = request.json()
        repo = request.store.get_repo(owner, repo_name)
        ref = body.get("ref") or ""
        sha = body.get("sha") or ""
        result = request.store.create_ref(repo, ref, sha)
        return json_response(request.store, 201, result)

    def get_ref(request, owner, repo_name, branch):
        auth(request)
        repo = request.store.get_repo(owner, repo_name)
        ref = f"refs/heads/{branch}"
        if ref not in repo.refs:
            raise ForgeError(404, "Reference not found")
        return json_response(request.store, 200,
                             {"ref": ref, "object": {"sha": repo.refs[ref]}})

    def list_branches(request, owner, repo_name):
        auth(request)
        repo = request.store.get_repo(owner, repo_name)
        return json_response(request.store, 200, request.store.list_branches(repo))

    def get_branch(request, owner, repo_name, branch):
        auth(request)
        repo = request.store.get_repo(owner, repo_name)
        tip = request.store.branch_tip(repo, branch)
        if tip is None:
            raise ForgeError(404, "Branch not found")
        return json_response(request.store, 200,
                             {"name": branch, "commit": {"sha": tip}})

    def list_commits(request, owner, repo_name):
        auth(request)
        repo = request.store.get_repo(owner, repo_name)
        ref = request.query_one("sha")
        commits = request.store.list_commits(repo, ref)
        return json_response(request.store, 200, commits)

    # ------------------------------------------------------------ contents
    def put_contents(request, owner, repo_name, path):
        auth(request)
        body = request.json()
        repo = request.store.get_repo(owner, repo_name)
        status, result = request.store.put_contents(
            repo, path, body.get("message") or "", body.get("content") or "",
            body.get("branch"), body.get("sha"))
        return json_response(request.store, status, result)

    def get_contents(request, owner, repo_name, path):
        auth(request)
        repo = request.store.get_repo(owner, repo_name)
        result = request.store.get_contents(repo, path, request.query_one("ref"))
        return json_response(request.store, 200, result)

    def delete_contents(request, owner, repo_name, path):
        auth(request)
        body = request.json() if request.body else {}
        repo = request.store.get_repo(owner, repo_name)
        result = request.store.delete_contents(
            repo, path, body.get("message") or "", body.get("branch"), body.get("sha"))
        return json_response(request.store, 200, result)

    # ------------------------------------------------------------ labels
    def create_label(request, owner, repo_name):
        auth(request)
        body = request.json()
        repo = request.store.get_repo(owner, repo_name)
        result = request.store.create_label(repo, body.get("name") or "",
                                            body.get("color") or "")
        return json_response(request.store, 201, result)

    # ------------------------------------------------------------ issues
    def create_issue(request, owner, repo_name):
        auth(request)
        body = request.json()
        repo = request.store.get_repo(owner, repo_name)
        issue = request.store.create_issue(repo, body.get("title") or "",
                                           body.get("body") or "",
                                           body.get("labels"))
        return json_response(request.store, 201, public_issue(issue))

    def list_issues(request, owner, repo_name):
        auth(request)
        repo = request.store.get_repo(owner, repo_name)
        issues = request.store.list_issues(
            repo, request.query_one("state"), request.query_one("sort"),
            request.query_one("direction"))
        per_page = _int_param(request, "per_page", 30)
        page = _int_param(request, "page", 1)
        start = (page - 1) * per_page
        page_items = issues[start:start + per_page]
        return json_response(request.store, 200, [public_issue(i) for i in page_items])

    def get_issue(request, owner, repo_name, number):
        auth(request)
        repo = request.store.get_repo(owner, repo_name)
        issue = request.store.get_issue(repo, _num(number))
        # fast path: cached serialized bytes for the hot read workload
        cached = issue.get("_cached")
        if cached is not None:
            from .httpd import Response
            return Response(200, cached,
                            {"Content-Type": "application/json",
                             "X-GitHub-Request-Id": request.store.next_request_id()})
        body = json.dumps(public_issue(issue), separators=(",", ":"),
                          ensure_ascii=False).encode("utf-8")
        issue["_cached"] = body
        from .httpd import Response
        return Response(200, body,
                        {"Content-Type": "application/json",
                         "X-GitHub-Request-Id": request.store.next_request_id()})

    def patch_issue(request, owner, repo_name, number):
        auth(request)
        body = request.json()
        repo = request.store.get_repo(owner, repo_name)
        issue = request.store.patch_issue(repo, _num(number), body.get("state"))
        issue.pop("_cached", None)
        return json_response(request.store, 200, public_issue(issue))

    def create_comment(request, owner, repo_name, number):
        auth(request)
        body = request.json()
        repo = request.store.get_repo(owner, repo_name)
        comment = request.store.create_comment(repo, _num(number), body.get("body") or "")
        return json_response(request.store, 201,
                             {"id": comment["id"], "body": comment["body"]})

    def list_comments(request, owner, repo_name, number):
        auth(request)
        repo = request.store.get_repo(owner, repo_name)
        issue = request.store.get_issue(repo, _num(number))
        return json_response(request.store, 200,
                             [{"id": c["id"], "body": c["body"]} for c in issue["comments"]])

    # ------------------------------------------------------------ pulls
    def create_pull(request, owner, repo_name):
        auth(request)
        body = request.json()
        repo = request.store.get_repo(owner, repo_name)
        pr = request.store.create_pull(repo, body.get("title") or "",
                                       body.get("body") or "",
                                       body.get("head") or "",
                                       body.get("base") or "")
        return json_response(request.store, 201, public_pull(pr))

    def list_pulls(request, owner, repo_name):
        auth(request)
        repo = request.store.get_repo(owner, repo_name)
        pulls = request.store.list_pulls(repo, request.query_one("state"))
        per_page = _int_param(request, "per_page", 30)
        page = _int_param(request, "page", 1)
        start = (page - 1) * per_page
        page_items = pulls[start:start + per_page]
        return json_response(request.store, 200, [public_pull(p) for p in page_items])

    def get_pull(request, owner, repo_name, number):
        auth(request)
        repo = request.store.get_repo(owner, repo_name)
        pr = request.store.get_pull(repo, _num(number))
        return json_response(request.store, 200, public_pull(pr))

    def list_pull_files(request, owner, repo_name, number):
        auth(request)
        repo = request.store.get_repo(owner, repo_name)
        pr = request.store.get_pull(repo, _num(number))
        return json_response(request.store, 200, request.store.pull_files(repo, pr))

    def merge_pull(request, owner, repo_name, number):
        auth(request)
        body = request.json() if request.body else {}
        repo = request.store.get_repo(owner, repo_name)
        pr = request.store.get_pull(repo, _num(number))
        result = request.store.merge_pull(repo, pr, body.get("merge_method"))
        return json_response(request.store, 200, result)

    # ------------------------------------------------------------ routes
    r = router
    r.add("GET", API + "/", root)
    r.add("GET", API, root)
    r.add("GET", API + "/meta", meta)
    r.add("GET", API + "/user", get_user)
    r.add("POST", API + "/user/repos", mutating(create_repo))
    r.add("GET", API + "/user/repos", list_user_repos)
    r.add("GET", API + "/repos/(?P<owner>[^/]+)/(?P<repo_name>[^/]+)", get_repo)
    r.add("PATCH", API + "/repos/(?P<owner>[^/]+)/(?P<repo_name>[^/]+)", mutating(patch_repo))
    r.add("DELETE", API + "/repos/(?P<owner>[^/]+)/(?P<repo_name>[^/]+)", mutating(delete_repo))
    r.add("POST", API + "/repos/(?P<owner>[^/]+)/(?P<repo_name>[^/]+)/git/refs", mutating(create_ref))
    r.add("GET", API + "/repos/(?P<owner>[^/]+)/(?P<repo_name>[^/]+)/git/refs/heads/(?P<branch>.+)", get_ref)
    r.add("GET", API + "/repos/(?P<owner>[^/]+)/(?P<repo_name>[^/]+)/branches", list_branches)
    r.add("GET", API + "/repos/(?P<owner>[^/]+)/(?P<repo_name>[^/]+)/branches/(?P<branch>.+)", get_branch)
    r.add("GET", API + "/repos/(?P<owner>[^/]+)/(?P<repo_name>[^/]+)/commits", list_commits)
    r.add("PUT", API + "/repos/(?P<owner>[^/]+)/(?P<repo_name>[^/]+)/contents/(?P<path>.+)", mutating(put_contents))
    r.add("GET", API + "/repos/(?P<owner>[^/]+)/(?P<repo_name>[^/]+)/contents/(?P<path>.+)", get_contents)
    r.add("DELETE", API + "/repos/(?P<owner>[^/]+)/(?P<repo_name>[^/]+)/contents/(?P<path>.+)", mutating(delete_contents))
    r.add("POST", API + "/repos/(?P<owner>[^/]+)/(?P<repo_name>[^/]+)/labels", mutating(create_label))
    r.add("POST", API + "/repos/(?P<owner>[^/]+)/(?P<repo_name>[^/]+)/issues", mutating(create_issue))
    r.add("GET", API + "/repos/(?P<owner>[^/]+)/(?P<repo_name>[^/]+)/issues", list_issues)
    r.add("GET", API + "/repos/(?P<owner>[^/]+)/(?P<repo_name>[^/]+)/issues/(?P<number>[0-9]+)", get_issue)
    r.add("PATCH", API + "/repos/(?P<owner>[^/]+)/(?P<repo_name>[^/]+)/issues/(?P<number>[0-9]+)", mutating(patch_issue))
    r.add("POST", API + "/repos/(?P<owner>[^/]+)/(?P<repo_name>[^/]+)/issues/(?P<number>[0-9]+)/comments", mutating(create_comment))
    r.add("GET", API + "/repos/(?P<owner>[^/]+)/(?P<repo_name>[^/]+)/issues/(?P<number>[0-9]+)/comments", list_comments)
    r.add("POST", API + "/repos/(?P<owner>[^/]+)/(?P<repo_name>[^/]+)/pulls", mutating(create_pull))
    r.add("GET", API + "/repos/(?P<owner>[^/]+)/(?P<repo_name>[^/]+)/pulls", list_pulls)
    r.add("GET", API + "/repos/(?P<owner>[^/]+)/(?P<repo_name>[^/]+)/pulls/(?P<number>[0-9]+)", get_pull)
    r.add("GET", API + "/repos/(?P<owner>[^/]+)/(?P<repo_name>[^/]+)/pulls/(?P<number>[0-9]+)/files", list_pull_files)
    r.add("PUT", API + "/repos/(?P<owner>[^/]+)/(?P<repo_name>[^/]+)/pulls/(?P<number>[0-9]+)/merge", mutating(merge_pull))
