#!/usr/bin/env python3
"""log_build.py — the read-only local twin of this repo's `log-build` Action.

    python3 .github/scripts/log_build.py [--no-pull]

Run from anywhere; it works on the clone it sits in.

1. `git pull --ff-only` — the clone gets what the nightly Action last pushed.
2. Read the OPEN `log:` issues (one per repo `request.yml` provisioned) — repos made since the last
   nightly drain, e.g. in class today.
3. Validate each body as the Action does (repo, username, course, assignment). A repo already in
   `log/` is skipped; the rest replace `local/pending/` (`local/` is in `.gitignore`).

It never commits, pushes or closes an issue — the Action stays the one writer of `log/` and the one
closer of `log:` issues. `repo_report.py` reads `log/` plus this pending set.
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import hub_common as hc  # noqa: E402


def build(clone=hc.CLONE, pull=True, quiet=False):
    """Refresh the clone and the pending set. Returns {issues, pending, already, bad}."""
    clone = Path(clone)
    admin = hc.admin_repo(clone)
    if pull:
        p = subprocess.run(["git", "-C", str(clone), "pull", "--ff-only", "-q"],
                           capture_output=True, text=True)
        if p.returncode != 0:
            raise SystemExit(f"git pull failed in {clone}: {p.stderr.strip()}")
    issues = hc.get(hc.token(), f"/repos/{admin}/issues?state=open&per_page=100")
    if issues is None:
        raise SystemExit(f"could not list the open issues of {admin}")
    logs = [i for i in issues if not i.get("pull_request")
            and (i.get("title") or "").startswith(hc.LOG_PREFIX)]
    pend = hc.pending_dir(clone)
    shutil.rmtree(pend, ignore_errors=True)
    pend.mkdir(parents=True)
    n = {"issues": len(logs), "pending": 0, "already": 0, "bad": 0}
    for i in logs:
        try:
            rec = json.loads(i.get("body") or "")
        except ValueError:
            rec = None
        if not hc.is_record(rec):
            n["bad"] += 1
            if not quiet:
                print(f"  ⚠ issue #{i['number']}: body is not a log record — skipped")
            continue
        if (clone / "log" / f"{rec['repo']}.json").exists():
            n["already"] += 1
            continue
        (pend / f"{rec['repo']}.json").write_text(json.dumps(rec, indent=1, sort_keys=True))
        n["pending"] += 1
    return n


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
                                 epilog="example:\n  python3 .github/scripts/log_build.py\n\n"
                                        "output: local/pending/<repo>.json (read by repo_report.py)")
    ap.add_argument("--no-pull", action="store_true", help="skip git pull (default: pull first)")
    a = ap.parse_args()
    t0 = time.time()
    n = build(pull=not a.no_pull)
    print(f"log: issues open {n['issues']} · new (pending) {n['pending']} · already in log/ "
          f"{n['already']} · bad {n['bad']}  ({time.time() - t0:.1f}s)")


if __name__ == "__main__":
    main()
