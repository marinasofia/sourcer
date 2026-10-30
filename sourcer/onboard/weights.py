"""Skill weights computed from approved facts, each with a short reason.

No model call: the weight is a fixed formula over four signals, so the same
facts and postings always give the same weights.

depth      distinct jobs, projects and other groups whose facts name the
           skill (5 or more counts in full), plus a bonus when a fact says
           the work shipped (deployed, in production, users, released).
recency    the latest date among those facts: within 6 months 1, within a
           year 0.75, two years 0.5, four years 0.25, older or undated 0.
agreement  distinct source types naming it (resume, GitHub, a folder...),
           3 or more counts in full.
demand     the share of matching postings in the local database (titles
           that fit the target roles) whose text names the skill or a
           synonym; 40% or more counts in full. Skipped, and said so in the
           reason, when there are fewer than MIN_POSTINGS of them.

score  = 0.40 depth + 0.20 recency + 0.15 agreement + 0.25 demand
         (divided by 0.75 when demand is skipped)
weight = round(1 + 9 score), so 1 to 10.

The candidate can override any weight (``sourcer skills set``); an override
keeps its weight through every recomputation until ``sourcer skills reset``.
"""

from __future__ import annotations

import json
import math
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from .. import config, db
from ..log import get_logger
from .store import now, write_private

log = get_logger(__name__)

MIN_POSTINGS = 30
FULL_DEPTH = 5
FULL_AGREEMENT = 3
FULL_DEMAND = 0.40
PARTS = {"depth": 0.40, "recency": 0.20, "agreement": 0.15, "demand": 0.25}
COMPUTED, OVERRIDE = "computed", "override"

SHIPPED_RE = re.compile(
    r"\b(deploy(?:ed|s|ment)?|in production|production|shipped|released|launched|live"
    r"|users|customers|downloads|published|in use)\b",
    re.I,
)
# spellings that name the same skill; the first is how it is shown
ALIASES: tuple[tuple[str, ...], ...] = (
    ("PostgreSQL", "Postgres"),
    ("JavaScript", "JS"),
    ("TypeScript", "TS"),
    ("Kubernetes", "k8s"),
    ("scikit-learn", "sklearn", "scikit learn"),
    ("Go", "Golang"),
    ("AWS", "Amazon Web Services"),
    ("Google Cloud", "GCP", "Google Cloud Platform"),
    ("Machine learning", "ML"),
    ("LLMs", "LLM", "large language models", "large language model"),
    ("NLP", "natural language processing"),
    ("Node.js", "Node", "NodeJS"),
    ("React", "React.js", "ReactJS"),
    ("Hugging Face", "HuggingFace", "transformers"),
    ("Power BI", "PowerBI"),
)
CATEGORIES: dict[str, tuple[str, ...]] = {
    "Languages": ("python", "javascript", "typescript", "java", "go", "rust", "c", "c++", "c#",
                  "r", "sql", "scala", "kotlin", "swift", "ruby", "php", "bash", "matlab"),
    "AI/ML": ("machine learning", "pytorch", "tensorflow", "scikit-learn", "llms", "nlp",
              "langchain", "langgraph", "hugging face", "keras", "xgboost", "computer vision",
              "deep learning", "openai", "claude", "rag"),
    "Data": ("pandas", "numpy", "spark", "postgresql", "mysql", "sqlite", "snowflake", "dbt",
             "airflow", "tableau", "power bi", "excel", "bigquery", "mongodb", "kafka", "duckdb"),
    "Cloud": ("aws", "google cloud", "azure", "docker", "kubernetes", "terraform"),
    "Software": ("react", "node.js", "django", "flask", "fastapi", "git", "linux", "html", "css"),
}  # fmt: skip
SOURCE_LABELS = {
    "resume": "resume", "github": "GitHub", "folder": "project folders",
    "linkedin": "LinkedIn", "page": "your site", "feed": "your blog", "kaggle": "Kaggle",
    "huggingface": "Hugging Face", "devpost": "Devpost", "scholar": "Google Scholar",
    "credly": "Credly", "publications": "your publications list", "link": "a link",
}  # fmt: skip
KIND_NOUNS = {
    "job": "job", "project": "project", "education": "school", "certification": "certification",
    "publication": "publication", "other": "other mention",
}  # fmt: skip
ORDER = {t: n for n, t in enumerate(SOURCE_LABELS)}
RECENCY = ((6, 1.0), (12, 0.75), (24, 0.5), (48, 0.25))  # months ago -> score
MONTHS = {m: n for n, m in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1
)}  # fmt: skip
MONTH_YEAR_RE = re.compile(
    r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+((?:19|20)\d\d)\b", re.I
)
ISO_RE = re.compile(r"\b((?:19|20)\d\d)[-/](0?[1-9]|1[0-2])\b")
SLASH_RE = re.compile(r"\b(0?[1-9]|1[0-2])/((?:19|20)\d\d)\b")
YEAR_RE = re.compile(r"\b((?:19|20)\d\d)\b")
NOW_RE = re.compile(r"\b(present|current|now|ongoing|today)\b", re.I)


def _key(name: str) -> str:
    return " ".join(re.sub(r"[\s_\-]+", " ", name.strip().lower()).split()).strip(".")


_ALIAS = {_key(a): group[0] for group in ALIASES for a in group}
_CATEGORY = {_key(s): cat for cat, names in CATEGORIES.items() for s in names}


def canonical(name: str) -> str:
    """The key one skill is counted under: case, spacing and known aliases ignored."""
    k = _key(name)
    return _key(_ALIAS.get(k, k))


def plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def joined(words: list[str]) -> str:
    return words[0] if len(words) == 1 else ", ".join(words[:-1]) + " and " + words[-1]


# ---------------------------------------------------------------- signals


def latest_month(text: str, today: date) -> tuple[int, int] | None:
    """The latest (year, month) a date text names; "present" counts as today."""
    found: list[tuple[int, int]] = []
    if NOW_RE.search(text):
        found.append((today.year, today.month))
    found += [(int(y), MONTHS[m[:3].lower()]) for m, y in MONTH_YEAR_RE.findall(text)]
    found += [(int(y), int(m)) for y, m in ISO_RE.findall(text)]
    found += [(int(y), int(m)) for m, y in SLASH_RE.findall(text)]
    if not found:
        found = [(int(y), 12) for y in YEAR_RE.findall(text)]
    found = [f for f in found if 1990 <= f[0] <= today.year + 1]
    if not found:
        return None
    return min(max(found), (today.year, today.month))


def _recency(latest: tuple[int, int] | None, today: date) -> tuple[float, str]:
    if latest is None:
        return 0.0, "no dates"
    months = (today.year - latest[0]) * 12 + today.month - latest[1]
    score = next((s for limit, s in RECENCY if months <= limit), 0.0)
    if months <= 0:
        return score, "used this month"
    if months < 12:
        return score, f"used {plural(months, 'month')} ago"
    return score, f"last used in {latest[0]}"


def _pattern(names: list[str]) -> re.Pattern[str]:
    """Any of the names as a whole word. One or two letter names (R, C, Go)
    must match their case, so "go" and "r" in plain prose do not count."""
    parts = []
    for name in sorted({n.strip() for n in names if n.strip()}, key=len, reverse=True):
        word = r"(?<!\w)" + re.escape(name) + r"(?!\w)"
        parts.append(word if len(name) <= 2 or name.lower() == "go" else f"(?i:{word})")
    return re.compile("|".join(parts))


def demand_texts() -> list[str] | None:
    """Posting texts from the local database whose title fits the target roles.
    None when there is no local database to read."""
    if db.BACKEND != "sqlite" or not config.DB_PATH.exists():
        return None
    try:
        conn = db.connect()
        try:
            rows = conn.execute(
                "SELECT title, description, requirements, tech_stack FROM postings"
            ).fetchall()
        finally:
            conn.close()
    except Exception as exc:  # a locked or old database must not stop a review
        log.warning("postings not read for demand: %s", type(exc).__name__)
        return None
    return [
        " ".join(str(r[k] or "") for k in ("title", "description", "requirements", "tech_stack"))
        for r in rows
        if config.ROLE_RE.search(str(r["title"] or ""))
    ]


# ---------------------------------------------------------------- weights


@dataclass
class _Skill:
    names: Counter[str] = field(default_factory=Counter)  # spelling -> facts
    groups: dict[tuple[str, str], str] = field(default_factory=dict)  # group key -> kind
    shipped: bool = False
    latest: tuple[int, int] | None = None
    types: set[str] = field(default_factory=set)


def _gather(facts: list[dict[str, Any]], today: date) -> dict[str, _Skill]:
    skills: dict[str, _Skill] = {}
    for fact in facts:
        group = str(fact.get("group") or "").strip().lower()
        kind = str(fact.get("kind")) if fact.get("kind") in KIND_NOUNS else "other"
        gkey = (kind, group) if group else ("item", str(fact.get("item_id") or fact.get("id")))
        when = latest_month(str(fact.get("dates") or ""), today)
        for raw in fact.get("skills") or []:
            name = str(raw).strip()
            if not name:
                continue
            s = skills.setdefault(canonical(name), _Skill())
            s.names[_ALIAS.get(_key(name), name)] += 1
            s.groups[gkey] = kind
            s.shipped = s.shipped or bool(SHIPPED_RE.search(str(fact.get("text") or "")))
            if when and (s.latest is None or when > s.latest):
                s.latest = when
            s.types.add(str(fact.get("source_type") or "link"))
    return skills


def _depth_phrase(kinds: Counter[str], shipped: bool) -> str:
    order = ("project", "job", "education", "certification", "publication", "other")
    words = [plural(kinds[k], KIND_NOUNS[k]) for k in order if kinds[k]]
    return joined(words) + (" with shipped work" if shipped else "")


def compute(
    facts: list[dict[str, Any]], postings: list[str] | None, today: date | None = None
) -> list[dict[str, Any]]:
    """Weighted skills from approved facts, highest first. ``postings`` are the
    matching posting texts, or None when there is no local database."""
    today = today or config.utc_now().date()
    approved = [f for f in facts if f.get("status", "approved") == "approved"]
    enough = postings is not None and len(postings) >= MIN_POSTINGS
    ranked: list[tuple[tuple[int, int, str], dict[str, Any]]] = []
    for key, s in _gather(approved, today).items():
        shown = s.names.most_common(1)[0][0]
        spellings = set(s.names) | {a for g in ALIASES if canonical(g[0]) == key for a in g}
        synonyms = sorted({n for n in spellings if n.lower() != shown.lower()}, key=str.lower)
        kinds = Counter(s.groups.values())
        depth = min(len(s.groups), FULL_DEPTH) / FULL_DEPTH * 0.8 + (0.2 if s.shipped else 0)
        recency, when = _recency(s.latest, today)
        types = sorted(s.types, key=lambda t: ORDER.get(t, len(ORDER)))
        agreement = min(len(types), FULL_AGREEMENT) / FULL_AGREEMENT
        total = PARTS["depth"] * depth + PARTS["recency"] * recency
        total += PARTS["agreement"] * agreement
        reason = [_depth_phrase(kinds, s.shipped), when,
                  "named by " + joined([SOURCE_LABELS.get(t, t) for t in types])]  # fmt: skip
        if enough:
            if postings is None:  # enough implies postings were loaded
                raise RuntimeError("postings missing while demand is enabled")
            pattern = _pattern([shown, *synonyms])
            share = sum(1 for p in postings if pattern.search(p)) / len(postings)
            total += PARTS["demand"] * min(share / FULL_DEMAND, 1.0)
            reason.append(f"in {round(100 * share)}% of matching postings")
        else:
            total /= 1 - PARTS["demand"]
            reason.append(
                "demand not counted, no local postings yet"
                if not postings
                else f"demand not counted, only {len(postings)} matching postings "
                f"(needs {MIN_POSTINGS})"
            )
        weight = max(1, min(10, math.floor(1 + 9 * total + 0.5)))
        entry = {
            "skill": shown, "weight": weight, "category": _CATEGORY.get(key, "Other"),
            "synonyms": synonyms, "reason": ", ".join(reason), "source": COMPUTED,
        }  # fmt: skip
        ranked.append(((-weight, -len(s.groups), shown.lower()), entry))
    return [entry for _, entry in sorted(ranked, key=lambda r: r[0])]


def ranking(facts: list[dict[str, Any]], n: int = 6) -> list[str]:
    """The top skill names for a set of facts, by the same formula (no database read)."""
    return [e["skill"] for e in compute(facts, None)[:n]]


# ---------------------------------------------------------------- the vector file

META = {
    "source": "Computed by sourcer from your approved facts (sourcer skills shows each reason). "
    "Change a weight with `sourcer skills set NAME WEIGHT`; it is kept on every recompute.",
    "weighting": "1-10 from depth, recency, agreement across sources and demand; see docs/adr/0014",
}


def load_vector() -> dict[str, Any]:
    try:
        data = json.loads(config.SKILLS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"skills": []}
    if not isinstance(data, dict) or not isinstance(data.get("skills"), list):
        return {"skills": []}
    return data


def hand_written(data: dict[str, Any]) -> bool:
    """A vector someone wrote by hand: it has entries Sourcer did not make."""
    return any(
        not isinstance(e, dict) or e.get("source") not in (COMPUTED, OVERRIDE)
        for e in data.get("skills") or []
    )


@dataclass
class Recomputed:
    written: bool
    skills: list[dict[str, Any]]
    note: str = ""


def _merge(computed: list[dict[str, Any]], old: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Computed weights with every override kept, highest weight first."""
    overrides = {canonical(e["skill"]): e for e in old if e.get("source") == OVERRIDE}
    out = []
    for entry in computed:
        mine = overrides.pop(canonical(entry["skill"]), None)
        if mine is None:
            out.append(entry)
            continue
        reason = f"set by you; computed {entry['weight']}: {entry['reason']}"
        out.append({**entry, "weight": mine["weight"], "source": OVERRIDE, "reason": reason})
    for mine in overrides.values():  # an override with no approved fact behind it stays
        out.append({**mine, "reason": "set by you; no approved fact names it"})
    out.sort(key=lambda e: -int(e["weight"]))
    return out


def write_vector(skills: list[dict[str, Any]]) -> None:
    data = {"meta": {**META, "computed_at": now()}, "skills": skills}
    write_private(config.SKILLS_PATH, json.dumps(data, indent=2, ensure_ascii=False) + "\n")


HAND_WRITTEN = (
    "your skills_vector.json is hand written, so Sourcer leaves it as it is; "
    "`sourcer skills compute --force` replaces it and keeps a .bak copy"
)


def recompute(facts: list[dict[str, Any]], *, force: bool = False) -> Recomputed:
    """Recompute and write the skills vector. A hand written vector is left
    alone unless ``force``; then it is kept next to the new one as a backup."""
    old = load_vector()
    if hand_written(old):
        if not force:
            return Recomputed(False, [], HAND_WRITTEN)
        backup = config.SKILLS_PATH.with_name(config.SKILLS_PATH.name + ".bak")
        write_private(backup, config.SKILLS_PATH.read_text(encoding="utf-8"))
        old = {"skills": []}
    skills = _merge(compute(facts, demand_texts()), old["skills"])
    write_vector(skills)
    return Recomputed(True, skills)


def _without(entries: list[dict[str, Any]], key: str) -> list[dict[str, Any]]:
    return [
        e for e in entries if not (e.get("source") == OVERRIDE and canonical(e["skill"]) == key)
    ]


def set_override(facts: list[dict[str, Any]], name: str, weight: int) -> dict[str, Any]:
    """Keep ``weight`` for ``name`` through every recompute. Returns its entry.
    ValueError on a hand written vector or a weight outside 1 to 10."""
    data = load_vector()
    if hand_written(data):
        raise ValueError(HAND_WRITTEN)
    if not 1 <= weight <= 10:
        raise ValueError("a weight is a whole number from 1 to 10")
    key = canonical(name)
    computed = {canonical(e["skill"]): e for e in compute(facts, None)}
    base = computed.get(key) or {
        "skill": " ".join(name.split()), "category": _CATEGORY.get(key, "Other"), "synonyms": [],
    }  # fmt: skip
    mine = {"skill": base["skill"], "weight": weight, "category": base["category"],
            "synonyms": base["synonyms"], "reason": "", "source": OVERRIDE}  # fmt: skip
    skills = _merge(compute(facts, demand_texts()), [*_without(data["skills"], key), mine])
    write_vector(skills)
    return next(e for e in skills if canonical(e["skill"]) == key)


def reset_override(facts: list[dict[str, Any]], name: str) -> bool:
    """Drop the override for ``name``. False when it had none."""
    data = load_vector()
    if hand_written(data):
        raise ValueError(HAND_WRITTEN)
    kept = _without(data["skills"], canonical(name))
    if len(kept) == len(data["skills"]):
        return False
    write_vector(_merge(compute(facts, demand_texts()), kept))
    return True
