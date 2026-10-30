# Reference

Every setting, input file, and command. The [README](../README.md) has the overview;
[operations](operations.md) covers running and debugging, and
[deployment](deployment.md) covers Docker, Actions, Lambda, Kubernetes, and Snowflake.

## Configuration

Configuration is read once at import time. The project `.env` supplies defaults
before settings are evaluated; existing process environment variables win.
Set `SOURCER_ROOT` in the process environment to select a different input root.

The runtime `profile.json` can set `earliest_start_full_time` to a `YYYY-MM`
month. That explicit availability overrides the default month after graduation.
For example, `2028-05` allows May 2028 starts. Missing posting dates still need
manual review. Full-time postings need internship/entry-level wording, a junior
title, or a stated minimum of zero to two years. "Full-time" alone is not enough.
The text prefilter recognizes common numeric and written experience requirements;
optional qualifications are not mandatory, and unfamiliar wording still needs
review. Parsed experience above two years and mid/senior roles are hard exclusions.
A stricter `max_years_experience` in the profile still applies after parsing.

Before AI triage or scoring, collection also checks descriptions for common
explicit office attendance, relocation, or residency requirements outside NYC.
These override a misleading remote location label. Optional visits and explicit
NYC or remote alternatives remain eligible. This is a bounded text check:
unfamiliar wording or places still need review, and it does not replace the
location check after parsing. A zero-dollar run does not produce AI scores.

| Variable | Default | Purpose |
|---|---|---|
| `ANTHROPIC_API_KEY` | required | model calls |
| `SOURCER_MODEL` | `claude-haiku-4-5-20251001` | model id for the fit checks and title triage |
| `SOURCER_EXTRACT_MODEL` | `claude-haiku-5-5` | model id for the extraction worker that copies a posting's facts with its own words before the rules run |
| `SOURCER_SCORE_MODEL` | `SOURCER_MODEL` | model id for the fit checks and title triage when it should differ from `SOURCER_MODEL` |
| `SOURCER_LLM` | `api` | `claude-code` runs calls through the Claude Code CLI instead of the API |
| `SOURCER_EVIDENCE` | `data/evidence.txt` | the candidate's approved facts; `sourcer facts review` writes its marked section |
| `SOURCER_EVIDENCE_SCORING` | unset | `1` scores fit against the evidence file instead of the skills list; `sourcer init` sets it |
| `SOURCER_SOURCES` | `sources.json` next to `profile.json` | onboarding sources (gitignored) |
| `SOURCER_FACTS` | `data/facts.json` | candidate facts and their status |
| `SOURCER_FACTS_MODEL` | `SOURCER_EXTRACT_MODEL` | model id that turns source excerpts into candidate facts |
| `SOURCER_ONBOARD_PREVIEW` | `data/onboard-preview.txt` | where a preview writes the exact excerpts it would send |
| `SOURCER_ONBOARD_LOG` | `data/onboard-log.jsonl` | local log of onboarding's outbound calls, never content |
| `SOURCER_ONBOARD_BATCH_CHARS` | `12000` | excerpt characters per fact extraction call |
| `SOURCER_ONBOARD_MAX_FILE_BYTES` | `200000` | a local file larger than this is never read |
| `SOURCER_ONBOARD_GITHUB_MAX_REPOS` | `20` | most recently pushed public repos read per GitHub username |
| `SOURCER_ONBOARD_LINK_MAX_PAGES` | `5` | pages read for one link (a site and its same host pages, Devpost projects) |
| `GITHUB_TOKEN` | empty | optional; raises the GitHub API rate limit for onboarding, sent only to api.github.com |
| `SOURCER_RUN_MINUTES` | no limit | no new model call after this many minutes in one run |
| `SOURCER_BROWSER` | `0` | `1` reads careers pages that only render with scripts, in a sandboxed headless Chromium |
| `SOURCER_COST_CAP` | `2.50` | estimated pipeline budget in USD; not a provider billing limit |
| `SOURCER_DB` | `data/sourcer.db` | SQLite path |
| `SOURCER_ROOT` | the checkout | root for relative inputs and exports; set it for an installed package or a container |
| `SOURCER_BACKEND` | `sqlite` | `sqlite` or `snowflake` |
| `SOURCER_SKILLS` | `skills_vector.json` | weighted skills with synonyms |
| `SOURCER_COMPANIES` | `companies.json` | company list (name, ATS, slug) |
| `SOURCER_PROFILE` | `profile.json` | candidate profile for level fit and availability |
| `SOURCER_SIMPLIFY` | `1` | set `0` to skip the SimplifyJobs lists |
| `SOURCER_FRESHNESS_DAYS` | `30` | the one freshness window (`config.is_fresh`) and the age penalty threshold; a role its company's board still lists, or an undated one, is never dropped for age |
| `SOURCER_DRY_RUN` | `1` | disables Telegram network activity and state marking |
| `SOURCER_TELEGRAM_TOKEN` | empty | private Bot API token |
| `SOURCER_TELEGRAM_ALLOWED_CHAT_IDS` | empty | comma-separated destination allowlist |
| `SOURCER_TELEGRAM_ALLOWED_USER_IDS` | empty | comma-separated sender allowlist |
| `SOURCER_GETRO_API_KEY` | empty | optional Getro portfolio API token |
| `SOURCER_GETRO_NETWORKS` | `{}` | JSON map of supported VC names to network IDs |
| `SOURCER_DEGREE` | `Bachelor's` | degree filter for SimplifyJobs listings |
| `SOURCER_ALERT_SCORE` | `85` | alert threshold |
| `SOURCER_ALERT_CAP` | `60` | flood control for alert issues |
| `SOURCER_PRICE_IN`, `SOURCER_PRICE_OUT` | `1.00`, `5.00` | USD per million tokens for the cost estimate |
| `SOURCER_CACHE_MIN_TOKENS` | `4096` | model's minimum cacheable prefix |
| `SOURCER_LOG_LEVEL` | `INFO` | logging level for the package |
| `SOURCER_HTTP_HOST` | `127.0.0.1` | HTTP bind address; Kubernetes explicitly binds all container interfaces |
| `SOURCER_HTTP_TOKEN` | required | bearer token for `/score` and `/digest`; the service will not start without it |
| `SOURCER_HTTP_CONCURRENCY` | `2` | scores the HTTP service runs at once |
| `SOURCER_HTTP_PORT` | `8500` | HTTP scoring service port |
| `SOURCER_S3_BUCKET`, `SOURCER_SKIP_S3` | | Lambda state persistence |
| `SNOWFLAKE_ACCOUNT`, `SNOWFLAKE_USER`, `SNOWFLAKE_PASSWORD` | | Snowflake backend |

Input files:

- `companies.json`: the target company list.
- `skills_vector.json`: gitignored, personal. `sourcer init` and
  `sourcer facts review` compute it from your approved facts (ADR 0014): each
  entry has `skill`, `weight` (1 to 10), `category`, `synonyms`, a `reason`
  and `source` (`computed` or `override`). The scorer reads only the first
  four, and only when evidence scoring is off; with it on, fit is judged from
  the approved facts themselves.
  A file you wrote by hand (entries without `source`) is never replaced
  unless you run `sourcer skills compute --force`, which keeps a `.bak` copy.
- `sources.json`: gitignored, written by `sourcer init`. See [privacy](privacy.md).
- `profile.json`: optional. Copy `profile.example.json` to enable level fit
  and the graduation availability rule. Without it, every posting fits.

## Usage

Activate the quickstart environment with `source .venv/bin/activate` before
running these commands. Live scoring commands can make paid model requests.

```bash
sourcer init                                 # set up: privacy note, sources one at a time, profile
sourcer init --add                           # add more sources later
sourcer init --yes --resume cv.pdf --github USER --folder ~/code   # no questions
sourcer facts scan [--dry-run]               # read sources again, extract new facts
sourcer facts review                         # approve, reject or edit pending facts
sourcer facts list [--status pending]        # list facts
sourcer facts log                            # every outbound call onboarding made
sourcer init --add --link URL                # add a link; its kind comes from the URL
sourcer init --add --publications pubs.txt   # a pasted publications list (TXT or MD)
sourcer sync [--yes] [--dry-run]             # recheck every source, queue new or changed facts
sourcer sync --quiet --yes                   # scheduled: no prompts, one line of output
sourcer skills                               # skill weights with the reason for each
sourcer skills set SQL 9                     # keep your own weight through every recompute
sourcer skills reset SQL                     # back to the computed weight
sourcer skills compute [--force]             # recompute now (--force replaces a hand written file)
sourcer forget [--source github | --all]     # remove one source and its facts, or everything
sourcer --cost-cap 0.05                      # run the pipeline (same as python -m sourcer.main)
python -m sourcer.track list                 # ranked board with ids
python -m sourcer.track set 97 applied "sent 09-03"
python -m sourcer.score_listings --limit 30  # score unscored listings live
python -m sourcer.batch --dry-run            # price the backlog at batch rates
python -m sourcer.apply.verify --limit 600   # archive dead links
sourcer probe "Company Name"                 # discover and save its ATS board
sourcer probe --all                          # probe companies whose ATS type is empty
sourcer discover                             # weekly company discovery and bounded ATS probing
sourcer collect                              # daily structured collection and scoring
sourcer orchestrate daily                    # locked daily run plus Telegram digest
sourcer orchestrate weekly --probe-limit 25  # discovery, probe, collect, and weekly report
sourcer telegram                             # allowlisted URL intake and feedback bot
python -m sourcer.rescore                    # re-score after editing the vector
python -m sourcer.alerts                     # print or open the alert issue
python -m sourcer.mcp_server                 # MCP tools over stdio
python -m sourcer.serve                      # HTTP scoring service on :8500
```

### Links

`--link URL` (or a link typed during `sourcer init`) is saved as the kind its
URL names:

| Kind | URL | What is read |
|---|---|---|
| `page` | any other https link | the page's main text and up to `SOURCER_ONBOARD_LINK_MAX_PAGES` pages in all on the same host |
| `feed` | a path ending in `.xml`, `.rss`, `.atom`, `/feed` or `/rss` | titles, dates and summaries of the latest 25 posts |
| `kaggle` | `kaggle.com/...` | the page title and description; Kaggle shows notebooks and datasets only with JavaScript, so add each notebook or dataset link |
| `huggingface` | `huggingface.co/USER` | public models, spaces and datasets from the public API |
| `devpost` | `devpost.com/USER` or a project | each project's tagline, story and "built with" list |
| `scholar` | `scholar.google.*/citations?user=ID` | one profile page; Google often refuses automated reads, and then `--publications FILE` is the way |
| `credly` | `credly.com/users/USER` | public badges (`badges.json`) |

LinkedIn links are refused: LinkedIn is read only from the export zip
(`--linkedin-export`). A GitHub profile link becomes a GitHub source. Every
request checks robots.txt first (an unreadable robots.txt means nothing is
read there), sends `User-Agent: Sourcer/VERSION (+https://github.com/marinasofia/sourcer)`,
times out, reads at most 2,000,000 bytes per reply, never follows a redirect
to another host, and is written to the call log.

### Sync

`sourcer sync` rechecks every source in `sources.json`. A resume or LinkedIn
export whose file hash is unchanged is not read again; a GitHub repo with
nothing pushed since the last read is skipped; folders and links are read and
only excerpts that changed go on. What is left passes the same one line cost
gate, preview, redaction and call log as `sourcer init`, and new facts wait
for `sourcer facts review` (a fact whose quote is already known from that
source is not queued twice). After review and after sync the skill weights
are recomputed. The run ends with a line such as
"3 new facts waiting. Run `sourcer facts review`.", and the morning digest
prints the same reminder while facts wait.

`--quiet` is for a schedule: no prompts and one line. Without `--yes` it
sends nothing and queues nothing when model calls are needed, logs it, and
says so; the next `sourcer sync` in a terminal asks. To run it weekly on your
own computer, add a cron line (`crontab -e`), Monday at 8:00:

```
0 8 * * 1 cd /path/to/sourcer && .venv/bin/sourcer sync --quiet --yes >> data/sync.log 2>&1
```

Leave out `--yes` to be asked first. The private data workflow does not run
sync: the runner has no resume, folders or Claude login, and facts need your
review, so run it on your own computer.

Deployment on Docker, GitHub Actions, AWS Lambda, Kubernetes, and Snowflake is
covered in [`docs/deployment.md`](docs/deployment.md). Running, debugging, and
reading the run log are in [`docs/operations.md`](docs/operations.md).

## Code and data

This repository holds code only. A separate private data repository holds the
operator's skills vector, profile, scored results, and scheduled workflow. Its
workflow installs this code at a pinned commit, checks that the commit is on
`main`, and never pushes into this repository. The job that reads outside pages
holds no write access; a separate job saves the results.

A role on a live company board is never dropped for age. `config.age_penalty`
lowers its rank instead, and the stored model score stays unchanged.
