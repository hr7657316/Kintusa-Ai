"""In-memory stand-ins for the parts of PyGithub Kintsua uses.

Components call ``get_github_app().installation_client(installation_id)`` and then
``gh.get_repo(full_name)`` -> ``FakeRepo`` with ``create_issue``, ``get_issue``,
``create_pull``, ``get_contents``, ``get_branch``, ``create_git_ref`` ... enough for the
matcher, fixer and webhook handlers to be exercised without the network.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any

from kintsua.core.github import GitHubApp


@dataclass
class FakeComment:
    body: str
    id: int = 0


@dataclass
class FakeLabel:
    name: str


@dataclass
class FakeIssue:
    number: int
    title: str
    body: str
    labels: list[FakeLabel] = field(default_factory=list)
    state: str = "open"
    comments: list[FakeComment] = field(default_factory=list)
    html_url: str = ""

    def create_comment(self, body: str) -> FakeComment:
        c = FakeComment(body=body, id=len(self.comments) + 1)
        self.comments.append(c)
        return c

    def edit(self, **kw: Any) -> None:
        for k, v in kw.items():
            setattr(self, k, v)

    def get_labels(self):
        return list(self.labels)

    def add_to_labels(self, *names: str) -> None:
        for n in names:
            if not any(lb.name == n for lb in self.labels):
                self.labels.append(FakeLabel(n))


@dataclass
class FakePull(FakeIssue):
    head: str = ""
    base: str = "main"
    merged: bool = False
    merged_at: Any = None

    def get_issue_comments(self):
        return list(self.comments)

    def create_issue_comment(self, body: str) -> FakeComment:
        return self.create_comment(body)


@dataclass
class FakeBranch:
    name: str
    protected: bool = False

    @property
    def commit(self):
        return type("C", (), {"sha": "0" * 40})()


class FakeRepo:
    def __init__(self, full_name: str, default_branch: str = "main", repo_id: int = 1):
        self.full_name = full_name
        self.name = full_name.split("/")[-1]
        self.default_branch = default_branch
        self.id = repo_id
        self.issues: dict[int, FakeIssue] = {}
        self.pulls: dict[int, FakePull] = {}
        self.branches: dict[str, FakeBranch] = {default_branch: FakeBranch(default_branch)}
        self.labels: dict[str, FakeLabel] = {}
        self.files: dict[str, str] = {}
        self._n = 0
        self.html_url = f"https://github.com/{full_name}"
        self.private = False
        self.lock = threading.Lock()

    def _next(self) -> int:
        with self.lock:
            self._n += 1
            return self._n

    # issues ----------------------------------------------------------------------
    def create_issue(
        self, title: str, body: str = "", labels: list[str] | None = None, **_: Any
    ) -> FakeIssue:
        n = self._next()
        issue = FakeIssue(
            number=n, title=title, body=body, labels=[FakeLabel(name) for name in (labels or [])]
        )
        issue.html_url = f"{self.html_url}/issues/{n}"
        self.issues[n] = issue
        return issue

    def get_issue(self, number: int) -> FakeIssue:
        if number in self.issues:
            return self.issues[number]
        if number in self.pulls:
            return self.pulls[number]
        raise KeyError(number)

    def get_issues(self, state: str = "open", labels: list[Any] | None = None, **_: Any):
        want = {getattr(lb, "name", lb) for lb in (labels or [])}
        return [
            i
            for i in self.issues.values()
            if (state == "all" or i.state == state)
            and (not want or want <= {lb.name for lb in i.labels})
        ]

    def get_label(self, name: str) -> FakeLabel:
        if name not in self.labels:
            raise KeyError(name)
        return self.labels[name]

    def create_label(self, name: str, color: str = "ededed", description: str = "") -> FakeLabel:
        self.labels[name] = FakeLabel(name)
        return self.labels[name]

    # pulls -------------------------------------------------------------------------
    def create_pull(self, title: str, body: str, head: str, base: str, **_: Any) -> FakePull:
        n = self._next()
        pr = FakePull(number=n, title=title, body=body, head=head, base=base)
        pr.html_url = f"{self.html_url}/pull/{n}"
        self.pulls[n] = pr
        return pr

    def get_pull(self, number: int) -> FakePull:
        return self.pulls[number]

    # branches / contents -------------------------------------------------------------
    def get_branch(self, name: str) -> FakeBranch:
        if name not in self.branches:
            raise KeyError(name)
        return self.branches[name]

    def create_git_ref(self, ref: str, sha: str) -> None:
        self.branches[ref.replace("refs/heads/", "")] = FakeBranch(ref.replace("refs/heads/", ""))

    def get_contents(self, path: str, ref: str | None = None):
        if path not in self.files:
            from github import GithubException

            raise GithubException(404, {"message": "Not Found"}, None)
        content = self.files[path]
        return type("Contents", (), {"decoded_content": content.encode(), "path": path})()


class FakeGithub:
    def __init__(self, app: FakeGitHubApp, installation_id: int):
        self.app = app
        self.installation_id = installation_id

    def get_repo(self, full_name: str) -> FakeRepo:
        return self.app.repo(full_name)


class FakeGitHubApp(GitHubApp):
    """Drop-in for ``GitHubApp`` with in-memory repos."""

    def __init__(self):
        super().__init__(app_id="1", private_key="fake", base_url="https://api.github.com")
        self.repos: dict[str, FakeRepo] = {}
        self.tokens_issued: list[int] = []

    @property
    def configured(self) -> bool:
        return True

    def repo(self, full_name: str) -> FakeRepo:
        if full_name not in self.repos:
            self.repos[full_name] = FakeRepo(full_name, repo_id=abs(hash(full_name)) % 10_000_000)
        return self.repos[full_name]

    def installation_token(self, installation_id: int) -> str:
        self.tokens_issued.append(installation_id)
        return f"ghs_fake_{installation_id}"

    def installation_client(self, installation_id: int):
        return FakeGithub(self, installation_id)

    def clone_url(self, installation_id: int, full_name: str) -> str:
        return f"https://x-access-token:{self.installation_token(installation_id)}@github.com/{full_name}.git"
