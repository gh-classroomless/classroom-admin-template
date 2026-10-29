"""hub_common — shared pieces of the read-only local report tools (`log_build.py`, `repo_report.py`).

Self-contained: Python's standard library and a GitHub token, nothing else. Clone your
`<org>/classroom-admin`, and the tools run from that clone on any machine.

The tools READ the clone and the GitHub API. They write only inside the clone's `.git/` and an
excluded `local-reports/` folder, and never commit, push or close an issue: this repo's Actions
stay the only writers of `log/`, `reports/`, `roster.md`, `repo-report.md`.

    .git/hub-local/pending/<repo>.json   log: issues not yet drained by the nightly log-build
    .git/hub-local/scores.json           {repo: {run_id, result, score}} — a finished run is read once
    local-reports/<course>[-<CODE>]-repo-report.md

Token: `$GH_TOKEN` or `$GITHUB_TOKEN`, else `gh auth token`. It must read this private repo and
the org's private student repos (a classic PAT with `repo`, or your `gh` login).
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

API = "https://api.github.com"
LOG_PREFIX = "log: "
CLONE = Path(__file__).resolve().parents[2]          # <clone>/.github/scripts/hub_common.py


def token():
    t = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not t:
        try:
            t = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True).stdout.strip()
        except FileNotFoundError:
            t = ""
    if not t:
        raise SystemExit("no GitHub token: set GH_TOKEN, or log in with `gh auth login`")
    return t


def _request(url, tok, raw=False, retries=3):
    """(status, body, link) for one GET. 404 → (404, None, ''); never raises on HTTP errors."""
    headers = {"Authorization": f"Bearer {tok}", "Accept": "application/vnd.github+json",
               "X-GitHub-Api-Version": "2022-11-28"}
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=60) as r:
                body = r.read()
                return r.status, (body if raw else json.loads(body or b"null")), r.headers.get("Link", "")
        except urllib.error.HTTPError as e:
            if e.code in (429, 502, 503, 504) and attempt + 1 < retries:
                time.sleep(2 * (attempt + 1))
                continue
            return e.code, None, ""
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            if attempt + 1 < retries:
                time.sleep(1 + attempt)
                continue
            return 0, None, ""
    return 0, None, ""


def get(tok, path, paginate=True):
    """GET an API path; a list is followed through every `Link: next` page. None on failure."""
    url = path if path.startswith("http") else API + path
    status, data, link = _request(url, tok)
    if data is None:
        return None
    if not (paginate and isinstance(data, list)):
        return data
    items = list(data)
    while True:
        m = re.search(r'<([^>]+)>;\s*rel="next"', link or "")
        if not m:
            return items
        status, data, link = _request(m.group(1), tok)
        if data is None:
            return None
        items.extend(data)


def get_bytes(tok, path):
    """Raw bytes of an API path (a run-log zip), or None."""
    status, body, _ = _request(API + path, tok, raw=True)
    return body if status == 200 and body else None


def admin_repo(clone=CLONE):
    """'<org>/<hub>-admin' from the clone's origin URL."""
    url = subprocess.run(["git", "-C", str(clone), "remote", "get-url", "origin"],
                         capture_output=True, text=True).stdout.strip()
    m = re.search(r"github\.com[:/]([^/]+)/([^/]+?)(?:\.git)?$", url)
    if not m or not m.group(2).endswith("-admin"):
        raise SystemExit(f"{clone} is not a clone of <org>/<hub>-admin (origin: {url or 'none'})")
    return f"{m.group(1)}/{m.group(2)}"


def local_dir(clone=CLONE):
    d = Path(clone) / ".git" / "hub-local"
    d.mkdir(parents=True, exist_ok=True)
    return d


def pending_dir(clone=CLONE):
    return local_dir(clone) / "pending"


def is_record(obj):
    """The validity test the log-build Action applies to a `log:` issue body."""
    return isinstance(obj, dict) and all(obj.get(k) for k in ("repo", "username", "course", "assignment"))


def current_records(clone=CLONE):
    """[(record, 'log'|'pending')] — top-level log/*.json (the current term), then the pending set."""
    out, seen = [], set()
    for f in sorted((Path(clone) / "log").glob("*.json")):
        try:
            rec = json.loads(f.read_text())
        except (OSError, ValueError):
            continue
        if is_record(rec):
            out.append((rec, "log"))
            seen.add(rec["repo"])
    if pending_dir(clone).is_dir():
        for f in sorted(pending_dir(clone).glob("*.json")):
            rec = json.loads(f.read_text())
            if is_record(rec) and rec["repo"] not in seen:
                out.append((rec, "pending"))
    return out


def exclude_local_reports(clone=CLONE):
    """Keep `local-reports/` out of `git status` (.git/info/exclude is local and never pushed)."""
    ex = Path(clone) / ".git" / "info" / "exclude"
    ex.parent.mkdir(parents=True, exist_ok=True)
    text = ex.read_text() if ex.exists() else ""
    if "local-reports/" not in text.split():
        ex.write_text(text + ("\n" if text and not text.endswith("\n") else "") + "local-reports/\n")
