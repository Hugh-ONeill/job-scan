# job-scan

> A daily, resume-matched job scanner. Pulls fresh postings from company ATS boards and
> remote-job aggregators, scores each one against your resume with a **local LLM**, and
> delivers a ranked Markdown report + web dashboard — fully self-hosted, no API keys.

## Why

Job boards bury good matches in noise, and generic keyword alerts surface roles that don't
actually fit. job-scan reads each posting the way you would — weighing skills, seniority,
domain, and location against *your* resume — and only surfaces what clears a quality bar.
It runs itself on a timer and costs nothing to operate (scoring happens on a local GPU).

## What it does

- **Scrapes multiple sources** on a schedule:
  - ATS boards: **Greenhouse, Lever, Ashby, Workday, SmartRecruiters**
  - Aggregators: **Hacker News "Who is hiring?", RemoteOK, Remotive, We Work Remotely,
    freehire.me, LinkedIn** (public jobs-guest endpoints)
- **Cheap pre-filters** (title include/exclude, location) drop obvious misses before spending LLM time.
- **LLM matching** scores each posting 0–100 against your resume using a local model (Ollama / Gemma),
  via **decomposed sub-scores** combined deterministically (see [Scoring](#scoring)).
- **Deduplicates** against a local SQLite store so each run only surfaces genuinely new postings.
- **Reports**: a ranked daily Markdown digest + a web dashboard with application tracking.
- **Tracks applications past "applied"** — interview/offer/rejected stages, a quiet-day clock, and
  follow-up nudges (see [Application tracking](#application-tracking)).
- **Degrades without a GPU**: if Ollama is unreachable, a run collects and dedups postings unscored
  rather than failing, and `rescore --unscored-only` sweeps the backlog later.
- **Runs unattended** via systemd timers (daily scan + weekly sweep), network-aware on resume.

## How it works

```
ATS boards + aggregators ──▶ pre-filters (title/location) ──▶ LLM scorer (local Gemma)
                                                                      │
   SQLite (dedup + status) ◀── decomposed sub-scores ──▶ deterministic final score
                                                                      │
                                              ranked Markdown report + web dashboard
```

## Scoring

The LLM rates four independent dimensions; the final number is computed in Python so it's
transparent and tunable (no "trust the model's vibe" black box):

```
capability = skill_weight * skill_fit + (1 - skill_weight) * seniority_fit
base       = capability * (domain_floor + (1 - domain_floor) * domain_fit / 100)
final      = clamp(base + location_points[tier] + interview_points)
```

- **skill_fit / seniority_fit / domain_fit** (0–100) — rated by the model.
- **location_tier** (1–7) — remote / relocation-supported / commutable / neutral / unsupported.
- Posting **age** is a sort dimension, not a score input (so it doesn't saturate the top into a wall of 100s).

Weights and tiers live in [`config/settings.yaml`](config/settings.yaml).

## Quickstart

```bash
# 1. Install (uv)
uv sync

# 2. A local model via Ollama
ollama pull gemma4:26b-a4b-it-q4_K_M     # or any model; set it in settings.yaml

# 3. Point it at your resume + tune preferences
#    edit config/settings.yaml (resume_path, model, scoring, location)
#    edit config/companies.yaml (boards to scan) and config/filters.yaml (title/location rules)

# 4. Run a scan
job-scan run

# 5. Browse results
job-scan serve          # web dashboard at http://localhost:8000
```

## Application tracking

Finding a role is half the job; the other half is not losing track of what you sent. Statuses run
`pending → visited → applied → interview → offer` (plus `rejected`, `no_response`, `dismissed`), and
the dashboard's **Applications** panel is the pipeline view: one row per application with an inline
status dropdown, notes field, and a **quiet-day clock**.

The clock counts from the most recent real touch — the date you applied, your last logged follow-up,
or the last status change (a stage advancing means *they* contacted you). Past
`followup_after_days` (default 10) with fewer than two follow-ups sent, the row is flagged as needing
a nudge, in the dashboard and at the top of the daily report. Past 3× that, or after two silent
follow-ups, it's flagged as ghosted instead — the suggestion becomes recording `no_response`, since
a third chase isn't the honest move.

Nothing is sent for you. The tool tracks and reminds; the writing and sending stay yours.

## Configuration

| File | Purpose |
|------|---------|
| `config/settings.yaml` | resume path, model, scoring weights, location preferences, aggregators |
| `config/companies.yaml` | ATS boards to scan (priority + broader sweep) |
| `config/filters.yaml`   | pre-LLM title include/exclude + location rules |
| `config/gap_terms.yaml` | vocabulary for the `gaps` skill-gap rollup |

### LinkedIn source

The `linkedin` aggregator uses LinkedIn's public, unauthenticated `jobs-guest` endpoints, which fills
the gap the other sources leave: local non-remote roles at companies not in `companies.yaml`.
Automated access is against LinkedIn's ToS, so it's built for **personal use at low volume** — one
search page per query × location, detail pages fetched only for postings not already stored, a hard
per-run cap, and a pause between requests. The `linkedin_*` knobs in `settings.yaml` control all of
this; leaving them low is the point.

## CLI

| Command | Description |
|---------|-------------|
| `job-scan run`        | run a full scan (default) |
| `job-scan run --no-llm` | collect + dedup only, defer scoring (automatic if Ollama is unreachable) |
| `job-scan probe <ats> <slug>` | test an ATS + slug combo before adding it |
| `job-scan rescore`    | re-score stored postings under the current mechanism |
| `job-scan rescore --unscored-only` | score only the backlog collected while the LLM was down |
| `job-scan recompute`  | recombine stored sub-scores under current config (no LLM) |
| `job-scan gaps`       | tally recurring skill gaps across good matches (no LLM) |
| `job-scan prune`      | re-fetch boards and hide postings with dead links |
| `job-scan serve`      | run the dashboard web server |

## Scheduling

`systemd/` holds user-level timers for a daily scan and a weekly sweep; `install.sh` installs and
enables them. The runner waits for network before firing (so a timer that triggers pre-network on
resume doesn't fail).

## Tech stack

Python · FastAPI (dashboard) · SQLite · Ollama (local LLM) · httpx · systemd · `uv`

## Privacy

Everything runs locally. Your resume, scored postings, reports, and cover letters never leave your
machine and are git-ignored — this repo is the application code only.
