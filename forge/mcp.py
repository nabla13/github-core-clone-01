"""MCP endpoint (POST /mcp) — Streamable HTTP, era 2026-07-28.

See specs/mcp-spec.md. Plain JSON responses (no SSE), no sessions.
Order of checks: body parse (400 / -32700) BEFORE auth (401), auth before
tool resolution. Notifications (no JSON-RPC id) -> HTTP 202, empty body.
"""
from __future__ import annotations

import json

from .httpd import check_auth, json_response
from .store import ForgeError, public_issue, public_pull

SERVER_INFO = {"name": "forge", "version": "1.0.0"}
PROTOCOL_VERSION = "2026-07-28"

TOOLS = [
    {
        "name": "create_repo",
        "description": "Create a repository in the forge.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Repository name"},
                "description": {"type": "string", "description": "Repository description"},
                "private": {"type": "boolean", "description": "Whether the repository is private"},
                "owner": {"type": "string", "description": "Owner login (defaults to the token owner)"},
            },
            "required": ["name"],
        },
    },
    {
        "name": "get_repo",
        "description": "Get a repository by owner and name.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "owner": {"type": "string", "description": "Owner login"},
                "repo": {"type": "string", "description": "Repository name"},
            },
            "required": ["owner", "repo"],
        },
    },
    {
        "name": "create_issue",
        "description": "Create an issue in a repository.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "owner": {"type": "string", "description": "Owner login"},
                "repo": {"type": "string", "description": "Repository name"},
                "title": {"type": "string", "description": "Issue title"},
                "body": {"type": "string", "description": "Issue body"},
            },
            "required": ["owner", "repo", "title"],
        },
    },
    {
        "name": "list_issues",
        "description": "List issues in a repository.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "owner": {"type": "string", "description": "Owner login"},
                "repo": {"type": "string", "description": "Repository name"},
                "state": {"type": "string", "description": "open, closed, or all"},
            },
            "required": ["owner", "repo"],
        },
    },
    {
        "name": "create_pull_request",
        "description": "Create a pull request.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "owner": {"type": "string", "description": "Owner login"},
                "repo": {"type": "string", "description": "Repository name"},
                "title": {"type": "string", "description": "Pull request title"},
                "head": {"type": "string", "description": "Head branch name"},
                "base": {"type": "string", "description": "Base branch name"},
                "body": {"type": "string", "description": "Pull request body"},
            },
            "required": ["owner", "repo", "title", "head", "base"],
        },
    },
    {
        "name": "merge_pull_request",
        "description": "Merge a pull request with the squash method.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "owner": {"type": "string", "description": "Owner login"},
                "repo": {"type": "string", "description": "Repository name"},
                "pull_number": {"type": "integer", "description": "Pull request number"},
            },
            "required": ["owner", "repo", "pull_number"],
        },
    },
]

_TOOL_BY_NAME = {tool["name"]: tool for tool in TOOLS}

_REQUIRED = {
    "create_repo": ["name"],
    "get_repo": ["owner", "repo"],
    "create_issue": ["owner", "repo", "title"],
    "list_issues": ["owner", "repo"],
    "create_pull_request": ["owner", "repo", "title", "head", "base"],
    "merge_pull_request": ["owner", "repo", "pull_number"],
}


def _rpc_result(request, rpc_id, result):
    return json_response(request.store, 200,
                         {"jsonrpc": "2.0", "id": rpc_id, "result": result})


def _rpc_error(request, rpc_id, code, message, http_status=200):
    return json_response(request.store, http_status,
                         {"jsonrpc": "2.0", "id": rpc_id,
                          "error": {"code": code, "message": message}})


def _tool_result(payload, is_error=False):
    result = {"content": [{"type": "text",
                           "text": json.dumps(payload, ensure_ascii=False)}],
              "structuredContent": payload}
    if is_error:
        result["isError"] = True
    return result


def _call_tool(store, name, args):
    """Run one tool against the store; returns the tool result payload dict."""
    if name not in _TOOL_BY_NAME:
        raise ForgeError(32602, f"Unknown tool: {name}")
    missing = [key for key in _REQUIRED[name]
               if args.get(key) is None or args.get(key) == ""]
    if missing:
        return _tool_result(
            {"error": f"Missing required argument: {', '.join(missing)}"},
            is_error=True)
    try:
        if name == "create_repo":
            # The forge has one owner: the token owner (the scenario viewer);
            # an explicit owner arg is accepted and ignored.
            repo = store.create_repo(
                args["name"], bool(args.get("private", False)),
                args.get("description") or "")
            return _tool_result(repo.public_json())
        if name == "get_repo":
            repo = store.get_repo(args["owner"], args["repo"])
            return _tool_result(repo.public_json())
        if name == "create_issue":
            repo = store.get_repo(args["owner"], args["repo"])
            issue = store.create_issue(repo, args["title"],
                                       args.get("body") or "", None)
            return _tool_result(public_issue(issue))
        if name == "list_issues":
            repo = store.get_repo(args["owner"], args["repo"])
            state = args.get("state") or "open"
            issues = store.list_issues(repo, state)
            return _tool_result([public_issue(i) for i in issues])
        if name == "create_pull_request":
            repo = store.get_repo(args["owner"], args["repo"])
            pr = store.create_pull(repo, args["title"], args.get("body") or "",
                                   args["head"], args["base"])
            return _tool_result(public_pull(pr))
        if name == "merge_pull_request":
            repo = store.get_repo(args["owner"], args["repo"])
            number = args["pull_number"]
            pr = next((p for p in repo.pulls if p["number"] == number), None)
            if pr is None:
                raise ForgeError(404, "Not Found")
            merged = store.merge_pull(repo, pr, "squash")
            return _tool_result({"sha": merged["sha"], "merged": True,
                                 "message": merged["message"]})
    except ForgeError as err:
        return _tool_result({"error": err.message}, is_error=True)
    raise ForgeError(32602, f"Unknown tool: {name}")


def mcp_endpoint(request, token):
    # 1. Body must parse before auth is checked.
    try:
        body = json.loads(request.body or b"")
    except Exception:
        return _rpc_error(request, None, -32700, "Parse error", http_status=400)
    # 2. Auth, before tool resolution (applies to every MCP request).
    check_auth(request, token)
    if not isinstance(body, dict):
        return _rpc_error(request, None, -32601, "Method not found")
    method = body.get("method")
    rpc_id = body.get("id")
    # Notifications carry no id -> HTTP 202, empty body.
    if rpc_id is None:
        return 202, b"", "text/plain"
    params = body.get("params") or {}
    if method == "server/discover":
        return _rpc_result(request, rpc_id, {
            "supportedVersions": ["2026-07-28"],
            "serverInfo": SERVER_INFO,
            "capabilities": {"tools": {}},
            "_meta": {"io.modelcontextprotocol/serverInfo": SERVER_INFO},
        })
    if method == "initialize":
        client_version = params.get("protocolVersion") or "2025-03-26"
        return _rpc_result(request, rpc_id, {
            "protocolVersion": client_version,
            "capabilities": {"tools": {}},
            "serverInfo": SERVER_INFO,
        })
    if method == "tools/list":
        return _rpc_result(request, rpc_id, {"tools": TOOLS})
    if method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") or {}
        if not isinstance(args, dict):
            args = {}
        if name not in _TOOL_BY_NAME:
            return _rpc_error(request, rpc_id, -32602, f"Unknown tool: {name}")
        with request.store.lock:
            result = _call_tool(request.store, name, args)
        return _rpc_result(request, rpc_id, result)
    return _rpc_error(request, rpc_id, -32601, f"Method not found: {method}")


def register(router, token):
    router.add("POST", "/mcp", lambda request: mcp_endpoint(request, token))
    router.add("DELETE", "/mcp", lambda request: json_response(
        request.store, 405, {"message": "Method not allowed"}))
