"""Deterministic seeded scenario — an exact port of env/src/wire.ts buildScenario().

Every stream is seeded from SCENARIO_SEED ^ fnv1a64(label) through the splitmix64
finalizer, and every generator consumes its stream in exactly the same order as the
TypeScript original, so this module produces byte-identical data to the kit's
buildScenario(). Verified against the kit (node --experimental-strip-types) output.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone

U64 = (1 << 64) - 1

SCENARIO_SEED = 0x6A09E667F3BCC909
SCENARIO_SEED_HEX = "6a09e667f3bcc909"
SCENARIO_EPOCH_SEC = 1735689600  # 2025-01-01T00:00:00Z

SCENARIO_USERS = 12
SCENARIO_VIEWER_REPOS = 30
SCENARIO_ORG_REPOS = 6
SCENARIO_STRANGER_REPOS = 3
SCENARIO_FILE_PATHS = 60
SCENARIO_BLOBS = 24
SCENARIO_COMMITS = 60
SCENARIO_ISSUES = 60
SCENARIO_PULLS = 30
DEFAULT_BRANCH = "main"

FIRST_NAMES = (
    "ada", "bram", "cora", "dmitri", "elena", "felix", "greta", "hugo", "iris", "jonas",
    "kira", "lars", "mira", "nico", "olga", "pavel", "quinn", "rosa", "sven", "tess",
    "ulla", "vito", "wren", "yara", "zane",
)
LAST_NAMES = (
    "anders", "bauer", "costa", "dahl", "eriksen", "fischer", "garcia", "holm", "iversen",
    "jansen", "keller", "lind", "moreau", "novak", "olsen", "peters", "quist", "rossi",
    "sato", "turi", "ulrich", "vega", "weber", "xu", "yilmaz", "zimmer",
)
REPO_ADJECTIVES = (
    "amber", "brisk", "calm", "crisp", "eager", "faint", "gentle", "happy", "idle", "jolly",
    "keen", "lively", "mellow", "nimble", "open", "plain", "quick", "quiet", "rapid", "soft",
    "steady", "tidy", "upbeat", "vivid", "warm", "witty", "young", "zesty",
)
REPO_NOUNS = (
    "acorn", "badger", "canyon", "cedar", "comet", "coral", "delta", "ember", "falcon",
    "glade", "harbor", "indigo", "juniper", "kettle", "lantern", "meadow", "nebula", "orchid",
    "pebble", "quartz", "raven", "saffron", "thistle", "umber", "violet", "willow", "yarrow",
    "zephyr",
)
REPO_DESCRIPTIONS = (
    "a small service with a sharp edge",
    "notes and experiments, kept until they are not",
    "the tooling behind the thing",
    "a reference implementation, for reading",
    "what the prototype became",
    "fixtures, generators and the odd benchmark",
    "the parts that must not break",
    "a sandbox for the next idea",
)
PATH_SEGMENTS = (
    "src", "lib", "docs", "test", "scripts", "api", "core", "util", "config", "data", "web",
    "cli", "pkg", "internal", "examples", "assets", "build", "tools", "server", "client",
)
FILE_STEMS = (
    "index", "request", "response", "server", "client", "schema", "parser", "router", "config",
    "store", "clock", "queue", "render", "format", "validate", "session", "token", "event",
    "stream", "cache",
)
FILE_EXTENSIONS = ("ts", "js", "json", "md", "txt", "yml", "toml", "css", "html", "sql")
BLOB_WORDS = (
    "alpha", "bravo", "charlie", "delta", "echo", "foxtrot", "golf", "hotel", "india",
    "juliet", "kilo", "lima", "mike", "november", "oscar", "papa", "quebec", "romeo",
    "sierra", "tango", "uniform", "victor", "whiskey", "xray", "yankee", "zulu",
    "amber", "basalt", "cobalt", "drift", "ember", "frost", "granite", "hollow", "ivory",
    "jasper", "kelp", "lichen", "moss", "nickel",
)
COMMIT_VERBS = (
    "Add", "Fix", "Refactor", "Document", "Test", "Tidy", "Speed up", "Simplify", "Handle",
    "Extract", "Inline", "Rename", "Guard", "Wire", "Drop",
)
COMMIT_OBJECTS = (
    "the request parser", "repo lookup", "issue pagination", "the merge path", "token checks",
    "the clone endpoint", "packfile handling", "error bodies", "branch listing",
    "the GraphQL node query", "MCP tool dispatch", "rate limit headers", "the upload pack",
    "the ref advertisement", "commit ancestry",
)
ISSUE_ADJECTIVES = (
    "Broken", "Slow", "Missing", "Confusing", "Unexpected", "Flaky", "Incorrect", "Unclear",
    "Stale", "Noisy",
)
ISSUE_SUBJECTS = (
    "clone over HTTP", "push report-status", "issue pagination", "merge button",
    "GraphQL node ids", "MCP tool list", "shallow fetch", "tag listing", "repo creation",
    "content update", "branch deletion", "commit listing",
)
PULL_VERBS = ("Add", "Fix", "Improve", "Simplify", "Document", "Refactor", "Speed up", "Harden")
PULL_SUBJECTS = (
    "shallow fetch support", "push report status checks", "issue label validation",
    "merge conflict detection", "GraphQL cursor pagination", "MCP tool schemas",
    "packfile delta reuse", "conditional requests", "repo pagination", "branch protection stubs",
)
SENTENCE_STARTS = (
    "Steps to reproduce are below", "This showed up after the last change",
    "The expected behaviour is clear", "A minimal case is attached",
    "It works on a fresh repository", "The same flow works over the API",
)
SENTENCE_ENDS = (
    "and the server answers with a JSON error", "but the second request fails",
    "and nothing is written to the log", "while the reply body stays empty",
    "and the status is wrong for the method", "so the client gives up",
)
ISSUE_LABELS = ("bug", "enhancement", "documentation", "question", "good first issue", "help wanted")
DEFAULT_BRANCHES = ("main", "develop", "release/1.0")
BRANCH_KINDS = ("feature", "fix", "perf", "docs", "chore")


def _fnv1a64(text: str) -> int:
    h = 0xCBF29CE484222325
    for ch in text:  # labels are ASCII; charCodeAt == ord
        h ^= ord(ch)
        h = (h * 0x100000001B3) & U64
    return h


def _mix64(value: int) -> int:
    z = (value + 0x9E3779B97F4A7C15) & U64
    z = ((z ^ (z >> 30)) * 0xBF58476D1CE4E5B9) & U64
    z = ((z ^ (z >> 27)) * 0x94D049BB133111EB) & U64
    return (z ^ (z >> 31)) & U64


def _scenario_seed_for(label: str) -> int:
    return _mix64(SCENARIO_SEED ^ _fnv1a64(label))


class Rng:
    """xorshift64* — the exact generator from wire.ts."""

    __slots__ = ("state",)

    def __init__(self, seed: int):
        masked = seed & U64
        self.state = masked if masked != 0 else SCENARIO_SEED

    def next_u64(self) -> int:
        x = self.state
        x ^= (x >> 12) & U64
        x ^= (x << 25) & U64
        x ^= (x >> 27) & U64
        self.state = x & U64
        return (self.state * 0x2545F4914F6CDD1D) & U64

    def next_u32(self) -> int:
        return self.next_u64() >> 32

    def next_int(self, bound: int) -> int:
        """Rejection-sampled int in [0, bound) — mirrors the TS loop exactly."""
        if bound <= 0:
            raise ValueError("bound must be positive")
        if bound == 1:
            return 0
        limit = (1 << 32) - ((1 << 32) % bound)
        while True:
            v = self.next_u32()
            if v < limit:
                return v % bound

    def fraction(self) -> float:
        return self.next_u32() / 4294967296.0

    def chance(self, p: float) -> bool:
        return self.fraction() < p

    def pick(self, items):
        return items[self.next_int(len(items))]

    def shuffle(self, items):
        out = list(items)
        for i in range(len(out) - 1, 0, -1):
            j = self.next_int(i + 1)
            out[i], out[j] = out[j], out[i]
        return out

    def bytes(self, length: int) -> bytes:
        out = bytearray()
        while len(out) < length:
            out.extend(self.next_u32().to_bytes(4, "little"))
        return bytes(out[:length])

    def hex(self, length: int) -> str:
        return "".join("0123456789abcdef"[self.next_int(16)] for _ in range(length))

    def words(self, count: int, items):
        return [self.pick(items) for _ in range(count)]


def _scenario_rng(label: str) -> Rng:
    return Rng(_scenario_seed_for(label))


def _iso_from_epoch(seconds: int) -> str:
    dt = datetime.fromtimestamp(seconds, tz=timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def _scenario_date(rng: Rng):
    epoch_sec = SCENARIO_EPOCH_SEC + rng.next_int(90 * 24 * 60 * 60)
    return epoch_sec, _iso_from_epoch(epoch_sec)


def _generate_users(rng: Rng, count: int):
    users = []
    used = set()
    for i in range(count):
        first = rng.pick(FIRST_NAMES)
        last = rng.pick(LAST_NAMES)
        base = re.sub(r"[^a-z0-9-]", "", first + last)
        login = base
        suffix = 2
        while login in used:
            login = f"{base}-{suffix}"
            suffix += 1
        used.add(login)
        users.append({
            "id": 1000 + i,
            "login": login,
            "name": f"{first} {last}",
            "email": f"{login}@example.invalid",
        })
    return users


def _generate_repos(rng: Rng, owner: str, count: int, first_id: int = 5000):
    repos = []
    used = set()
    for i in range(count):
        base = f"{rng.pick(REPO_ADJECTIVES)}-{rng.pick(REPO_NOUNS)}"
        name = base
        suffix = 2
        while name in used:
            name = f"{base}-{suffix}"
            suffix += 1
        used.add(name)
        repos.append({
            "id": first_id + i,
            "owner": owner,
            "name": name,
            "fullName": f"{owner}/{name}",
            "description": rng.pick(REPO_DESCRIPTIONS),
            "private": i % 7 == 3,
            "defaultBranch": DEFAULT_BRANCH,
        })
    return repos


def _generate_file_paths(rng: Rng, count: int):
    used = set()
    paths = []
    for _ in range(count):
        depth = 0 if rng.next_int(3) == 0 else 1 + rng.next_int(2)
        dirs = [] if depth == 0 else rng.shuffle(PATH_SEGMENTS)[:depth]
        base = "/".join(dirs + [f"{rng.pick(FILE_STEMS)}.{rng.pick(FILE_EXTENSIONS)}"])
        path = base
        suffix = 2
        while path in used:
            path = "/".join(dirs + [f"{suffix}-{base.split('/')[-1]}"])
            suffix += 1
        used.add(path)
        paths.append(path)
    return paths


def _generate_blob_text(rng: Rng, size: int) -> str:
    out = ""
    while len(out) < size:
        out += rng.pick(BLOB_WORDS)
        if len(out) < size:
            out += "\n" if rng.chance(0.14) else " "
    return out[:size]


def _generate_blob(rng: Rng, path: str, size: int):
    import base64

    content = _generate_blob_text(rng, size)
    return {
        "path": path,
        "content": content,
        "base64": base64.b64encode(content.encode("utf-8")).decode("ascii"),
    }


def _generate_sentence(rng: Rng) -> str:
    return f"{rng.pick(SENTENCE_STARTS)} {rng.pick(SENTENCE_ENDS)}."


def _generate_body(rng: Rng) -> str:
    count = 1 + rng.next_int(3)
    return " ".join(_generate_sentence(rng) for _ in range(count))


def _generate_commit_message(rng: Rng) -> str:
    head = f"{rng.pick(COMMIT_VERBS)} {rng.pick(COMMIT_OBJECTS)}"
    return f"{head}\n\n{_generate_sentence(rng)}" if rng.chance(0.35) else head


def _generate_commit(rng: Rng, author: dict):
    epoch_sec, iso = _scenario_date(rng)
    return {
        "message": _generate_commit_message(rng),
        "author": author,
        "epochSec": epoch_sec,
        "iso": iso,
    }


def _generate_issue(rng: Rng):
    return {
        "title": f"{rng.pick(ISSUE_ADJECTIVES)} {rng.pick(ISSUE_SUBJECTS)}",
        "body": _generate_body(rng),
        "labels": [rng.pick(ISSUE_LABELS)] if rng.chance(0.5) else [],
    }


def _generate_branch_name(rng: Rng) -> str:
    kind = rng.pick(BRANCH_KINDS)
    subject = rng.pick(PULL_SUBJECTS).lower()
    subject = re.sub(r"[^a-z0-9]+", "-", subject).strip("-")
    return f"{kind}/{subject}"


def _generate_pull(rng: Rng, base: str = DEFAULT_BRANCH):
    return {
        "title": f"{rng.pick(PULL_VERBS)} {rng.pick(PULL_SUBJECTS)}",
        "body": _generate_body(rng),
        "head": _generate_branch_name(rng),
        "base": base,
    }


def _unique_branch(rng: Rng, used: set, order: list) -> str:
    branch = _generate_branch_name(rng)
    suffix = 2
    while branch in used:
        branch = f"{_generate_branch_name(rng)}-{suffix}"
        suffix += 1
    used.add(branch)
    order.append(branch)
    return branch


def build_scenario() -> dict:
    users = _generate_users(_scenario_rng("users"), SCENARIO_USERS)
    viewer, teammate, stranger = users[0], users[1], users[2]

    org_rng = _scenario_rng("org")
    org = f"{org_rng.pick(REPO_ADJECTIVES)}-{org_rng.pick(REPO_NOUNS)}-org"

    branch_rng = _scenario_rng("branches")
    branch_set = set(DEFAULT_BRANCHES)
    branch_order = list(DEFAULT_BRANCHES)
    for _ in range(8):
        _unique_branch(branch_rng, branch_set, branch_order)

    file_paths = _generate_file_paths(_scenario_rng("paths"), SCENARIO_FILE_PATHS)

    blob_rng = _scenario_rng("blobs")
    blobs = [
        _generate_blob(blob_rng, path, 64 + blob_rng.next_int(4033))
        for path in file_paths[:SCENARIO_BLOBS]
    ]

    commit_rng = _scenario_rng("commits")
    commits = [
        _generate_commit(commit_rng, commit_rng.pick(users))
        for _ in range(SCENARIO_COMMITS)
    ]

    issue_rng = _scenario_rng("issues")
    issues = [_generate_issue(issue_rng) for _ in range(SCENARIO_ISSUES)]

    pull_rng = _scenario_rng("pulls")
    pulls = [_generate_pull(pull_rng) for _ in range(SCENARIO_PULLS)]

    repos = _generate_repos(_scenario_rng("repos.viewer"), viewer["login"], SCENARIO_VIEWER_REPOS, 5000)
    org_repos = _generate_repos(_scenario_rng("repos.org"), org, SCENARIO_ORG_REPOS, 6000)
    stranger_repos = _generate_repos(
        _scenario_rng("repos.stranger"), stranger["login"], SCENARIO_STRANGER_REPOS, 7000
    )

    return {
        "seed": SCENARIO_SEED_HEX,
        "viewer": viewer,
        "teammate": teammate,
        "stranger": stranger,
        "users": users,
        "org": org,
        "repos": repos,
        "orgRepos": org_repos,
        "strangerRepos": stranger_repos,
        "labels": list(ISSUE_LABELS),
        "branches": branch_order,
        "filePaths": file_paths,
        "blobs": blobs,
        "commits": commits,
        "issues": issues,
        "pulls": pulls,
    }


if __name__ == "__main__":
    import sys

    json.dump(build_scenario(), sys.stdout, indent=2, ensure_ascii=False)
    sys.stdout.write("\n")
