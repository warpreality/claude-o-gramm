from tg_claude.sessions import _input_text
from tg_claude.transcript import to_events


def test_assistant_text_and_tools():
    rec = {"type": "assistant", "message": {"content": [
        {"type": "text", "text": "Привет"},
        {"type": "tool_use", "name": "Bash", "input": {"command": "ls -la", "description": "List files"}},
        {"type": "tool_use", "name": "Read", "input": {"file_path": "/repo/src/a.py"}},
        {"type": "tool_use", "name": "AskUserQuestion", "input": {}},
    ]}}
    ev = to_events(rec, "/repo")
    assert [e.kind for e in ev] == ["text", "tool", "tool"]
    assert "List files" in ev[1].text and "src/a.py" in ev[2].text


def test_sidechain_and_meta_skipped():
    assert to_events({"type": "assistant", "isSidechain": True, "message": {"content": [{"type": "text", "text": "x"}]}}, "/") == []
    assert to_events({"type": "user", "isMeta": True, "message": {"content": "x"}}, "/") == []


def test_unknown_command_and_local_output():
    ev = to_events({"type": "system", "subtype": "informational", "content": "Unknown command: /project"}, "/")
    assert ev[0].kind == "local" and "Unknown command" in ev[0].text
    ev = to_events({"type": "user", "message": {"content": "<local-command-stdout>Set model to \x1b[1mopus\x1b[0m</local-command-stdout>"}}, "/")
    assert ev[0].kind == "local" and ev[0].text == "<pre>Set model to opus</pre>"


def test_api_error_only_first_and_last_attempt():
    def err(n):
        return to_events({"type": "system", "subtype": "api_error", "error": {"formatted": "529 Overloaded"}, "retryAttempt": n, "maxRetries": 10}, "/")
    assert err(1) and not err(5) and err(10)


def test_input_text():
    assert _input_text("foo\n❯ Try \"fix lint errors\"\n──") == ""
    assert _input_text("❯ \n") == ""
    assert _input_text("❯ застрявший текст\n  продолжение") == "застрявший текст"
    # снимки реального экрана (capture-pane -e): серая подсказка и набранный текст
    assert _input_text("\x1b[39m❯\xa0\x1b[2mпокажи мои открытые MR\x1b[0m") == ""
    assert _input_text("\x1b[39m❯\xa0настоящий текст") == "настоящий текст"


def test_titles():
    ev = to_events({"type": "ai-title", "aiTitle": "GitLab authorization"}, "/")
    assert ev[0].kind == "title" and ev[0].text == "GitLab authorization" and not ev[0].custom
    ev = to_events({"type": "custom-title", "customTitle": "Мой тред"}, "/")
    assert ev[0].custom
