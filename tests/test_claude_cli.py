"""The Claude Code backend: a locked down CLI call that looks like messages.create."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

from sourcer import claude_cli, config, llm


def fake_claude(tmp_path: Path, reply: dict) -> Path:
    """A stand-in binary that records its argv and env, then prints ``reply``."""
    log = tmp_path / "call.json"
    script = tmp_path / "claude"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        "a = sys.argv[1:]\n"
        "f = a[a.index('--system-prompt-file') + 1] if '--system-prompt-file' in a else None\n"
        f"open({str(log)!r}, 'w').write(json.dumps({{'argv': a, "
        "'key': os.environ.get('ANTHROPIC_API_KEY'), 'cwd': os.getcwd(), "
        "'stdin': sys.stdin.read(), 'system': open(f).read() if f else None, "
        "'telegram': os.environ.get('SOURCER_TELEGRAM_TOKEN'), "
        "'think': os.environ.get('MAX_THINKING_TOKENS')}))\n"
        f"print({json.dumps(json.dumps(reply))})\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return script


def reply(result: str, **over: object) -> dict:
    return {
        "is_error": False,
        "result": result,
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 400, "output_tokens": 50},
        **over,
    }


def test_json_call_runs_through_the_cli_locked_down(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    binary = fake_claude(tmp_path, reply('{"score": 88, "rationale": "fits"}'))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "should-not-leak")
    monkeypatch.setenv("SOURCER_TELEGRAM_TOKEN", "should-not-leak-either")
    monkeypatch.setattr(config, "COST_CAP_USD", 1.0)
    client = claude_cli.ClaudeCodeClient(str(binary))
    usage = llm.Usage()
    data, err = llm.json_call(
        client,  # type: ignore[arg-type]
        "SYSTEM",
        "Score this",
        {"score": int, "rationale": str},
        usage,
    )
    assert err is None and data == {"score": 88, "rationale": "fits"}
    assert usage.prompt_tokens == 400 and usage.completion_tokens == 50
    call = json.loads((tmp_path / "call.json").read_text())
    argv = call["argv"]
    assert argv[0] == "-p" and "Score this" not in argv and "SYSTEM" not in argv  # not in ps
    assert call["stdin"] == "Score this" and call["system"] == "SYSTEM"
    assert call["telegram"] is None  # only what the CLI needs
    assert argv[argv.index("--tools") + 1] == ""
    for flag in ("--strict-mcp-config", "--disable-slash-commands", "--no-session-persistence"):
        assert flag in argv
    assert argv[argv.index("--setting-sources") + 1] == "project"
    assert call["key"] is None  # subscription login, never the API key
    assert call["think"] == "0"
    assert not os.path.exists(call["cwd"])  # ran in a throwaway directory


def test_retry_turns_become_one_prompt() -> None:
    prompt = claude_cli._prompt(
        [
            {"role": "user", "content": "Score this"},
            {"role": "assistant", "content": "not json"},
            {"role": "user", "content": "Return valid JSON"},
        ]
    )
    assert prompt.startswith("The request:\nScore this")
    assert "Your previous reply:\nnot json" in prompt
    assert prompt.endswith("Return valid JSON")


def test_cli_errors_and_batches_fail_clearly(tmp_path: Path) -> None:
    bad = claude_cli.ClaudeCodeClient(
        str(fake_claude(tmp_path, reply("Not logged in", is_error=True)))
    )
    with pytest.raises(RuntimeError, match="Not logged in"):
        bad.messages.create(model="m", system="s", messages=[{"role": "user", "content": "x"}])
    with pytest.raises(RuntimeError, match="Messages API"):
        _ = bad.messages.batches


def test_get_client_switches_on_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "LLM_BACKEND", "claude-code")
    monkeypatch.setenv("SOURCER_CLAUDE_BIN", str(fake_claude(tmp_path, reply("{}"))))
    assert isinstance(llm.get_client(), claude_cli.ClaudeCodeClient)


def test_a_binary_removed_by_a_self_update_is_found_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    good = fake_claude(tmp_path, reply('{"score": 70, "rationale": "ok"}'))
    gone = tmp_path / "old" / "claude"
    monkeypatch.setattr(claude_cli, "FALLBACK_BINARIES", (str(good),))
    monkeypatch.setattr(claude_cli.shutil, "which", lambda name: None)
    monkeypatch.setattr(config, "COST_CAP_USD", 1.0)
    client = claude_cli.ClaudeCodeClient(str(gone))
    data, err = llm.json_call(
        client,  # type: ignore[arg-type]
        "SYSTEM", "Score this", {"score": int, "rationale": str}, llm.Usage(),
    )  # fmt: skip
    assert err is None and data == {"score": 70, "rationale": "ok"}
    assert client.messages.binary == str(good)


def test_no_binary_anywhere_is_a_runtime_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(claude_cli, "FALLBACK_BINARIES", ())
    monkeypatch.setattr(claude_cli.shutil, "which", lambda name: None)
    client = claude_cli.ClaudeCodeClient(str(tmp_path / "missing"))
    with pytest.raises(RuntimeError, match="claude CLI missing"):
        client.messages.create(
            model="m", system="s", messages=[{"role": "user", "content": "x"}], max_tokens=10
        )


def test_newer_models_keep_their_thinking_and_older_ones_switch_it_off(tmp_path: Path) -> None:
    binary = fake_claude(tmp_path, reply("ok"))
    client = claude_cli.ClaudeCodeClient(str(binary))
    for model, think in (("claude-sonnet-5-5", None), ("claude-haiku-4-5-20251001", "0")):
        client.messages.create(
            model=model, max_tokens=10, messages=[{"role": "user", "content": "hi"}]
        )
        assert json.loads((tmp_path / "call.json").read_text())["think"] == think


def test_a_model_that_refuses_thinking_off_is_asked_again_without_it(tmp_path: Path) -> None:
    script = tmp_path / "claude"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os\n"
        f"open({str(tmp_path / 'calls')!r}, 'a')"
        ".write(str(os.environ.get('MAX_THINKING_TOKENS')) + '\\n')\n"
        "if os.environ.get('MAX_THINKING_TOKENS') == '0':\n"
        "    print(json.dumps({'is_error': True, 'result': "
        "'API Error: 400 To turn thinking off on this model, send between_tools'}))\n"
        "else:\n"
        "    print(json.dumps({'is_error': False, 'result': 'ok', 'usage': {}}))\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    client = claude_cli.ClaudeCodeClient(str(script))
    resp = client.messages.create(
        model="claude-new-9", max_tokens=10, messages=[{"role": "user", "content": "hi"}]
    )
    assert resp.content[0].text == "ok"
    assert (tmp_path / "calls").read_text().split() == ["0", "None"]


def test_effort_reaches_the_cli_only_when_asked_and_only_known_levels(tmp_path: Path) -> None:
    binary = fake_claude(tmp_path, reply('{"ok": true}'))
    client = claude_cli.ClaudeCodeClient(str(binary))
    _, err = llm.json_call(client, "S", "u", {"ok": bool}, llm.Usage(), effort="low")  # type: ignore[arg-type]
    assert err is None
    argv = json.loads((tmp_path / "call.json").read_text())["argv"]
    assert argv[argv.index("--effort") + 1] == "low"
    llm.json_call(client, "S", "u", {"ok": bool}, llm.Usage())  # type: ignore[arg-type]
    assert "--effort" not in json.loads((tmp_path / "call.json").read_text())["argv"]
    with pytest.raises(ValueError, match="effort"):
        client.messages.create(
            model="m", max_tokens=1, effort="--tools", messages=[{"role": "user", "content": "x"}]
        )


def test_the_messages_api_path_never_sends_effort() -> None:
    sent: dict = {}

    class Api:
        class messages:  # the anthropic client's own attribute name
            @staticmethod
            def create(**params: object) -> object:
                sent.update(params)
                return SimpleNamespace(
                    content=[SimpleNamespace(type="text", text='{"ok": true}')],
                    stop_reason="end_turn",
                    usage=SimpleNamespace(input_tokens=1, output_tokens=1),
                )

    assert llm.json_call(Api(), "S", "u", {"ok": bool}, llm.Usage(), effort="low")[1] is None  # type: ignore[arg-type]
    assert "effort" not in sent
