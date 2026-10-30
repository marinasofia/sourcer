"""Run model calls through the Claude Code CLI instead of the Messages API.

  SOURCER_LLM=claude-code

For a single operator who has a Claude subscription and no API key: each call
is one scripted ``claude -p`` run on the operator's own login. The client
mimics the one ``messages.create`` surface ``llm.json_call`` uses, so every
caller works unchanged. Batches need the API and are refused.

Every call is locked down: no tools (a call that asks for web search gets the
web search and fetch tools only), no MCP servers, no user or plugin
settings (so no hooks fire), no slash commands, no saved session, and an
empty working directory. ``ANTHROPIC_API_KEY`` is removed from the child
environment so the CLI uses the subscription login, not a key, and extended
thinking is off.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess  # nosec B404  fixed argv, no shell
import tempfile
from types import SimpleNamespace
from typing import Any

from .log import get_logger

log = get_logger(__name__)

# Models that reject a request to turn thinking off (API 400 asking for
# "between_tools"); the CLI's MAX_THINKING_TOKENS=0 sends exactly that request.
THINKING_ALWAYS_ON = re.compile(r"claude-(?:sonnet-5-5|opus-5-5|fable-5|mythos-5)")
REFUSES_THINKING_OFF = "To turn thinking off on this model"
TIMEOUT_S = int(os.environ.get("SOURCER_CLAUDE_CLI_TIMEOUT", "180"))
# How much the model reasons before it answers (the CLI's --effort). A short
# follow up call that returns a few edits needs far less than a first draft.
EFFORTS = frozenset({"low", "medium", "high", "xhigh", "max"})


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    return "".join(block.get("text", "") for block in content if isinstance(block, dict))


def _prompt(messages: list[dict[str, Any]]) -> str:
    """One prompt from a message list. A corrective retry arrives as
    user, assistant, user; the earlier turns become quoted context."""
    if len(messages) == 1:
        return _text(messages[0]["content"])
    parts = []
    for m in messages[:-1]:
        label = "Your previous reply" if m["role"] == "assistant" else "The request"
        parts.append(f"{label}:\n{_text(m['content'])}")
    parts.append(_text(messages[-1]["content"]))
    return "\n\n".join(parts)


FALLBACK_BINARIES = ("/opt/homebrew/bin/claude", "/usr/local/bin/claude")
WEB_TOOLS = "WebSearch,WebFetch"


# what the CLI needs to run and sign in; the run's other secrets (Telegram,
# Getro, AWS, GitHub) stay out of a process that reads untrusted postings
CLI_ENV_KEYS = frozenset(
    {"PATH", "HOME", "USER", "LOGNAME", "SHELL", "TMPDIR", "LANG", "TERM",
     "HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY", "https_proxy", "http_proxy", "no_proxy",
     "SSL_CERT_FILE", "NODE_EXTRA_CA_CERTS", "XDG_CONFIG_HOME"}
)  # fmt: skip


def _cli_env() -> dict[str, str]:
    """The CLI's environment: the allowlist, its own CLAUDE_ settings, locale."""
    return {
        k: v for k, v in os.environ.items() if k in CLI_ENV_KEYS or k.startswith(("CLAUDE_", "LC_"))
    }


def _find_binary(exclude: str | None = None) -> str | None:
    """The claude CLI on PATH, else a known install location."""
    for candidate in (shutil.which("claude"), *FALLBACK_BINARIES):
        if candidate and candidate != exclude and os.path.exists(candidate):
            return candidate
    return None


class _Messages:
    accepts_effort = True

    def __init__(self, binary: str) -> None:
        self.binary = binary

    def _run(self, argv: list[str], cwd: str, env: dict[str, str], prompt: str = "") -> Any:
        """Run the CLI. A self update can briefly remove the binary it was found
        at; look it up again once before giving up on the call."""
        try:
            return subprocess.run(  # noqa: S603  # nosec B603
                argv, capture_output=True, text=True, timeout=TIMEOUT_S, cwd=cwd, env=env,
                check=False, input=prompt,
            )  # fmt: skip
        except FileNotFoundError:
            found = _find_binary(exclude=self.binary)
            if not found:
                raise RuntimeError(f"claude CLI missing at {self.binary}") from None
            self.binary = argv[0] = found
            return subprocess.run(  # noqa: S603  # nosec B603
                argv, capture_output=True, text=True, timeout=TIMEOUT_S, cwd=cwd, env=env,
                check=False, input=prompt,
            )  # fmt: skip

    def _call(
        self, argv: list[str], env: dict[str, str], system: str = "", prompt: str = ""
    ) -> dict[str, Any]:
        """The prompt goes in on stdin and the system prompt in a private file,
        so neither the posting nor the candidate's evidence shows in ps."""
        with tempfile.TemporaryDirectory(prefix="sourcer-cc-") as cwd:
            system_file = os.path.join(cwd, "system.txt")
            fd = os.open(system_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as fh:
                fh.write(system)
            try:
                proc = self._run([*argv, "--system-prompt-file", system_file], cwd, env, prompt)
            except subprocess.TimeoutExpired as exc:
                raise RuntimeError(f"claude CLI timed out after {TIMEOUT_S}s") from exc
        try:
            out: dict[str, Any] = json.loads(proc.stdout)
        except json.JSONDecodeError as exc:
            detail = (proc.stderr or proc.stdout).strip()[-300:]
            raise RuntimeError(f"claude CLI failed ({proc.returncode}): {detail}") from exc
        return out

    @property
    def batches(self) -> Any:
        raise RuntimeError("batches need the Messages API; unset SOURCER_LLM=claude-code")

    def create(self, **params: Any) -> SimpleNamespace:
        # a call that asks for the web search server tool gets the CLI's own
        # web tools and nothing else: no files, no shell
        web = any(
            str(t.get("type", "")).startswith("web_search") for t in params.get("tools") or []
        )
        argv = [
            self.binary, "-p",
            "--model", params["model"],
            "--tools", WEB_TOOLS if web else "",
            *(["--allowedTools", WEB_TOOLS] if web else []),
            "--setting-sources", "project",
            "--strict-mcp-config",
            "--disable-slash-commands",
            "--no-session-persistence",
            "--output-format", "json",
        ]  # fmt: skip
        effort = params.get("effort")
        if effort is not None:
            if effort not in EFFORTS:
                raise ValueError(f"effort must be one of {sorted(EFFORTS)}")
            argv += ["--effort", effort]
        env = _cli_env()
        # Extraction and scoring need no extended thinking; it multiplies output
        # tokens. Newer models refuse to switch it off, so they run as they are.
        system, prompt = _text(params.get("system") or ""), _prompt(params["messages"])
        if not THINKING_ALWAYS_ON.search(params["model"]):
            env["MAX_THINKING_TOKENS"] = "0"
        out = self._call(argv, env, system, prompt)
        if (
            out.get("is_error")
            and REFUSES_THINKING_OFF in str(out.get("result"))
            and "MAX_THINKING_TOKENS" in env
        ):
            del env["MAX_THINKING_TOKENS"]  # a model not on the list yet: once more as it is
            out = self._call(argv, env, system, prompt)
        if out.get("is_error"):
            raise RuntimeError(f"claude CLI error: {str(out.get('result'))[:300]}")
        u = out.get("usage") or {}
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text=out.get("result") or "")],
            stop_reason=out.get("stop_reason"),
            usage=SimpleNamespace(
                input_tokens=u.get("input_tokens", 0),
                output_tokens=u.get("output_tokens", 0),
                cache_creation_input_tokens=u.get("cache_creation_input_tokens", 0),
                cache_read_input_tokens=u.get("cache_read_input_tokens", 0),
            ),
        )


class ClaudeCodeClient:
    """Stand-in for ``anthropic.Anthropic`` backed by the ``claude`` CLI."""

    def __init__(self, binary: str | None = None) -> None:
        found = binary or os.environ.get("SOURCER_CLAUDE_BIN") or _find_binary()
        if not found:
            raise RuntimeError("SOURCER_LLM=claude-code but no claude CLI on PATH")
        self.messages = _Messages(found)
