#!/usr/bin/env python3
"""repo_report.py — the read-only local twin of this repo's `repo-report` Action, per course.

    python3 .github/scripts/repo_report.py --course <course> [--assignment <CODE>]
                                           [--no-log-build] [--out <file.md>]

Run from anywhere; it works on the clone it sits in.

1. Runs `log_build.build()` first (git pull + the open `log:` issues), unless `--no-log-build`.
2. Takes that course's current-term records: `log/*.json` plus the pending set.
3. For each repo: its latest PUSH run (the Action's own query) → ✅ pass / ❌ fail / — no run, and
   the score from the run-log zip, read the way the Action reads it (the last `"totalPoints"` and
   `"maxPoints"` in the log). A FINISHED run's result is cached by run id in
   `.git/hub-local/scores.json`; the next report re-reads only repos with a new run.
4. Writes `local-reports/<course>-repo-report.md`, or `<course>-<CODE>-repo-report.md` with
   `--assignment` (excluded from git): one table per assignment, `Username | Name | Result | Score
   | Repo`, last-name order — the Action's detail layout. A row still pending is marked `(pending)`.

Writes nothing to GitHub.
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import io
import json
import re
import sys
import time
import zipfile
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))
import hub_common as hc  # noqa: E402
import log_build  # noqa: E402

WORKERS = 32
PT = ZoneInfo("America/Los_Angeles")
_TP = re.compile(r'"totalPoints": *(\d+)')
_MP = re.compile(r'"maxPoints": *(\d+)')


def score_from_zip(blob):
    """'earned/max' from a run-log zip — the LAST match of each, as the Action's grep|tail does."""
    try:
        z = zipfile.ZipFile(io.BytesIO(blob))
        text = "".join(z.read(n).decode("utf-8", "replace") for n in z.namelist())
    except zipfile.BadZipFile:
        return "—"
    tp, mp = _TP.findall(text), _MP.findall(text)
    return f"{tp[-1]}/{mp[-1]}" if tp and mp else "—"


def result_of(tok, org, repo, cache):
    """{run_id, result, score, final} for the repo's latest push run; a finished run from `cache`."""
    status, runs, _ = hc._request(f"{hc.API}/repos/{org}/{repo}/actions/runs?event=push&per_page=1", tok)
    if status == 404:       # the repo was deleted (or the token cannot see it)
        return {"run_id": None, "result": "⛔ repo not found", "score": "—"}
    if runs is None:
        return {"run_id": None, "result": f"⚠ unreadable (HTTP {status})", "score": "—"}
    run = (runs.get("workflow_runs") or [None])[0]
    if not run:
        return {"run_id": None, "result": "— no run", "score": "—"}
    rid, concl = run["id"], run.get("conclusion")
    hit = cache.get(repo)
    if concl and hit and hit.get("run_id") == rid:
        return hit
    result = {"success": "✅ pass", "failure": "❌ fail"}.get(concl, f"… {concl or run.get('status')}")
    blob = hc.get_bytes(tok, f"/repos/{org}/{repo}/actions/runs/{rid}/logs")
    return {"run_id": rid, "result": result, "score": score_from_zip(blob) if blob else "—",
            "final": bool(concl)}


def last_name_key(name):
    parts = (name or "").split()
    return ((parts[-1] if parts else "").lower(), (name or "").lower())


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--course", required=True, help="course key in the log records, e.g. cs122 (case-insensitive)")
    ap.add_argument("--assignment", help="one assignment code only")
    ap.add_argument("--no-log-build", action="store_true", help="skip git pull + the open log: issues")
    ap.add_argument("--out", help="output file (default: local-reports/<course>[-<CODE>]-repo-report.md)")
    a = ap.parse_args()
    t0 = time.time()
    clone = hc.CLONE
    org = hc.admin_repo(clone).split("/")[0]
    if not a.no_log_build:
        n = log_build.build(clone, quiet=True)
        print(f"log-build (local): {n['pending']} pending · {n['already']} open issue(s) already in log/")

    course = a.course.lower()
    recs_all = hc.current_records(clone)
    recs = [(r, s) for r, s in recs_all if r["course"].lower() == course
            and (not a.assignment or r["assignment"] == a.assignment)]
    if not recs:
        courses = " ".join(sorted({r["course"].lower() for r, _ in recs_all}))
        raise SystemExit(f"no current-term record for course={course}"
                         f"{' assignment=' + a.assignment if a.assignment else ''}. Courses: {courses}")

    cache_file = hc.local_dir(clone) / "scores.json"
    cache = json.loads(cache_file.read_text()) if cache_file.exists() else {}
    tok = hc.token()
    with cf.ThreadPoolExecutor(max_workers=WORKERS) as ex:
        res = dict(zip([r["repo"] for r, _ in recs],
                       ex.map(lambda rs: result_of(tok, org, rs[0]["repo"], cache), recs)))
    for repo, v in res.items():
        if v.get("final"):
            cache[repo] = v
    cache_file.write_text(json.dumps(cache, indent=0, sort_keys=True))

    stamp = datetime.now(PT).strftime("%Y-%m-%d %I:%M %p %Z")
    by_asmt = {}
    for r, src in recs:
        by_asmt.setdefault(r["assignment"], []).append((r, src))
    lines = [f"# {course}{' / ' + a.assignment if a.assignment else ''} — repo report (local)", "",
             f"_Generated {stamp} from `{org}/classroom-admin` log/ + open log: issues · "
             f"{len(recs)} repos · {time.time() - t0:.1f}s_", "",
             "| Assignment | Repos | Pass |", "|---|---|---|"]
    for asmt in sorted(by_asmt):
        rows = by_asmt[asmt]
        lines.append(f"| [{asmt}](#{asmt.lower()}) | {len(rows)} | "
                     f"{sum(res[r['repo']]['result'] == '✅ pass' for r, _ in rows)} |")
    for asmt in sorted(by_asmt):
        lines += ["", f"## {asmt}", "", "| Username | Name | Result | Score | Repo |", "|---|---|---|---|---|"]
        for r, src in sorted(by_asmt[asmt], key=lambda rs: last_name_key(rs[0].get("name"))):
            v = res[r["repo"]]
            tag = " (pending)" if src == "pending" else ""
            lines.append(f"| {r['username']} | {r.get('name', '')} | {v['result']} | {v['score']} | "
                         f"[{r['repo']}](https://github.com/{org}/{r['repo']}){tag} |")
    hc.exclude_local_reports(clone)
    stem = f"{course}-{a.assignment}" if a.assignment else course
    out = Path(a.out) if a.out else clone / "local-reports" / f"{stem}-repo-report.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines) + "\n")
    n_pass = sum(v["result"] == "✅ pass" for v in res.values())
    print(f"{course}: {len(recs)} repos · {n_pass} pass · {len(by_asmt)} assignment(s) · "
          f"{time.time() - t0:.1f}s\n→ {out}")


if __name__ == "__main__":
    main()
