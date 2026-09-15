"""Only synthetic keys and fake console events; no external requests."""
import importlib.util
import io
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "elsevier_input_runner", Path(__file__).with_name("run_elsevier_diagnostics.py")
)
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)
FAKE = "synthetic-not-a-real-api-key"


def input_events(events):
    stream = io.StringIO()
    chars = iter(events)
    value = runner.masked_input("Key: ", lambda: next(chars), stream)
    return value, stream.getvalue()


def test_masks_each_character_without_echo():
    value, output = input_events(FAKE + "\r")
    assert value == FAKE
    assert output == "Key: " + "*" * len(FAKE) + "\n"
    assert FAKE not in output


def test_backspace_and_extended_key():
    value, output = input_events("\bAB\bx\x00K\r")
    assert value == "Ax"
    assert output == "Key: **\b \b*\n"


@pytest.mark.parametrize("event,exception", [("\x03", KeyboardInterrupt), ("\x1b", EOFError), ("\x1a", EOFError)])
def test_cancel(event, exception):
    with pytest.raises(exception):
        input_events("abc" + event)


@pytest.mark.parametrize("events", ["中\r", "a\t\r", "a" * 513 + "\r"])
def test_unsafe_console_input(events):
    with pytest.raises(ValueError):
        input_events(events)


def confirmed(values):
    values = iter(values)
    return runner.confirmed_api_key(lambda prompt: next(values))


def test_matching_inputs():
    assert confirmed([FAKE, FAKE]) == FAKE


@pytest.mark.parametrize("first,second", [(FAKE, "different"), (FAKE, ""), (FAKE, "中")])
def test_mismatch_no_secret_in_error(first, second):
    with pytest.raises(ValueError) as caught:
        confirmed([first, second])
    assert FAKE not in str(caught.value)
    if second:
        assert second not in str(caught.value)


@pytest.mark.parametrize("value", ["", " " + FAKE, FAKE + " ", '"' + FAKE + '"', "'" + FAKE, FAKE + "\n", "中", "a" * 513])
def test_invalid_key_rejected_before_confirmation(value):
    with pytest.raises(ValueError):
        confirmed([value])


def test_no_echo_fallback_without_interactive_console(monkeypatch):
    monkeypatch.setattr(runner.sys, "stdin", io.StringIO())
    with pytest.raises(RuntimeError, match="不会降级为明文输入"):
        runner.masked_input("Key: ", stream=io.StringIO())


def test_confirmation_failure_never_starts_mcp(monkeypatch, capsys):
    import asyncio

    monkeypatch.setattr("builtins.input", lambda prompt: "APPROVED")
    values = iter([FAKE, "different"])
    monkeypatch.setattr(runner, "masked_input", lambda prompt: next(values))
    monkeypatch.setattr(runner, "stdio_client", lambda *a, **kw: pytest.fail("MCP must not start"))
    asyncio.run(runner.main(live=True))
    output = capsys.readouterr().out
    assert '"api_requests":0' in output and FAKE not in output
