"""Operation keys shared by every matching path.

Call sites carry operation strings in three dialects (``"POST /v1/charges/{id}"`` for raw
HTTP, ``"get_weather"`` / ``"tool:get_weather"`` for MCP tools, ``"stripe.Charge.create"`` for
SDK symbols) and classifications carry ``affected_paths`` such as ``"POST /v1/charges"``,
``"/v1/charges"`` (a whole path item), ``"tool:get_weather"``, ``"package:stripe"``,
``"resource:<uri>"``, ``"prompt:<name>"``, ``"schema:<Name>"`` and ``"*"``.

``parse_operation`` turns any of them into an :class:`Operation`; ``normalize_operation``
gives the canonical string (``"POST /v1/charges/{id}"`` with numeric / uuid / vendor-id
segments templated, host stripped, path lowercased; ``"tool:<name>"``; ``"sdk:<dotted>"``;
``"package:<name>"``) and ``operations_match`` compares a call-site operation with an
affected path, including path templates, trailing slashes and the SDK symbol <-> HTTP
operation equivalences in :data:`SDK_RULES` (a data table: add a row to extend it).

Pure Python (stdlib only) so it can be reused by the CLI or the API without a database.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Literal
from urllib.parse import urlsplit

OperationKind = Literal[
    "http", "tool", "sdk", "package", "resource", "prompt", "schema", "any", "other"
]

HTTP_METHODS = frozenset(
    {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS", "TRACE", "CONNECT"}
)
ANY_METHOD = "*"

_METHOD_RE = re.compile(
    r"^(GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS|TRACE|CONNECT)\s+(\S.*)$", re.IGNORECASE
)
_HOST_RE = re.compile(r"^(?:[a-z0-9-]+(?:\.[a-z0-9-]+)+|localhost)(?::\d+)?$", re.IGNORECASE)
_PREFIXED_KINDS = ("tool", "sdk", "package", "resource", "prompt", "schema")

# Segments that are identifiers rather than static path parts.
_ID_PATTERNS = (
    re.compile(r"^\d+$"),  # 123
    re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I),  # uuid
    re.compile(r"^[0-9a-f]{16,}$", re.I),  # long hex (object ids, shas)
    re.compile(r"^[a-z]{1,8}_[A-Za-z0-9]{10,}$"),  # stripe-style ch_1Nx..., cus_..., acct_...
    re.compile(r"^[CUGDTWB][A-Z0-9]{8,10}$"),  # slack ids C0123456789 / U...
    re.compile(r"^(?=.*\d)[A-Za-z0-9_-]{24,}$"),  # long opaque tokens with a digit
)


@dataclass(frozen=True)
class Operation:
    """A parsed operation. ``segments`` are lowercased path segments with templates kept as
    ``{name}`` (``*`` is also a wildcard segment)."""

    kind: OperationKind
    method: str = ""  # http only: uppercase, or "*" when unknown
    segments: tuple[str, ...] = ()
    name: str = ""  # tool / sdk / package / resource / prompt / schema
    raw: str = ""

    @property
    def path(self) -> str:
        return "/" + "/".join(self.segments)

    def canonical(self) -> str:
        if self.kind == "http":
            if self.method == ANY_METHOD:
                return self.path
            return f"{self.method} {self.path}"
        if self.kind in _PREFIXED_KINDS:
            return f"{self.kind}:{self.name}"
        if self.kind == "any":
            return "*"
        return self.raw


# --------------------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------------------


def _looks_like_id(segment: str) -> bool:
    return any(p.match(segment) for p in _ID_PATTERNS)


def template_segment(segment: str) -> str:
    """One path segment -> its canonical form (``{id}`` for ids, ``{name}`` for templates
    in any of the common syntaxes, lowercase literal otherwise)."""
    s = segment.strip()
    if not s:
        return s
    if s == "*" or s == "**":
        return "*"
    if s.startswith("${") and s.endswith("}"):
        inner = s[2:-1].strip()
        return (
            "{" + inner.lower() + "}" if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", inner) else "{id}"
        )
    if s.startswith("{") and s.endswith("}"):
        inner = s[1:-1].strip().lower()
        return "{" + (inner or "id") + "}"
    if s.startswith(":") and len(s) > 1:
        return "{" + s[1:].lower() + "}"
    if s.startswith("<") and s.endswith(">") and len(s) > 2:
        inner = s[1:-1].split(":")[-1].strip().lower()
        return "{" + (inner or "id") + "}"
    if _looks_like_id(s):
        return "{id}"
    return s.lower()


def is_template_segment(segment: str) -> bool:
    return segment == "*" or (segment.startswith("{") and segment.endswith("}"))


def _split_path(target: str) -> tuple[str, ...]:
    """Strip scheme/host/query, collapse slashes, drop the trailing slash, template ids."""
    t = target.strip()
    if "://" in t:
        parts = urlsplit(t)
        t = parts.path or "/"
    else:
        t = t.split("?", 1)[0].split("#", 1)[0]
        if not t.startswith("/"):
            first, _, rest = t.partition("/")
            if _HOST_RE.match(first):
                t = "/" + rest
            else:
                t = "/" + t
    segs = [seg for seg in t.split("/") if seg != ""]
    return tuple(template_segment(seg) for seg in segs)


def _is_sdk_symbol(s: str) -> bool:
    return (
        "." in s
        and " " not in s
        and not s.startswith("/")
        and re.fullmatch(r"[@A-Za-z_][\w@/.-]*", s) is not None
    )


@lru_cache(maxsize=4096)
def parse_operation(value: str | None) -> Operation:
    raw = (value or "").strip()
    if not raw:
        return Operation(kind="other", raw="")
    if raw == "*":
        return Operation(kind="any", raw=raw)
    lower = raw.lower()
    for prefix in _PREFIXED_KINDS:
        if lower.startswith(prefix + ":"):
            name = raw[len(prefix) + 1 :].strip()
            if prefix == "package":
                name = _normalize_package_name(name)
            return Operation(kind=prefix, name=name, raw=raw)  # type: ignore[arg-type]
    m = _METHOD_RE.match(raw)
    if m:
        return Operation(
            kind="http", method=m.group(1).upper(), segments=_split_path(m.group(2)), raw=raw
        )
    if raw.startswith("/") or "://" in raw:
        return Operation(kind="http", method=ANY_METHOD, segments=_split_path(raw), raw=raw)
    if _is_sdk_symbol(raw):
        return Operation(kind="sdk", name=raw, raw=raw)
    if re.fullmatch(r"[A-Za-z_][\w.-]*", raw):
        return Operation(kind="tool", name=raw, raw=raw)
    return Operation(kind="other", raw=raw)


def _normalize_package_name(name: str) -> str:
    n = name.strip()
    if n.startswith("@"):
        return n.lower()
    return re.sub(r"[-_.]+", "-", n).lower()


def normalize_operation(value: str | None) -> str:
    """Canonical string for an operation in any dialect (see the module docstring)."""
    return parse_operation(value).canonical()


def http_path(value: str | None) -> str | None:
    """The canonical path of an HTTP operation, or None for the other kinds."""
    op = parse_operation(value)
    return op.path if op.kind == "http" else None


def pointer_operation(pointer: str) -> str | None:
    """The operation a diff JSON pointer refers to: ``/paths/~1v1~1charges/post/...`` ->
    ``"POST /v1/charges"``, ``/paths/~1v1~1charges`` -> ``"/v1/charges"``,
    ``/tools/get_weather/inputSchema`` -> ``"tool:get_weather"``, ``/resources/<uri>`` ->
    ``"resource:<uri>"``, ``/prompts/<name>`` -> ``"prompt:<name>"``; None otherwise."""
    if not pointer or not pointer.startswith("/"):
        return None
    tokens = [t.replace("~1", "/").replace("~0", "~") for t in pointer.lstrip("/").split("/")]
    if not tokens:
        return None
    head = tokens[0]
    if head == "paths" and len(tokens) >= 2:
        path = tokens[1]
        if len(tokens) >= 3 and tokens[2].upper() in HTTP_METHODS:
            return f"{tokens[2].upper()} {path}"
        return path
    if head == "tools" and len(tokens) >= 2:
        return f"tool:{tokens[1]}"
    if head == "resources" and len(tokens) >= 2:
        return f"resource:{tokens[1]}"
    if head == "prompts" and len(tokens) >= 2:
        return f"prompt:{tokens[1]}"
    return None


# --------------------------------------------------------------------------------------
# SDK symbol <-> HTTP operation table
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class SdkRule:
    """A family of SDK symbols and the HTTP operations they issue.

    ``pattern`` is an anchored regex over the call-site symbol (as the indexer records it:
    package root first, e.g. ``slack_sdk.WebClient.chat_postMessage``). ``operations`` are
    ``(method, path)`` pairs; ``{n}`` in a path is replaced by regex group ``n`` and
    ``{n:dots}`` by group ``n`` with underscores turned into dots. ``"*"`` as the method
    means "whatever method the spec uses".
    """

    vendor: str
    pattern: str
    operations: tuple[tuple[str, str], ...]


def _rule(vendor: str, pattern: str, *operations: tuple[str, str]) -> SdkRule:
    return SdkRule(vendor=vendor, pattern=pattern, operations=tuple(operations))


_GH_PREFIX = r"^(?:github|PyGithub|pygithub)\.(?:[\w]+\.)*?"
_OCTO_PREFIX = r"^(?:@octokit/[\w-]+|octokit)\.(?:[\w]+\.)*?"
_REPO = "/repos/{owner}/{repo}"
_OPENAI_PREFIX = r"^openai\.(?:[\w]+\.)*?"
_ANTHROPIC_PREFIX = r"^(?:anthropic|@anthropic-ai/sdk)\.(?:[\w]+\.)*?"
_NOTION_PREFIX = r"^(?:notion_client|@notionhq/client|notion)\.(?:[\w]+\.)*?"
_HUBSPOT_PREFIX = r"^(?:hubspot|@hubspot/api-client)\.(?:[\w]+\.)*?"
_TWILIO_PREFIX = r"^twilio\.(?:[\w]+\.)*?"
_SENDGRID_PREFIX = r"^(?:sendgrid|@sendgrid/mail|@sendgrid/client)\.(?:[\w]+\.)*?"
_SLACK_PREFIX = r"^(?:slack_sdk|slack|@slack/web-api|@slack/bolt)(?:\.[\w]+)*?\."

EXPLICIT_SDK_RULES: tuple[SdkRule, ...] = (
    # ---- Slack: python ``chat_postMessage`` and node ``chat.postMessage`` -> /chat.postMessage
    _rule(
        "slack", _SLACK_PREFIX + r"(?:Async)?WebClient\.([a-z][A-Za-z0-9_]*)$", ("*", "/{1:dots}")
    ),
    _rule(
        "slack",
        _SLACK_PREFIX + r"(?:WebClient|client|web)\.([a-z]+(?:\.[A-Za-z0-9]+)+)$",
        ("*", "/{1}"),
    ),
    # ---- PyGithub
    _rule("github", _GH_PREFIX + r"get_repo$", ("GET", _REPO)),
    _rule("github", _GH_PREFIX + r"get_user$", ("GET", "/user"), ("GET", "/users/{username}")),
    _rule("github", _GH_PREFIX + r"get_organization$", ("GET", "/orgs/{org}")),
    _rule("github", _GH_PREFIX + r"get_rate_limit$", ("GET", "/rate_limit")),
    _rule("github", _GH_PREFIX + r"search_issues$", ("GET", "/search/issues")),
    _rule("github", _GH_PREFIX + r"search_repositories$", ("GET", "/search/repositories")),
    _rule("github", _GH_PREFIX + r"search_code$", ("GET", "/search/code")),
    _rule("github", _GH_PREFIX + r"search_users$", ("GET", "/search/users")),
    _rule("github", _GH_PREFIX + r"create_issue$", ("POST", _REPO + "/issues")),
    _rule("github", _GH_PREFIX + r"get_issue$", ("GET", _REPO + "/issues/{issue_number}")),
    _rule("github", _GH_PREFIX + r"get_issues$", ("GET", _REPO + "/issues")),
    _rule(
        "github",
        _GH_PREFIX + r"(?:create_comment|create_issue_comment)$",
        ("POST", _REPO + "/issues/{issue_number}/comments"),
    ),
    _rule(
        "github",
        _GH_PREFIX + r"(?:get_comments|get_issue_comments)$",
        ("GET", _REPO + "/issues/{issue_number}/comments"),
    ),
    _rule(
        "github", _GH_PREFIX + r"add_to_labels$", ("POST", _REPO + "/issues/{issue_number}/labels")
    ),
    _rule("github", _GH_PREFIX + r"create_pull$", ("POST", _REPO + "/pulls")),
    _rule("github", _GH_PREFIX + r"get_pull$", ("GET", _REPO + "/pulls/{pull_number}")),
    _rule("github", _GH_PREFIX + r"get_pulls$", ("GET", _REPO + "/pulls")),
    _rule("github", _GH_PREFIX + r"merge$", ("PUT", _REPO + "/pulls/{pull_number}/merge")),
    _rule(
        "github", _GH_PREFIX + r"create_review$", ("POST", _REPO + "/pulls/{pull_number}/reviews")
    ),
    _rule("github", _GH_PREFIX + r"get_files$", ("GET", _REPO + "/pulls/{pull_number}/files")),
    _rule("github", _GH_PREFIX + r"get_contents$", ("GET", _REPO + "/contents/{path}")),
    _rule(
        "github", _GH_PREFIX + r"(?:create_file|update_file)$", ("PUT", _REPO + "/contents/{path}")
    ),
    _rule("github", _GH_PREFIX + r"delete_file$", ("DELETE", _REPO + "/contents/{path}")),
    _rule("github", _GH_PREFIX + r"get_readme$", ("GET", _REPO + "/readme")),
    _rule("github", _GH_PREFIX + r"get_branch$", ("GET", _REPO + "/branches/{branch}")),
    _rule("github", _GH_PREFIX + r"get_branches$", ("GET", _REPO + "/branches")),
    _rule("github", _GH_PREFIX + r"get_git_ref$", ("GET", _REPO + "/git/ref/{ref}")),
    _rule("github", _GH_PREFIX + r"create_git_ref$", ("POST", _REPO + "/git/refs")),
    _rule("github", _GH_PREFIX + r"get_commits$", ("GET", _REPO + "/commits")),
    _rule("github", _GH_PREFIX + r"get_commit$", ("GET", _REPO + "/commits/{ref}")),
    _rule("github", _GH_PREFIX + r"get_labels$", ("GET", _REPO + "/labels")),
    _rule("github", _GH_PREFIX + r"get_label$", ("GET", _REPO + "/labels/{name}")),
    _rule("github", _GH_PREFIX + r"create_label$", ("POST", _REPO + "/labels")),
    _rule("github", _GH_PREFIX + r"get_releases$", ("GET", _REPO + "/releases")),
    _rule("github", _GH_PREFIX + r"get_latest_release$", ("GET", _REPO + "/releases/latest")),
    _rule("github", _GH_PREFIX + r"create_git_release$", ("POST", _REPO + "/releases")),
    _rule("github", _GH_PREFIX + r"get_workflow_runs$", ("GET", _REPO + "/actions/runs")),
    _rule("github", _GH_PREFIX + r"get_workflows$", ("GET", _REPO + "/actions/workflows")),
    _rule("github", _GH_PREFIX + r"get_hooks$", ("GET", _REPO + "/hooks")),
    _rule("github", _GH_PREFIX + r"create_hook$", ("POST", _REPO + "/hooks")),
    _rule("github", _GH_PREFIX + r"get_collaborators$", ("GET", _REPO + "/collaborators")),
    _rule(
        "github",
        _GH_PREFIX + r"add_to_collaborators$",
        ("PUT", _REPO + "/collaborators/{username}"),
    ),
    _rule(
        "github",
        _GH_PREFIX + r"edit$",
        ("PATCH", _REPO + "/issues/{issue_number}"),
        ("PATCH", _REPO + "/pulls/{pull_number}"),
        ("PATCH", _REPO),
    ),
    # ---- Octokit (node)
    _rule("github", _OCTO_PREFIX + r"repos\.get$", ("GET", _REPO)),
    _rule("github", _OCTO_PREFIX + r"users\.getAuthenticated$", ("GET", "/user")),
    _rule("github", _OCTO_PREFIX + r"users\.getByUsername$", ("GET", "/users/{username}")),
    _rule("github", _OCTO_PREFIX + r"orgs\.get$", ("GET", "/orgs/{org}")),
    _rule("github", _OCTO_PREFIX + r"rateLimit\.get$", ("GET", "/rate_limit")),
    _rule("github", _OCTO_PREFIX + r"issues\.create$", ("POST", _REPO + "/issues")),
    _rule("github", _OCTO_PREFIX + r"issues\.get$", ("GET", _REPO + "/issues/{issue_number}")),
    _rule("github", _OCTO_PREFIX + r"issues\.update$", ("PATCH", _REPO + "/issues/{issue_number}")),
    _rule("github", _OCTO_PREFIX + r"issues\.listForRepo$", ("GET", _REPO + "/issues")),
    _rule(
        "github",
        _OCTO_PREFIX + r"issues\.createComment$",
        ("POST", _REPO + "/issues/{issue_number}/comments"),
    ),
    _rule(
        "github",
        _OCTO_PREFIX + r"issues\.listComments$",
        ("GET", _REPO + "/issues/{issue_number}/comments"),
    ),
    _rule(
        "github",
        _OCTO_PREFIX + r"issues\.addLabels$",
        ("POST", _REPO + "/issues/{issue_number}/labels"),
    ),
    _rule("github", _OCTO_PREFIX + r"pulls\.create$", ("POST", _REPO + "/pulls")),
    _rule("github", _OCTO_PREFIX + r"pulls\.get$", ("GET", _REPO + "/pulls/{pull_number}")),
    _rule("github", _OCTO_PREFIX + r"pulls\.list$", ("GET", _REPO + "/pulls")),
    _rule("github", _OCTO_PREFIX + r"pulls\.merge$", ("PUT", _REPO + "/pulls/{pull_number}/merge")),
    _rule(
        "github",
        _OCTO_PREFIX + r"pulls\.createReview$",
        ("POST", _REPO + "/pulls/{pull_number}/reviews"),
    ),
    _rule(
        "github", _OCTO_PREFIX + r"pulls\.listFiles$", ("GET", _REPO + "/pulls/{pull_number}/files")
    ),
    _rule("github", _OCTO_PREFIX + r"repos\.getContent$", ("GET", _REPO + "/contents/{path}")),
    _rule(
        "github",
        _OCTO_PREFIX + r"repos\.createOrUpdateFileContents$",
        ("PUT", _REPO + "/contents/{path}"),
    ),
    _rule("github", _OCTO_PREFIX + r"repos\.deleteFile$", ("DELETE", _REPO + "/contents/{path}")),
    _rule("github", _OCTO_PREFIX + r"repos\.listCommits$", ("GET", _REPO + "/commits")),
    _rule("github", _OCTO_PREFIX + r"repos\.getCommit$", ("GET", _REPO + "/commits/{ref}")),
    _rule("github", _OCTO_PREFIX + r"repos\.getBranch$", ("GET", _REPO + "/branches/{branch}")),
    _rule("github", _OCTO_PREFIX + r"repos\.listBranches$", ("GET", _REPO + "/branches")),
    _rule("github", _OCTO_PREFIX + r"repos\.createRelease$", ("POST", _REPO + "/releases")),
    _rule("github", _OCTO_PREFIX + r"repos\.listReleases$", ("GET", _REPO + "/releases")),
    _rule(
        "github", _OCTO_PREFIX + r"repos\.getLatestRelease$", ("GET", _REPO + "/releases/latest")
    ),
    _rule("github", _OCTO_PREFIX + r"git\.createRef$", ("POST", _REPO + "/git/refs")),
    _rule("github", _OCTO_PREFIX + r"git\.getRef$", ("GET", _REPO + "/git/ref/{ref}")),
    _rule("github", _OCTO_PREFIX + r"search\.issuesAndPullRequests$", ("GET", "/search/issues")),
    _rule("github", _OCTO_PREFIX + r"search\.repos$", ("GET", "/search/repositories")),
    _rule("github", _OCTO_PREFIX + r"search\.code$", ("GET", "/search/code")),
    _rule(
        "github",
        _OCTO_PREFIX + r"actions\.listWorkflowRunsForRepo$",
        ("GET", _REPO + "/actions/runs"),
    ),
    # ---- OpenAI
    _rule("openai", _OPENAI_PREFIX + r"chat\.completions\.create$", ("POST", "/chat/completions")),
    _rule("openai", _OPENAI_PREFIX + r"(?<!chat\.)completions\.create$", ("POST", "/completions")),
    _rule("openai", _OPENAI_PREFIX + r"responses\.create$", ("POST", "/responses")),
    _rule("openai", _OPENAI_PREFIX + r"embeddings\.create$", ("POST", "/embeddings")),
    _rule("openai", _OPENAI_PREFIX + r"images\.generate$", ("POST", "/images/generations")),
    _rule(
        "openai",
        _OPENAI_PREFIX + r"audio\.transcriptions\.create$",
        ("POST", "/audio/transcriptions"),
    ),
    _rule("openai", _OPENAI_PREFIX + r"audio\.speech\.create$", ("POST", "/audio/speech")),
    _rule("openai", _OPENAI_PREFIX + r"moderations\.create$", ("POST", "/moderations")),
    _rule("openai", _OPENAI_PREFIX + r"models\.list$", ("GET", "/models")),
    _rule("openai", _OPENAI_PREFIX + r"models\.retrieve$", ("GET", "/models/{model}")),
    _rule("openai", _OPENAI_PREFIX + r"files\.create$", ("POST", "/files")),
    _rule("openai", _OPENAI_PREFIX + r"batches\.create$", ("POST", "/batches")),
    _rule("openai", _OPENAI_PREFIX + r"fine_tuning\.jobs\.create$", ("POST", "/fine_tuning/jobs")),
    # ---- Anthropic
    _rule("anthropic", _ANTHROPIC_PREFIX + r"messages\.create$", ("POST", "/v1/messages")),
    _rule(
        "anthropic",
        _ANTHROPIC_PREFIX + r"messages\.(?:count_tokens|countTokens)$",
        ("POST", "/v1/messages/count_tokens"),
    ),
    _rule(
        "anthropic",
        _ANTHROPIC_PREFIX + r"messages\.batches\.create$",
        ("POST", "/v1/messages/batches"),
    ),
    _rule("anthropic", _ANTHROPIC_PREFIX + r"models\.list$", ("GET", "/v1/models")),
    # ---- Notion
    _rule("notion", _NOTION_PREFIX + r"pages\.create$", ("POST", "/v1/pages")),
    _rule("notion", _NOTION_PREFIX + r"pages\.retrieve$", ("GET", "/v1/pages/{page_id}")),
    _rule("notion", _NOTION_PREFIX + r"pages\.update$", ("PATCH", "/v1/pages/{page_id}")),
    _rule(
        "notion",
        _NOTION_PREFIX + r"databases\.query$",
        ("POST", "/v1/databases/{database_id}/query"),
    ),
    _rule(
        "notion", _NOTION_PREFIX + r"databases\.retrieve$", ("GET", "/v1/databases/{database_id}")
    ),
    _rule("notion", _NOTION_PREFIX + r"databases\.create$", ("POST", "/v1/databases")),
    _rule(
        "notion", _NOTION_PREFIX + r"databases\.update$", ("PATCH", "/v1/databases/{database_id}")
    ),
    _rule(
        "notion",
        _NOTION_PREFIX + r"blocks\.children\.list$",
        ("GET", "/v1/blocks/{block_id}/children"),
    ),
    _rule(
        "notion",
        _NOTION_PREFIX + r"blocks\.children\.append$",
        ("PATCH", "/v1/blocks/{block_id}/children"),
    ),
    _rule("notion", _NOTION_PREFIX + r"blocks\.retrieve$", ("GET", "/v1/blocks/{block_id}")),
    _rule("notion", _NOTION_PREFIX + r"blocks\.update$", ("PATCH", "/v1/blocks/{block_id}")),
    _rule("notion", _NOTION_PREFIX + r"blocks\.delete$", ("DELETE", "/v1/blocks/{block_id}")),
    _rule("notion", _NOTION_PREFIX + r"search$", ("POST", "/v1/search")),
    _rule("notion", _NOTION_PREFIX + r"users\.list$", ("GET", "/v1/users")),
    _rule("notion", _NOTION_PREFIX + r"users\.me$", ("GET", "/v1/users/me")),
    _rule("notion", _NOTION_PREFIX + r"users\.retrieve$", ("GET", "/v1/users/{user_id}")),
    _rule("notion", _NOTION_PREFIX + r"comments\.create$", ("POST", "/v1/comments")),
    _rule("notion", _NOTION_PREFIX + r"comments\.list$", ("GET", "/v1/comments")),
    # ---- HubSpot CRM objects (python basic_api / node basicApi)
    _rule(
        "hubspot",
        _HUBSPOT_PREFIX
        + r"crm\.(contacts|companies|deals|tickets)\.(?:basic_api|basicApi)\.create$",
        ("POST", "/crm/v3/objects/{1}"),
    ),
    _rule(
        "hubspot",
        _HUBSPOT_PREFIX
        + r"crm\.(contacts|companies|deals|tickets)\.(?:basic_api|basicApi)\.(?:get_by_id|getById)$",
        ("GET", "/crm/v3/objects/{1}/{id}"),
    ),
    _rule(
        "hubspot",
        _HUBSPOT_PREFIX
        + r"crm\.(contacts|companies|deals|tickets)\.(?:basic_api|basicApi)\.(?:get_page|getPage)$",
        ("GET", "/crm/v3/objects/{1}"),
    ),
    _rule(
        "hubspot",
        _HUBSPOT_PREFIX
        + r"crm\.(contacts|companies|deals|tickets)\.(?:basic_api|basicApi)\.update$",
        ("PATCH", "/crm/v3/objects/{1}/{id}"),
    ),
    _rule(
        "hubspot",
        _HUBSPOT_PREFIX
        + r"crm\.(contacts|companies|deals|tickets)\.(?:basic_api|basicApi)\.archive$",
        ("DELETE", "/crm/v3/objects/{1}/{id}"),
    ),
    _rule(
        "hubspot",
        _HUBSPOT_PREFIX
        + r"crm\.(contacts|companies|deals|tickets)\.(?:search_api|searchApi)\.(?:do_search|doSearch)$",
        ("POST", "/crm/v3/objects/{1}/search"),
    ),
    # ---- Twilio
    _rule(
        "twilio",
        _TWILIO_PREFIX + r"messages\.create$",
        ("POST", "/2010-04-01/Accounts/{AccountSid}/Messages.json"),
    ),
    _rule(
        "twilio",
        _TWILIO_PREFIX + r"messages\.list$",
        ("GET", "/2010-04-01/Accounts/{AccountSid}/Messages.json"),
    ),
    _rule(
        "twilio",
        _TWILIO_PREFIX + r"calls\.create$",
        ("POST", "/2010-04-01/Accounts/{AccountSid}/Calls.json"),
    ),
    _rule(
        "twilio", _TWILIO_PREFIX + r"verify\.(?:v2\.)?services\.create$", ("POST", "/v2/Services")
    ),
    # ---- SendGrid
    _rule("sendgrid", _SENDGRID_PREFIX + r"(?:send|mail\.send\.post)$", ("POST", "/v3/mail/send")),
)

# Stripe: one row per resource; verbs are shared.
STRIPE_RESOURCES: tuple[tuple[str, str, str], ...] = (
    # (python class path, node property path, REST path)
    ("Charge", "charges", "/v1/charges"),
    ("Customer", "customers", "/v1/customers"),
    ("PaymentIntent", "paymentIntents", "/v1/payment_intents"),
    ("SetupIntent", "setupIntents", "/v1/setup_intents"),
    ("PaymentMethod", "paymentMethods", "/v1/payment_methods"),
    ("Refund", "refunds", "/v1/refunds"),
    ("Subscription", "subscriptions", "/v1/subscriptions"),
    ("SubscriptionItem", "subscriptionItems", "/v1/subscription_items"),
    ("Invoice", "invoices", "/v1/invoices"),
    ("InvoiceItem", "invoiceItems", "/v1/invoiceitems"),
    ("Product", "products", "/v1/products"),
    ("Price", "prices", "/v1/prices"),
    ("Plan", "plans", "/v1/plans"),
    ("Coupon", "coupons", "/v1/coupons"),
    ("PromotionCode", "promotionCodes", "/v1/promotion_codes"),
    ("Payout", "payouts", "/v1/payouts"),
    ("Transfer", "transfers", "/v1/transfers"),
    ("Account", "accounts", "/v1/accounts"),
    ("Event", "events", "/v1/events"),
    ("WebhookEndpoint", "webhookEndpoints", "/v1/webhook_endpoints"),
    ("Token", "tokens", "/v1/tokens"),
    ("Dispute", "disputes", "/v1/disputes"),
    ("BalanceTransaction", "balanceTransactions", "/v1/balance_transactions"),
    ("PaymentLink", "paymentLinks", "/v1/payment_links"),
    ("Quote", "quotes", "/v1/quotes"),
    ("TaxRate", "taxRates", "/v1/tax_rates"),
    ("File", "files", "/v1/files"),
    ("checkout.Session", "checkout.sessions", "/v1/checkout/sessions"),
    ("billing_portal.Session", "billingPortal.sessions", "/v1/billing_portal/sessions"),
)
STRIPE_VERBS: tuple[tuple[str, str, str], ...] = (
    # (verb regex, method, path suffix)
    ("create", "POST", ""),
    ("retrieve", "GET", "/{id}"),
    ("modify|update", "POST", "/{id}"),
    ("list", "GET", ""),
    ("delete|del", "DELETE", "/{id}"),
    ("search", "GET", "/search"),
    ("capture", "POST", "/{id}/capture"),
    ("cancel", "POST", "/{id}/cancel"),
    ("confirm", "POST", "/{id}/confirm"),
    ("pay", "POST", "/{id}/pay"),
    ("finalize_invoice|finalizeInvoice", "POST", "/{id}/finalize"),
    ("void_invoice|voidInvoice", "POST", "/{id}/void"),
    ("send_invoice|sendInvoice", "POST", "/{id}/send"),
    ("expire", "POST", "/{id}/expire"),
    ("attach", "POST", "/{id}/attach"),
    ("detach", "POST", "/{id}/detach"),
    ("reject", "POST", "/{id}/reject"),
    ("increment_authorization|incrementAuthorization", "POST", "/{id}/increment_authorization"),
)


def _stripe_rules() -> tuple[SdkRule, ...]:
    out: list[SdkRule] = []
    prefix = r"^stripe\.(?:[\w]+\.)*?"
    for py, node, path in STRIPE_RESOURCES:
        # ``StripeClient`` services are snake_case after the REST path: client.v1.payment_intents
        service = path.removeprefix("/v1/").replace("/", ".")
        res = f"(?:{re.escape(py)}|{re.escape(node)}|{re.escape(service)})"
        for verb, method, suffix in STRIPE_VERBS:
            out.append(_rule("stripe", prefix + res + rf"\.(?:{verb})$", (method, path + suffix)))
    out.append(_rule("stripe", prefix + r"(?:Balance|balance)\.retrieve$", ("GET", "/v1/balance")))
    out.append(
        _rule(
            "stripe",
            prefix + r"(?:Invoice|invoices)\.(?:upcoming|retrieve_upcoming|retrieveUpcoming)$",
            ("GET", "/v1/invoices/upcoming"),
        )
    )
    return tuple(out)


SDK_RULES: tuple[SdkRule, ...] = EXPLICIT_SDK_RULES + _stripe_rules()

_EXTRA_RULES: list[SdkRule] = []


def register_sdk_rule(rule: SdkRule) -> None:
    """Extend the table at runtime (plugins, tests)."""
    _EXTRA_RULES.append(rule)
    _compiled_rules.cache_clear()
    sdk_http_operations.cache_clear()


@lru_cache(maxsize=1)
def _compiled_rules() -> tuple[tuple[re.Pattern[str], SdkRule], ...]:
    return tuple((re.compile(r.pattern), r) for r in (*SDK_RULES, *_EXTRA_RULES))


_GROUP_RE = re.compile(r"\{(\d+)(?::(\w+))?\}")


def _expand(template: str, m: re.Match[str]) -> str:
    def sub(g: re.Match[str]) -> str:
        idx, mod = int(g.group(1)), g.group(2)
        try:
            val = m.group(idx) or ""
        except IndexError:
            val = ""
        if mod == "dots":
            val = val.replace("_", ".")
        return val

    return _GROUP_RE.sub(sub, template)


@lru_cache(maxsize=4096)
def sdk_http_operations(symbol: str) -> tuple[Operation, ...]:
    """HTTP operations an SDK symbol issues according to :data:`SDK_RULES` (empty when
    the symbol is unknown)."""
    sym = symbol.strip()
    if sym.lower().startswith("sdk:"):
        sym = sym[4:].strip()
    out: list[Operation] = []
    seen: set[str] = set()
    for pat, rule in _compiled_rules():
        m = pat.match(sym)
        if m is None:
            continue
        for method, path in rule.operations:
            op = Operation(
                kind="http",
                method=method.upper() if method != ANY_METHOD else ANY_METHOD,
                segments=_split_path(_expand(path, m)),
                raw=f"{method} {path}",
            )
            key = op.canonical()
            if key not in seen:
                seen.add(key)
                out.append(op)
    return tuple(out)


def sdk_vendor(symbol: str) -> str | None:
    """The vendor slug of the first rule matching ``symbol`` (None when unknown)."""
    sym = symbol.strip()
    if sym.lower().startswith("sdk:"):
        sym = sym[4:].strip()
    for pat, rule in _compiled_rules():
        if pat.match(sym):
            return rule.vendor
    return None


# --------------------------------------------------------------------------------------
# Matching
# --------------------------------------------------------------------------------------


def _segments_match(a: tuple[str, ...], b: tuple[str, ...]) -> bool:
    if len(a) != len(b):
        return False
    for x, y in zip(a, b, strict=True):
        if x == y:
            continue
        if is_template_segment(x) or is_template_segment(y):
            continue
        return False
    return True


def _http_match(a: Operation, b: Operation) -> bool:
    if a.method != ANY_METHOD and b.method != ANY_METHOD and a.method != b.method:
        return False
    return _segments_match(a.segments, b.segments)


def _sdk_match(a: str, b: str) -> bool:
    """Equal symbols, or one is a dotted suffix of the other
    (``WebClient.chat_postMessage`` ~ ``slack_sdk.WebClient.chat_postMessage``)."""
    if a == b:
        return True
    short, long_ = (a, b) if len(a) <= len(b) else (b, a)
    return long_.endswith("." + short)


def operations_match(site_operation: str | None, affected: str | None) -> bool:
    """Does a call site's operation fall under an affected path?

    * ``"*"`` and ``"package:<name>"`` affect every call site of the dependency (the caller
      has already scoped call sites to the dependency that points at the source).
    * HTTP vs HTTP: methods must agree (``"*"`` is unknown and matches any), paths are
      compared segment by segment with templates (``{id}``, ``{charge}``, ``*``, numeric or
      uuid literals) as wildcards; trailing slashes, query strings and hosts are ignored.
    * tool / resource / prompt / schema: exact name.
    * SDK vs SDK: equal or dotted-suffix related.
    * SDK vs HTTP (either way round): through :data:`SDK_RULES`.
    """
    a = parse_operation(site_operation)
    b = parse_operation(affected)
    if a.kind == "other" and not a.raw:
        return False
    if b.kind in ("any", "package"):
        return True
    if a.kind == "any":
        return True
    if a.kind == b.kind:
        if a.kind == "http":
            return _http_match(a, b)
        if a.kind == "sdk":
            return _sdk_match(a.name, b.name)
        if a.kind in ("tool", "resource", "prompt", "schema"):
            return a.name == b.name
        return a.raw == b.raw
    if a.kind == "sdk" and b.kind == "http":
        return any(_http_match(h, b) for h in sdk_http_operations(a.name))
    if a.kind == "http" and b.kind == "sdk":
        return any(_http_match(a, h) for h in sdk_http_operations(b.name))
    if a.kind == "tool" and b.kind == "sdk":
        return b.name.rsplit(".", 1)[-1] == a.name
    if a.kind == "sdk" and b.kind == "tool":
        return a.name.rsplit(".", 1)[-1] == b.name
    return False


def matches_any(site_operation: str | None, affected_paths: list[str] | tuple[str, ...]) -> bool:
    return any(operations_match(site_operation, p) for p in affected_paths)


__all__ = [
    "ANY_METHOD",
    "HTTP_METHODS",
    "Operation",
    "SDK_RULES",
    "STRIPE_RESOURCES",
    "STRIPE_VERBS",
    "SdkRule",
    "http_path",
    "is_template_segment",
    "matches_any",
    "normalize_operation",
    "operations_match",
    "parse_operation",
    "pointer_operation",
    "register_sdk_rule",
    "sdk_http_operations",
    "sdk_vendor",
    "template_segment",
]
