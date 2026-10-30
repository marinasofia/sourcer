"""What onboarding may read from a local folder, and the redactor every
excerpt passes through before it is previewed, stored or sent.

The folder rules are deliberately conservative: a file is skipped when any
rule matches, and a .gitignore negation ("!keep.txt") never brings a file back.
"""

from __future__ import annotations

import fnmatch
import re
from pathlib import Path

# directories that hold dependencies or build output, never the candidate's work
DEPENDENCY_DIRS = frozenset(
    {"node_modules", ".venv", "venv", "env", "dist", "build", "__pycache__", "site-packages",
     "target", "vendor", ".tox", ".nox", ".mypy_cache", ".pytest_cache", ".ruff_cache",
     ".next", ".gradle", "bower_components", "Pods"}
)  # fmt: skip
SECRET_NAME_RE = re.compile(
    r"(^\.env|^\.netrc$|^\.npmrc$|^\.pypirc$|^\.pgpass$|^id_(rsa|dsa|ecdsa|ed25519)"
    r"|\.(pem|key|crt|cer|der|p12|pfx|jks|keystore|kdbx|gpg|asc|ovpn|tfstate|tfvars)$"
    r"|secret|credential|password|passwd|token|apikey|api_key|private)",
    re.I,
)
BINARY_SUFFIXES = frozenset(
    {".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".pdf", ".zip", ".gz", ".tar", ".7z",
     ".so", ".dylib", ".dll", ".exe", ".bin", ".o", ".a", ".class", ".jar", ".pyc", ".whl",
     ".db", ".sqlite", ".sqlite3", ".parquet", ".pkl", ".pickle", ".npy", ".npz", ".h5",
     ".pt", ".onnx", ".mp3", ".mp4", ".mov", ".wav", ".woff", ".woff2", ".ttf", ".otf"}
)  # fmt: skip


def name_reason(path: Path, max_bytes: int) -> str | None:
    """Why a file must not be read, judged without opening it: a link, a
    secret or key by its name, a hidden file, a binary type, or its size."""
    name = path.name
    if path.is_symlink():
        return "links"
    if SECRET_NAME_RE.search(name):
        return "secrets"
    if name.startswith("."):
        return "hidden"
    if path.suffix.lower() in BINARY_SUFFIXES:
        return "binaries"
    try:
        return "large files" if path.stat().st_size > max_bytes else None
    except OSError:
        return "unreadable"


def content_reason(path: Path) -> str | None:
    """ "binaries" when the first bytes hold a NUL. Only for a file that is
    about to be read and passed every other rule."""
    try:
        with path.open("rb") as fh:
            return "binaries" if b"\0" in fh.read(4096) else None
    except OSError:
        return "unreadable"


def skip_reason(path: Path, max_bytes: int) -> str | None:
    """Why a file must not be read, or None. Never follows a link."""
    return name_reason(path, max_bytes) or content_reason(path)


def dir_skip_reason(name: str) -> str | None:
    if name in DEPENDENCY_DIRS:
        return "dependencies"
    if name.startswith("."):
        return "hidden"
    if SECRET_NAME_RE.search(name):
        return "secrets"
    return None


class IgnoreRules:
    """.gitignore patterns from one folder tree, plus the candidate's own
    exclude globs. Matching is a superset of git's: a negation is ignored, so
    a file git would ignore is always skipped here."""

    def __init__(self, root: Path, excludes: list[str] | None = None) -> None:
        self.root = root
        self.rules: list[tuple[str, str]] = []  # (base dir relative to root, pattern)
        for glob in excludes or []:
            self.rules.append(("", glob.strip().strip("/")))

    def load(self, directory: Path) -> None:
        """Add the rules of ``directory``/.gitignore, scoped to that directory."""
        ignore = directory / ".gitignore"
        if ignore.is_symlink() or not ignore.is_file():
            return
        try:
            lines = ignore.read_text(errors="replace").splitlines()
        except OSError:
            return
        base = "" if directory == self.root else directory.relative_to(self.root).as_posix()
        for raw in lines:
            line = raw.strip()
            if not line or line.startswith(("#", "!")):
                continue
            self.rules.append((base, line.rstrip("/")))

    def ignored(self, path: Path) -> bool:
        rel = path.relative_to(self.root).as_posix()
        for base, pattern in self.rules:
            if base and not rel.startswith(base + "/"):
                continue
            sub = rel[len(base) + 1 :] if base else rel
            if "/" in pattern:
                if fnmatch.fnmatch(sub, pattern.lstrip("/")) or fnmatch.fnmatch(
                    sub, pattern.lstrip("/") + "/*"
                ):
                    return True
            elif any(fnmatch.fnmatch(part, pattern) for part in sub.split("/")):
                return True
        return False


# Redaction: secrets first (they may contain an @), then contact details.
_SECRET_PATTERNS = (
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(-----END [A-Z ]*PRIVATE KEY-----|\Z)", re.S),
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"),
    re.compile(r"\b[rs]k_(?:live|test)_[A-Za-z0-9]{10,}"),
    re.compile(r"\b(?:hf_[A-Za-z0-9]{20,}|glpat-[A-Za-z0-9_-]{20,}|npm_[A-Za-z0-9]{30,})"),
    re.compile(r"\bpypi-[A-Za-z0-9_-]{16,}"),
    re.compile(r"https://(?:hooks\.slack\.com/services|discord(?:app)?\.com/api/webhooks)/\S+"),
    re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{30,}"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{16,}"),
    re.compile(r"[a-z][a-z0-9+.-]*://[^\s/]*:[^\s/]*@", re.I),  # credentials in a URL
)
_ASSIGNED_SECRET = re.compile(
    r"(?i)\b([A-Za-z0-9_]*(?:api[_-]?key|_key|secret|token|passw(?:or)?d|pwd|access[_-]?key)[A-Za-z0-9_]*)"
    r"([\"']?\s*[:=]\s*)([\"']?)[^\s\"',;]{6,}\3"
)
EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
PHONE_RE = re.compile(
    r"(?<![\w.-])(?:\+\d{1,3}[\s.-]?)?\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}(?![\w.-])"
    r"|(?<![\w.-])\+\d{1,3}(?:[\s.-]?\d{2,4}){2,4}(?![\w.-])"
)
REDACTED = "[redacted]"


def redact(text: str) -> str:
    """The text with secrets, email addresses and phone numbers masked."""
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub(REDACTED, text)
    text = _ASSIGNED_SECRET.sub(lambda m: f"{m.group(1)}{m.group(2)}{REDACTED}", text)
    text = EMAIL_RE.sub("[email]", text)
    return PHONE_RE.sub("[phone]", text)


def contact_detail(text: str) -> bool:
    """True when a fact would carry contact details; such facts are dropped."""
    return bool(EMAIL_RE.search(text) or PHONE_RE.search(text)) or any(
        marker in text for marker in ("[email]", "[phone]", REDACTED)
    )
