import pytest

from drift_guard.escapes import EscapeKind, is_slash_command, parse


@pytest.mark.parametrize(
    ("prompt", "kind", "text"),
    [
        ("drift: new intent ship the report page", EscapeKind.NEW_INTENT, "ship the report page"),
        ("drift: new intent: ship the report page", EscapeKind.NEW_INTENT, "ship the report page"),
        ("  Drift:  New Intent  refactor store\nwith details", EscapeKind.NEW_INTENT, "refactor store\nwith details"),
        ("drift: ack", EscapeKind.ACK_DRIFT, ""),
        ("DRIFT: ACK: quick detour for lunch plans", EscapeKind.ACK_DRIFT, "quick detour for lunch plans"),
        ("drift:ack - what is the weather", EscapeKind.ACK_DRIFT, "what is the weather"),
    ],
)
def test_parse_escapes(prompt: str, kind: EscapeKind, text: str) -> None:
    esc = parse(prompt)
    assert esc is not None
    assert (esc.kind, esc.text) == (kind, text)


@pytest.mark.parametrize(
    "prompt",
    ["new intent: ship it", "ack drift", "feedback: wrong", "please drift: ack", "the drift score is high", "driftwood: ack"],
)
def test_not_escapes(prompt: str) -> None:
    assert parse(prompt) is None


@pytest.mark.parametrize(
    ("prompt", "rest"),
    [
        ("drift:", ""),
        ("drift: new intent", "new intent"),
        ("drift: feedback", "feedback"),
        ("drift: acknowledge", "acknowledge"),
        ("drift: help", "help"),
    ],
)
def test_usage(prompt: str, rest: str) -> None:
    esc = parse(prompt)
    assert esc is not None and (esc.kind, esc.text) == (EscapeKind.USAGE, rest)


def test_slash_command() -> None:
    assert is_slash_command("  /compact") and not is_slash_command("fix /etc/hosts")


@pytest.mark.parametrize(
    ("prompt", "text"),
    [
        ("drift: feedback wrong, same task", "wrong, same task"),
        ("  DRIFT:FEEDBACK right", "right"),
        ("Drift : feedback: great tool", "great tool"),
    ],
)
def test_parse_feedback(prompt: str, text: str) -> None:
    esc = parse(prompt)
    assert esc is not None and esc.kind is EscapeKind.FEEDBACK and esc.text == text
