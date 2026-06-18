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
  - Aggregators: **Hacker News "Who is hiring?", RemoteOK, Remotive, We Work Remotely**
- **Cheap pre-filters** (title include/exclude, location) drop obvious misses before spending LLM time.
- **LLM matching** scores each posting 0–100 against your resume using a local model (Ollama / Gemma),
  via **decomposed sub-scores** combined deterministically (see [Scoring](#scoring)).
- **Deduplicates** against a local SQLite store so each run only surfaces genuinely new postings.
- **Reports**: a ranked daily Markdown digest + a web dashboard with application-status tracking.
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

## Configuration

| File | Purpose |
|------|---------|
| `config/settings.yaml` | resume path, model, scoring weights, location preferences |
| `config/companies.yaml` | ATS boards to scan (priority + broader sweep) |
| `config/filters.yaml`   | pre-LLM title include/exclude + location rules |

## CLI

| Command | Description |
|---------|-------------|
| `job-scan run`        | run a full scan (default) |
| `job-scan probe <ats> <slug>` | test an ATS + slug combo before adding it |
| `job-scan rescore`    | re-score stored postings under the current mechanism |
| `job-scan recompute`  | recombine stored sub-scores under current config (no LLM) |
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
