# Privacy: what onboarding reads and sends

`sourcer init` turns your own sources into facts for your job search. This page
says exactly what it reads, what leaves your computer and where everything is
kept. `sourcer init` opens with two lines (Sourcer keeps your data only on
this computer; nothing is sent to us) and shows the text below when you type
`details`.

## The short version

Your files, facts and profile stay on this computer. Sourcer sends short
excerpts to your own Claude to pull out facts about your work. Nothing goes to
the author of Sourcer, and there is no account. You approve every fact before it
is used, and `sourcer forget` removes one source or everything.

## What Sourcer reads

Only the sources you add, each at the moment you add it:

- your resume file (PDF, DOCX, TXT or MD);
- the public repositories of the GitHub username you give: name, description,
  topics, languages, created and last pushed dates, stars and the start of the
  README;
- project folders you include: the README and package manifests at each
  project root, file counts by type (names only), and the first and last commit
  dates from git;
- the LinkedIn data export zip you downloaded yourself: Positions, Projects,
  Skills, Certifications and Education. Sourcer never visits LinkedIn;
- links you add. They are saved now; reading them comes in a later version.

In a folder Sourcer never opens `.env` files, keys, certificates, credential or
secret files, anything `.gitignore` excludes, dependency and build folders
(`node_modules`, `.venv`, `venv`, `dist`, `build` and similar), hidden files,
links, binaries, or files over `SOURCER_ONBOARD_MAX_FILE_BYTES` (200,000 bytes by
default). Before it scans a folder it shows how many files it will read and
asks. You can exclude subfolders with globs; they are kept in `sources.json`.

Every excerpt passes through a redactor before it is previewed, stored or
sent. It masks API keys, tokens, private keys, passwords in assignments,
credentials in URLs, email addresses and phone numbers. Contact details are
never kept as facts.

## What leaves your computer, and who sees it

1. **Excerpts to Anthropic's Claude.** Anthropic is the only model provider.
   Calls go through your own Claude CLI login (`SOURCER_LLM=claude-code`, on
   your subscription) or your own API key (`ANTHROPIC_API_KEY`, sent straight
   to api.anthropic.com). With the CLI each call runs with no tools, no file
   access, no MCP servers and an empty working folder, so the model sees only
   the excerpts Sourcer passes it. Anthropic's own policy covers what Anthropic
   keeps: <https://www.anthropic.com/legal/privacy>.
2. **Requests to api.github.com** for the username you give, public data only.
   `GITHUB_TOKEN`, when you set it, is sent only there.
3. **Requests to the links you add**, once link reading exists.

This project never collects your data and never trains anything on it. It has
no server, no telemetry and no account.

Before any model call Sourcer prints one line with what it will read and about
how many calls it will make (with a dollar estimate on an API key) and waits
for your yes. Type `p` there, or pass `--dry-run`, to write the exact excerpts
to the preview file with no call at all. `SOURCER_COST_CAP` still applies.
`sourcer facts log` lists every outbound call: time, destination, source ids
and character counts, never the content.

## How long it is kept

In local files until you delete them. Sourcer keeps nothing anywhere else.

| File | What |
|---|---|
| `sources.json` (next to `profile.json`, `SOURCER_SOURCES`) | the sources you added |
| `data/facts.json` (`SOURCER_FACTS`) | candidate facts and their status |
| `data/evidence.txt` (`SOURCER_EVIDENCE`) | approved facts, in a marked section; your own lines stay as they are |
| `profile.json` (`SOURCER_PROFILE`) | graduation, degree, work authorization |
| `data/onboard-preview.txt` (`SOURCER_ONBOARD_PREVIEW`) | the last preview |
| `data/onboard-log.jsonl` (`SOURCER_ONBOARD_LOG`) | the outbound call log |

All of them are gitignored and written readable by you only.

## Taking it back

- `sourcer forget --source github` (or a source id from `sourcer forget`)
  removes that source and its facts, and rewrites the evidence file without
  them.
- `sourcer forget --all` deletes every onboarding file above and the
  onboarding section of the evidence file. Your `profile.json` and `.env` stay.
