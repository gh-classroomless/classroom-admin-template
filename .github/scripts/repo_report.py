#!/usr/bin/env python3
"""repo_report.py — the read-only local twin of this repo's `repo-report` Action, per course.

Run from anywhere; it works on the clone it sits in. Writes nothing to GitHub.

1. Runs `log_build.build()` first (git pull + the open `log:` issues), unless `--no-log-build`.
2. Takes that course's current-term records: `log/*.json` plus the pending set.
3. `--results` decides the Result / Score columns, with the Action's own rules:
     none     (default) no API call; the columns stay empty
     changed  ask only repos pushed since this course's last check, or never stored; every other
              repo shows its stored result from `local/scores.json`
     all      ask every repo; a deleted repo (HTTP 404) is dropped from the tables
   Asking = the repo's latest PUSH run → ✅ pass / ❌ fail / — no run, and the score from the
   run-log zip (the last "totalPoints" / "maxPoints"), read only when the run id or attempt
   changed. A result is stored only when its run is finished AND is the run of the latest push.
4. Writes `local/reports/<course>[-<CODE>]-repo-report.md` and the same rows as `.html` beside it
   (search box, green / red results, repo links). One table per assignment, `Username | Name |
   Result | Score | Repo`, last-name order. A pending row is marked `(pending)`. A rerun
   overwrites both files.
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


CUTOFF_MARGIN = 1800      # a course's cutoff = its last check minus 30 min (a run created late)


def epoch(iso):
    return datetime.fromisoformat((iso or "1970-01-01T00:00:00Z").replace("Z", "+00:00")).timestamp()


def label(conclusion):
    return {"success": "✅ pass", "failure": "❌ fail"}.get(conclusion, f"… {conclusion}")


def load_store(path):
    """{checked_at: {course: iso}, repos: {repo: {run_id, run_attempt, created_at, result, score}}}"""
    try:
        s = json.loads(path.read_text())
    except (OSError, ValueError):
        s = None
    return s if isinstance(s, dict) and "repos" in s and "checked_at" in s else {"checked_at": {}, "repos": {}}


def org_pushed_since(tok, org, cutoff):
    """{repo: pushed_at epoch} for the org's repos pushed at/after `cutoff`, newest first, 100 a page."""
    status, data, _ = hc._request(f"{hc.API}/orgs/{org}/repos?type=private&per_page=1", tok)
    if not data:   # a token that cannot see private repos would list none and miss every push
        raise SystemExit(f"the token cannot list {org}'s private repos (HTTP {status}) — --results changed cannot run")
    pushed, page = {}, 1
    while True:
        status, data, _ = hc._request(f"{hc.API}/orgs/{org}/repos?sort=pushed&direction=desc"
                                      f"&per_page=100&page={page}", tok)
        if data is None:
            raise SystemExit(f"could not list {org}'s repos (page {page}, HTTP {status})")
        if not data:
            return pushed
        for r in data:
            pushed[r["name"]] = epoch(r.get("pushed_at"))
        if epoch(data[-1].get("pushed_at")) < cutoff:
            return pushed
        page += 1


def ask(tok, org, repo, stored):
    """The repo's latest push run: {kind: ok|norun|gone|err, …}. The log zip is read only when the
    run id or attempt differs from `stored`."""
    status, runs, _ = hc._request(f"{hc.API}/repos/{org}/{repo}/actions/runs?event=push&per_page=1", tok)
    if status == 404:
        return {"kind": "gone"}
    if runs is None:
        return {"kind": "err", "status": status}
    run = (runs.get("workflow_runs") or [None])[0]
    if not run:
        return {"kind": "norun"}
    a = {"kind": "ok", "run_id": run["id"], "run_attempt": run.get("run_attempt"),
         "created_at": run.get("created_at"), "conclusion": run.get("conclusion"), "score": "—"}
    if a["conclusion"]:
        if stored and stored.get("run_id") == a["run_id"] and stored.get("run_attempt") == a["run_attempt"]:
            a["score"] = stored.get("score", "—")
        else:
            blob = hc.get_bytes(tok, f"/repos/{org}/{repo}/actions/runs/{a['run_id']}/logs")
            a["score"] = score_from_zip(blob) if blob else "—"
    return a


def results_for(mode, tok, org, course, scope, store, update_course):
    """{repo: (result, score, drop)} for `scope`, and `store` updated in place — the Action's rules."""
    pushed, cut = {}, 0
    if mode == "all":
        to_ask = list(scope)
    else:
        last = store["checked_at"].get(course)
        cut = epoch(last) - CUTOFF_MARGIN if last else 0
        if cut > 0:
            pushed = org_pushed_since(tok, org, cut)
        to_ask = [r for r in scope if r not in store["repos"] or cut == 0 or pushed.get(r, 0) >= cut]
    print(f"results={mode}: asking {len(to_ask)} of {len(scope)} repos")
    with cf.ThreadPoolExecutor(max_workers=WORKERS) as ex:
        answers = dict(zip(to_ask, ex.map(lambda r: ask(tok, org, r, store["repos"].get(r)), to_ask)))
    for repo, a in answers.items():
        if a["kind"] in ("gone", "err"):
            store["repos"].pop(repo, None)
        elif a["kind"] == "norun":
            store["repos"][repo] = {"run_id": None, "run_attempt": None, "result": "— no run", "score": "—"}
        elif not a["conclusion"] or pushed.get(repo, 0) > epoch(a["created_at"]):
            store["repos"].pop(repo, None)       # unfinished, or not the latest push's run: ask again
        else:
            store["repos"][repo] = {"run_id": a["run_id"], "run_attempt": a["run_attempt"],
                                    "created_at": a["created_at"], "result": label(a["conclusion"]),
                                    "score": a["score"]}
    if update_course:
        store["checked_at"][course] = datetime.now(ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M:%SZ")
    out = {}
    for repo in scope:
        a, s = answers.get(repo), store["repos"].get(repo)
        if a and a["kind"] == "gone":
            out[repo] = ("", "", True)
        elif a and a["kind"] == "err":
            out[repo] = (f"⚠ unreadable (HTTP {a['status']})", "—", False)
        elif a and a["kind"] == "ok" and not a["conclusion"]:
            out[repo] = ("… in progress", "—", False)
        elif a and a["kind"] == "ok" and pushed.get(repo, 0) > epoch(a["created_at"]):
            out[repo] = ("… new push, run not started", "—", False)
        elif s:
            out[repo] = (s["result"], s["score"], False)
        else:
            out[repo] = ("", "", False)
    return out


def last_name_key(name):
    parts = (name or "").split()
    return ((parts[-1] if parts else "").lower(), (name or "").lower())


class _Help(argparse.RawDescriptionHelpFormatter):
    pass


EXAMPLES = """examples:
  python3 .github/scripts/repo_report.py --course cs122                        # repo list only, no API
  python3 .github/scripts/repo_report.py --course cs122 --results changed      # re-check pushed repos
  python3 .github/scripts/repo_report.py --course cs122 --results all          # re-check every repo
  python3 .github/scripts/repo_report.py --course cs122 --assignment W25 --results all

output: local/reports/<course>[-<CODE>]-repo-report.md + .html (open the .html in a browser)"""


def main():
    ap = argparse.ArgumentParser(description=__doc__, epilog=EXAMPLES, formatter_class=_Help)
    ap.add_argument("--course", required=True, help="REQUIRED. Course key in the log records, e.g. cs122 "
                                                    "(case-insensitive)")
    ap.add_argument("--assignment", default=None, help="one assignment code only (default: every assignment)")
    ap.add_argument("--results", choices=["none", "changed", "all"], default="none",
                    help="fill Result / Score: none = no API · changed = repos pushed since the last check · "
                         "all = every repo (default: none)")
    ap.add_argument("--no-log-build", action="store_true", help="skip git pull + reading the open log: issues "
                                                                "(default: do both first)")
    ap.add_argument("--out", default=None, help="the .md file (default: local/reports/<course>[-<CODE>]-repo-report.md;"
                                                " the .html goes beside it)")
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

    scope = [r["repo"] for r, _ in recs]
    if a.results == "none":
        shown = {repo: ("", "", False) for repo in scope}
    else:
        store_file = hc.local_dir(clone) / "scores.json"
        store = load_store(store_file)
        # one assignment does not check the whole course, so the course's checked_at stays
        shown = results_for(a.results, hc.token(), org, course, scope, store, update_course=not a.assignment)
        store_file.write_text(json.dumps(store, indent=0, sort_keys=True))
    recs = [(r, s) for r, s in recs if not shown[r["repo"]][2]]
    res = {repo: {"result": v[0], "score": v[1]} for repo, v in shown.items()}

    stamp = datetime.now(PT).strftime("%Y-%m-%d %I:%M %p %Z")
    by_asmt = {}
    for r, src in recs:
        by_asmt.setdefault(r["assignment"], []).append((r, src))
    lines = [f"# {course}{' / ' + a.assignment if a.assignment else ''} — repo report (local)", "",
             f"_Generated {stamp} (results={a.results}) from `{org}/classroom-admin` log/ + open log: "
             f"issues · {len(recs)} repos · {time.time() - t0:.1f}s_", "",
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
    stem = f"{course}-{a.assignment}" if a.assignment else course
    out = Path(a.out) if a.out else hc.local_dir(clone) / "reports" / f"{stem}-repo-report.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines) + "\n")
    out_html = out.with_suffix(".html")
    out_html.write_text(render_html(lines[0][2:], f"Generated {stamp} (results={a.results}) from {org}/classroom-admin log/ + "
                                    f"open log: issues · {len(recs)} repos · {time.time() - t0:.1f}s",
                                    by_asmt, res, org))
    n_pass = sum(v["result"] == "✅ pass" for v in res.values())
    print(f"{course}: {len(recs)} repos · {n_pass} pass · {len(by_asmt)} assignment(s) · "
          f"{time.time() - t0:.1f}s\n→ {out}\n→ {out_html}")


_STYLE = """
body{font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;margin:24px;color:#1f2328}
h1{font-size:22px;margin:0 0 4px} h2{font-size:17px;margin:28px 0 6px}
.meta{color:#59636e;font-size:13px;margin-bottom:14px}
#q{width:340px;padding:7px 10px;font-size:14px;border:1px solid #d0d7de;border-radius:6px}
table{border-collapse:collapse;font-size:13px;margin-top:6px}
th,td{border:1px solid #d0d7de;padding:4px 10px;text-align:left}
th{background:#f6f8fa} .pass{color:#1a7f37;font-weight:600} .fail{color:#cf222e;font-weight:600}
.other{color:#59636e} .pending{color:#9a6700;font-size:12px}
a{color:#0969da;text-decoration:none} a:hover{text-decoration:underline}
"""

_SEARCH = """
document.getElementById('q').addEventListener('input', function () {
  var q = this.value.trim().toLowerCase();
  document.querySelectorAll('section').forEach(function (sec) {
    var shown = 0;
    sec.querySelectorAll('tbody tr').forEach(function (tr) {
      var hit = !q || tr.textContent.toLowerCase().indexOf(q) >= 0;
      tr.style.display = hit ? '' : 'none';
      if (hit) shown++;
    });
    sec.style.display = shown ? '' : 'none';
  });
});
"""


def render_html(title, meta, by_asmt, res, org):
    """The same rows as the .md, as one self-contained page: a search box that keeps only the rows
    containing the typed text (any column), green/red results, repo names linked to GitHub."""
    from html import escape as e

    def cls(result):
        return "pass" if result == "✅ pass" else "fail" if result == "❌ fail" else "other"
    out = [f"<!doctype html><html><head><meta charset='utf-8'><title>{e(title)}</title>"
           f"<style>{_STYLE}</style></head><body>", f"<h1>{e(title)}</h1>", f"<div class='meta'>{e(meta)}</div>",
           "<input id='q' placeholder='Search name, username, repo, pass / fail …' autofocus>",
           "<table><thead><tr><th>Assignment</th><th>Repos</th><th>Pass</th></tr></thead><tbody>"]
    for asmt in sorted(by_asmt):
        rows = by_asmt[asmt]
        n = sum(res[r["repo"]]["result"] == "✅ pass" for r, _ in rows)
        out.append(f"<tr><td><a href='#{e(asmt)}'>{e(asmt)}</a></td><td>{len(rows)}</td><td>{n}</td></tr>")
    out.append("</tbody></table>")
    for asmt in sorted(by_asmt):
        out.append(f"<section id='{e(asmt)}'><h2>{e(asmt)}</h2><table><thead><tr><th>Username</th>"
                   "<th>Name</th><th>Result</th><th>Score</th><th>Repo</th></tr></thead><tbody>")
        for r, src in sorted(by_asmt[asmt], key=lambda rs: last_name_key(rs[0].get("name"))):
            v = res[r["repo"]]
            tag = " <span class='pending'>(pending)</span>" if src == "pending" else ""
            out.append(f"<tr><td>{e(r['username'])}</td><td>{e(r.get('name', ''))}</td>"
                       f"<td class='{cls(v['result'])}'>{e(v['result'])}</td><td>{e(v['score'])}</td>"
                       f"<td><a href='https://github.com/{e(org)}/{e(r['repo'])}' target='_blank' "
                       f"rel='noopener'>{e(r['repo'])}</a>{tag}</td></tr>")
        out.append("</tbody></table></section>")
    out.append(f"<script>{_SEARCH}</script></body></html>")
    return "\n".join(out) + "\n"


if __name__ == "__main__":
    main()
