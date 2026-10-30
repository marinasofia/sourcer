# 0013. Onboarding builds evidence from the candidate's own sources

Date: 2026-10-30
Status: accepted

## Context

Sourcer scored fit against a skills vector and an evidence file that the
operator wrote by hand. A stranger who clones the repository has neither, and
typing skills and weights is slow and unreliable. The useful facts already
exist in a resume, public repositories, project folders and a LinkedIn
export. People are rightly wary of pointing a tool from a public repository at
those files, so how the facts are gathered matters as much as the facts.

## Decision

1. `sourcer init` records sources (`sources.json`), one at a time, each with
   the candidate's consent, and scans each as it is added. Collectors live in
   `sourcer/onboard/` and return source items: a stable id, the source type, a
   location (file path or URL) and a short excerpt.
2. Local folders give structural signals only: README and manifest excerpts,
   extension counts and git dates. Secrets, gitignored files, dependencies,
   hidden files, links, binaries and large files are never opened. Every
   excerpt is redacted before it is previewed, stored or sent.
3. Before any model call the candidate sees what will be read and an estimate,
   and must say yes. A preview of the exact payload is one keypress away. The
   cost cap applies as everywhere else (0003).
4. One model call per batch of excerpts (`llm.FACTS_SYSTEM`, through
   `llm.json_call`, so the API and the Claude CLI backends both work) returns
   candidate facts. Code keeps a fact only when its quote is in its own excerpt
   and keeps only the skills the excerpt names, as 0012 does for postings.
5. Facts wait in `data/facts.json` as pending until the candidate approves
   them in `sourcer facts review`. Approved facts are written into a marked
   section of the evidence file the scorer already reads, so scoring needs no
   change; init turns evidence scoring on.
6. Each outbound call is logged locally without content, and `sourcer forget`
   removes one source or everything.

## Consequences

- A new candidate gets evidence without typing skills, and every fact keeps its
  source and quote.
- The facts store keeps the excerpt hash of each item read, so a later scan
  reads only new or changed items. This is the seam for a sync command.
- Skill weights are not computed yet; a later step derives them from approved
  facts. Links are recorded but not read until link collectors exist; the
  collector registry (`onboard.collect.COLLECTORS`) is where they plug in.
- Other CLI backends plug into `onboard.backends.BACKENDS` once they have a
  client that mimics `messages.create`.
