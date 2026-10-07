"""GitHub PR source for mining, via the `gh` CLI.

Read-only: runs `gh pr list` / `gh issue view` and local `git cat-file` / `git diff`; never
fetches. Anything that goes wrong listing PRs raises `GhUnavailable` with a short reason, and the
caller falls back to the local source (a failing `gh issue view` only drops that issue's text). The file split always comes from the local diff.
"""

import json
import re
import subprocess
from dataclasses import replace
from pathlib import Path

from crucible.mine import candidates as cd
from crucible.mine.config import MineConfig

PR_FIELDS = ["number", "title", "url", "mergeCommit", "headRefName", "labels", "files",
             "additions", "deletions", "mergedAt", "body", "closingIssuesReferences", "baseRefName"]
FILES_CAP = 100  # `gh pr list` returns at most this many files per PR
GITHUB_URL = re.compile(
    r"(?:[A-Za-z][A-Za-z0-9+.-]*://)?(?:[^/@\s]+@)?github\.com[:/]+([^/\s]+)/([^/\s]+?)(?:\.git)?/?\Z")
NOT_FETCHED = "merge commit not fetched; run `git fetch`"
REBASE = "rebase merge: diff doesn't match PR"


class GhUnavailable(RuntimeError):
    """`gh` can't be used here; the message says why (one line)."""


def github_repo(repo: Path) -> tuple[str, str]:
    """(remote name, `owner/name`) of the first remote whose URL is github.com (`origin`
    first, then alphabetical)."""
    try:
        names = cd._git(repo, "remote").decode().split()
    except cd.MineGitError as e:
        raise GhUnavailable(f"cannot list git remotes: {e}") from e
    for name in sorted(names, key=lambda n: (n != "origin", n)):
        try:
            url = cd._git(repo, "remote", "get-url", name).decode().strip()
        except cd.MineGitError:
            continue
        m = GITHUB_URL.match(url)
        if m:
            return name, f"{m.group(1)}/{m.group(2)}"
    raise GhUnavailable("no GitHub remote")


def _gh(*args: str) -> str:
    try:
        proc = subprocess.run(["gh", *args], capture_output=True, text=True, check=False)
    except FileNotFoundError as e:
        raise GhUnavailable("gh not found on PATH") from e
    if proc.returncode != 0:
        err = next((ln.strip() for ln in proc.stderr.splitlines() if ln.strip()), "")
        raise _GhFailed(f"gh {' '.join(args[:2])} failed (exit {proc.returncode})"
                        + (f": {err}" if err else ""), proc.stderr)
    return proc.stdout


class _GhFailed(GhUnavailable):
    def __init__(self, msg: str, stderr: str):
        super().__init__(msg)
        self.stderr = stderr


def _json(text: str, what: str):
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise GhUnavailable(f"{what} returned invalid JSON") from e


def list_prs(slug: str, limit: int) -> list[dict]:
    """Merged PRs, newest first. A field this `gh` doesn't support is dropped and retried."""
    fields = list(PR_FIELDS)
    while True:
        try:
            out = _gh("pr", "list", "--repo", slug, "--state", "merged", "--limit", str(limit),
                      "--json", ",".join(fields))
            break
        except _GhFailed as e:
            m = re.search(r'Unknown JSON field: "([^"]+)"', e.stderr)
            if not (m and m.group(1) in fields and len(fields) > 1):
                raise
            fields.remove(m.group(1))
    data = _json(out, "gh pr list")
    if not isinstance(data, list):
        raise GhUnavailable("gh pr list returned unexpected JSON")
    return data


def sort_newest_first(prs: list[dict]) -> list[dict]:
    """By `mergedAt` descending; PRs without it keep `gh` order, after the dated ones."""
    return sorted(prs, key=lambda p: p.get("mergedAt") or "", reverse=True)


def _has_commit(repo: Path, sha: str) -> bool:
    try:
        cd._git(repo, "cat-file", "-e", f"{sha}^{{commit}}")
        return True
    except cd.MineGitError:
        return False


def _first_parent(repo: Path, sha: str) -> str:
    out = cd._git(repo, "rev-list", "--parents", "-n", "1", sha).decode().split()
    return out[1] if len(out) > 1 else ""


def _diff_names(repo: Path, base: str, merge: str) -> set[str]:
    raw = cd._git(repo, "diff", "--name-only", "-z", "-M", base, merge).decode("utf-8", "replace")
    return {p for p in raw.split("\0") if p}


def _issue_refs(pr: dict) -> list[dict]:
    refs = []
    for r in pr.get("closingIssuesReferences") or []:
        ref = {"number": r.get("number"), "url": r.get("url")}
        repo = r.get("repository") or {}
        owner = (repo.get("owner") or {}).get("login")
        if owner and repo.get("name"):
            ref["repo"] = f"{owner}/{repo['name']}"
        refs.append({k: v for k, v in ref.items() if v is not None})
    return refs


def _files_agree(repo: Path, base: str, sha: str, files: list[dict]) -> bool:
    """The local diff names exactly the PR's files; a list cut off at FILES_CAP only has to be
    contained in the local diff."""
    pr_files = {f.get("path") for f in files}
    local = _diff_names(repo, base, sha)
    return local >= pr_files if len(files) >= FILES_CAP else local == pr_files


def pr_candidates(
    repo: Path, cfg: MineConfig, limit: int = cd.DEFAULT_LIMIT, branch: str | None = None
) -> tuple[list[cd.Candidate], list[cd.Excluded], str]:
    """(candidates to filter, candidates already excluded with a reason, `owner/name`), newest
    merge first. With `branch`, only PRs whose base branch is `branch` (all PRs otherwise)."""
    _, slug = github_repo(repo)
    found, excluded = [], []
    for pr in sort_newest_first(list_prs(slug, limit)):
        if branch and pr.get("baseRefName") not in (None, branch):
            continue
        sha = (pr.get("mergeCommit") or {}).get("oid")
        if not sha:
            continue
        extra = {
            "id": str(pr["number"]), "title": pr.get("title") or "", "merge": sha,
            "source": pr.get("url") or f"commit:{sha}",
            "labels": [lb["name"] for lb in pr.get("labels") or [] if lb.get("name")],
            "branch": pr.get("headRefName"), "url": pr.get("url"), "issues": _issue_refs(pr),
            "body": (pr.get("body") or "").strip(),
        }
        if not _has_commit(repo, sha):
            excluded.append(cd.Excluded(cd.make_candidate(repo, cfg, base="", **extra), NOT_FETCHED))
            continue
        base = _first_parent(repo, sha)
        cand = cd.make_candidate(repo, cfg, base=base, **extra)
        if base and "files" in pr and not _files_agree(repo, base, sha, pr["files"] or []):
            excluded.append(cd.Excluded(cand, REBASE))
        else:
            found.append(cand)
    return found, excluded, slug


def with_issue_details(cands: list[cd.Candidate], slug: str) -> list[cd.Candidate]:
    """Fill each linked issue's title and body (`gh issue view`)."""
    out = []
    for c in cands:
        issues = []
        for ref in c.issues:
            if "title" in ref:
                issues.append(ref)
                continue
            try:
                data = _json(_gh("issue", "view", str(ref["number"]), "--repo",
                                 ref.get("repo", slug), "--json", "number,title,body,url"),
                             "gh issue view")
            except GhUnavailable as e:  # deleted or transferred issue: keep the bare reference
                issues.append({**ref, "error": str(e)})
                continue
            issues.append({**ref, "title": data.get("title", ""), "body": data.get("body") or ""})
        out.append(replace(c, issues=issues))
    return out
