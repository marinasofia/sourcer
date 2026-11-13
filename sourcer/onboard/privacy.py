"""What onboarding may read from a local folder, and the redactor every
excerpt passes through before it is previewed, stored or sent.

The folder rules are deliberately conservative: a file is skipped when any
rule matches, and a .gitignore negation ("!keep.txt") never brings a file back.
"""

from __future__ import annotations

import fnmatch
import os
import re
import stat
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
    secret or key by its name, a hidden file, a binary type, not a regular
    file (a FIFO, socket or device can block when opened), or its size."""
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
        info = path.lstat()
    except OSError:
        return "unreadable"
    if not stat.S_ISREG(info.st_mode):
        reason: str | None = "special files"
    else:
        reason = "large files" if info.st_size > max_bytes else None
    return reason


def content_reason(path: Path) -> str | None:
    """ "binaries" when the first bytes hold a NUL. Only for a file that is
    about to be read and passed every other rule."""
    head = read_regular(path, 4096)
    if head is None:
        return "unreadable"
    return "binaries" if b"\0" in head else None


def read_regular(path: Path, limit: int) -> bytes | None:
    """Up to ``limit`` bytes of ``path`` when it is a regular file. Opened
    without following a link and without blocking, then checked with fstat,
    so a FIFO or a file swapped for a link after the walk is never read."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(path, flags)
    except OSError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        with os.fdopen(fd, "rb") as fh:
            fd = -1
            return fh.read(limit)
    except OSError:
        return None
    finally:
        if fd >= 0:
            os.close(fd)


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
                rooted = pattern.lstrip("/")
                # "**/x" also names x at the top, as in git
                forms = [rooted, rooted[3:]] if rooted.startswith("**/") else [rooted]
                if any(fnmatch.fnmatch(sub, f) or fnmatch.fnmatch(sub, f + "/*") for f in forms):
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
    # an Authorization header value of any scheme (Basic, Digest, Token)
    re.compile(r"(?i)\bauthorization[\"']?\s*[:=]\s*[\"']?[A-Za-z]+\s+[A-Za-z0-9._~+/=-]{4,}"),
    # credentials in a URL; every part is bounded so a long run of dots and
    # letters cannot make the match retry from every position
    re.compile(r"\b[a-z][a-z0-9+.-]{0,30}://[^\s/@:]{0,256}:[^\s/@]{0,256}@", re.I),
)
_CREDENTIAL_NAME = (
    r"[A-Za-z0-9_]{0,64}(?:api[_-]?key|_key|secret|token|(?<![a-z])pass(?:w(?:or)?d)?(?![a-z])|pwd"
    r"|access[_-]?key)[A-Za-z0-9_]{0,64}"
)
_ASSIGNED_SECRET = re.compile(
    rf"(?i)\b({_CREDENTIAL_NAME})([\"']?\s*[:=]\s*)([\"']?)[^\s\"',;]{{6,}}\3"
)
# a quoted value may hold spaces: password = "correct horse battery staple"
_QUOTED_SECRET = re.compile(
    rf"(?i)\b({_CREDENTIAL_NAME})([\"']?\s*[:=]\s*)([\"'])[^\"'\n]{{1,500}}\3"
)
# a bare 40 character key in the AWS secret format (base64 letters, mixed
# case and digits); all hex runs of that length are commit hashes and stay
_FORTY = re.compile(r"(?<![A-Za-z0-9/+=])[A-Za-z0-9/+]{40}(?![A-Za-z0-9/+=])")
# bounded quantifiers keep the scan linear on hostile text such as "a.a.a.a"
EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9.-]{1,253}\.[A-Za-z]{2,24}\b")
OBFUSCATED_EMAIL_RE = re.compile(
    r"(?i)\b[A-Za-z0-9._%+-]{1,64}\s*[\[(]\s*at\s*[\])]\s*[A-Za-z0-9-]{1,63}"
    r"(?:\s*[\[(]\s*dot\s*[\])]\s*[A-Za-z0-9-]{1,63}){1,4}"
)
PHONE_RE = re.compile(
    r"(?<![\w.-])(?:\+\d{1,3}[\s.-]?)?\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}(?![\w.-])"
    r"|(?<![\w.-])\+\d{1,3}(?:[\s.-]?\d{2,4}){2,4}(?![\w.-])"
)
REDACTED = "[redacted]"


def _looks_like_aws_secret(match: re.Match[str]) -> str:
    token = match.group(0)
    hexlike = all(c in "0123456789abcdefABCDEF" for c in token)
    mixed = any(c.isupper() for c in token) and any(c.islower() for c in token)
    mixed = mixed and any(c.isdigit() for c in token)
    return REDACTED if mixed and not hexlike else token


# text longer than the caller keeps is cut to this much past the limit before
# redaction, so a secret that crosses the final cut is still masked whole
REDACT_MARGIN = 8192


def redact(text: str, limit: int | None = None) -> str:
    """The text with secrets, email addresses and phone numbers masked.

    With ``limit``, only the first ``limit`` characters are returned, and the
    input is cut a margin past that before any pattern runs, so a huge hostile
    page costs no more than the excerpt it becomes.
    """
    if limit is not None:
        text = text[: limit + REDACT_MARGIN]
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub(REDACTED, text)
    text = _QUOTED_SECRET.sub(lambda m: f"{m.group(1)}{m.group(2)}{REDACTED}", text)
    text = _ASSIGNED_SECRET.sub(lambda m: f"{m.group(1)}{m.group(2)}{REDACTED}", text)
    text = _FORTY.sub(_looks_like_aws_secret, text)
    text = OBFUSCATED_EMAIL_RE.sub("[email]", text)
    text = EMAIL_RE.sub("[email]", text)
    text = PHONE_RE.sub("[phone]", text)
    return text if limit is None else text[:limit]


def contact_detail(text: str) -> bool:
    """True when a fact would carry contact details; such facts are dropped."""
    found = EMAIL_RE.search(text) or OBFUSCATED_EMAIL_RE.search(text) or PHONE_RE.search(text)
    return bool(found) or any(marker in text for marker in ("[email]", "[phone]", REDACTED))
