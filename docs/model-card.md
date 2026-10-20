# Model card: Sourcer scoring

Sourcer does not train a model. It uses a hosted general-purpose model
(Claude Haiku 4.5 by default, `SOURCER_MODEL`) for two narrow tasks and wraps
them in deterministic rules. This card documents that use so its behaviour,
limits, and failure modes are explicit.

## Intended use

- Extract structured fields from a job posting: title, location,
  requirements, seniority, tech stack, employment type, start date, minimum
  years, work mode.
- Score the extraction 0 to 100 against one candidate's weighted skills
  vector with a one-line rationale.
- Triage titles the role regex missed, once per run, batched.

Out of scope: ranking candidates, making hiring decisions, generating
application content in this repository. Scores are a personal triage aid for
one operator, never shown to employers.

## Inputs and outputs

| Input | Source | Trust |
|---|---|---|
| Posting text | Public ATS APIs, HTML stripped, truncated to 8,000 chars | untrusted |
| Skills vector | Operator-authored JSON, weights 1 to 10 with synonyms | trusted |
| Candidate profile | Operator-authored JSON | trusted |

Outputs are validated against a fixed schema (`llm.PARSE_SCORE_REQUIRED`)
with one corrective retry; an invalid reply is logged and the posting is
left unscored, never written with partial fields.

## Determinism and reproducibility

- Prompts are module constants in `sourcer/llm.py`; a diff shows any change.
- Model id, pricing, and cost cap are pinned by environment variables and
  recorded per run in `run_logs`.
- The evaluation harness (`python -m sourcer.evaluate`) replays a labelled
  set of postings through the deterministic rules and, optionally, the
  model, and reports agreement. The labelled set is versioned in `evals/`.
- Temperature is the API default; runs are not bit-reproducible. Scores
  for the same posting can vary by a few points between runs, which is why
  a twin transfers its score rather than being re-scored.

## Evaluation

`evals/labelled_postings.json` holds hand-labelled postings with the
expected exclusion outcome and, where scored, an expected score band.
`sourcer.evaluate` reports:

- exclusion precision and recall against the labels (deterministic, free);
- level-fit agreement;
- when run with `--model`, mean absolute score error and band agreement
  against the labelled bands, at the cost of one call per posting.

The CI suite runs the deterministic part on every change.

## Known limitations and failure modes

- **Location strings lie.** Boards list "Remote" for roles restricted to
  one country. The post-parse `work_mode` check catches hybrid or onsite
  claims, not residency restrictions the description omits.
- **Regex title filter.** Roles with unusual titles reach the model only
  through triage, which fails open to nothing when the cap is reached.
- **Rationale wording.** The public showcase strips gap language with a
  fail-closed scan, but the private rationale can be blunt or wrong.
- **Score drift across vector edits.** Editing the skills vector requires
  `sourcer.rescore`; stored scores are not recomputed automatically.
- **Prompt injection.** A posting can instruct the model. The blast radius
  is one wrong score or an invalid reply; nothing from the model is
  executed, and board-provided title, location, and url always win.

## Responsible use checklist

- [x] No protected attributes are in the skills vector or profile schema;
  the profile carries education status, graduation date, and a years cap.
- [x] Outputs stay private to the operator; the public export is aggregate
  or redacted and scanned.
- [x] Spend is capped per run and visible per run.
- [x] Model replies are schema-validated and never executed.
- [ ] Bias audit across employer sectors is not performed; the labelled set
  is small and reflects one candidate's target roles (tracked in the
  backlog).
