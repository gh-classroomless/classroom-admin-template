# gh-classroomless — Classroom Hub (admin · PRIVATE)

> ⛔ **PRIVATE — never make this repo public.** It stores PII: `roster/<username>.json` (student name / email / registered) and `log/<repo>.json` (per-repo provisioning records). Written by the `gh-classroomless/classroom` workflows via `ORG_PAT`.

## What's here
- **This page** = live submission status per course (autograder **pass/fail + score**). Click **open** for a per-assignment detail.
- [`roster.md`](roster.md) — student roster per course; each name links to that student's repo list.
- `reports/` — generated detail pages · `log/`, `roster/` — raw PII data (do not share).

## Refresh (manual, per course)
- **Actions → `repo-report` → Run workflow → enter a course** (e.g. `cs120`; case-insensitive) — rebuilds THAT course's pass/fail + score.
- **Actions → `roster` → Run workflow → enter a course** — rebuilds that course's roster.
- **Past term**: enter `semester` (e.g. `sp26`) — reads archived `log/archive/<term>/` into `repo-report-<term>.md` (current term needs no semester; never overwrites this page).
- Per-course on purpose: only the course you run is rebuilt (no all-course overhead).

## How to read the status
- **Result**: ✅ pass / ❌ fail = the latest push run's autograder conclusion.
- **Score**: autograder `totalPoints/maxPoints` parsed from the run log.

_Status generated 2026-09-04 09:21 AM PDT · total repos: **0**._

---
# Submission status
