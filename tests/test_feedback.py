import pytest

from drift_guard.drift import Action
from drift_guard.feedback import Label, Outcome, Verdict, confirmation, outcome, parse


@pytest.mark.parametrize(
    ("text", "label"),
    [
        ("wrong, this is the same task", Label(Verdict.WRONG, "this is the same task")),
        ("Right", Label(Verdict.RIGHT, "")),
        ("correct. good catch", Label(Verdict.RIGHT, "good catch")),
        ("MISSED - I wandered into lunch plans", Label(Verdict.MISSED, "I wandered into lunch plans")),
        ("no: it is a follow-up", Label(Verdict.WRONG, "it is a follow-up")),
        ("the warning text is too long", Label(Verdict.NOTE, "the warning text is too long")),
        ("rightly so", Label(Verdict.NOTE, "rightly so")),
    ],
)
def test_parse(text: str, label: Label) -> None:
    assert parse(text) == label


@pytest.mark.parametrize(
    ("verdict", "action", "expected"),
    [
        (Verdict.RIGHT, Action.WARN, Outcome.TRUE_POSITIVE),
        (Verdict.RIGHT, Action.BLOCK, Outcome.TRUE_POSITIVE),
        (Verdict.WRONG, Action.WARN, Outcome.FALSE_POSITIVE),
        (Verdict.RIGHT, Action.NONE, Outcome.TRUE_NEGATIVE),
        (Verdict.WRONG, Action.NONE, Outcome.MISSED_DRIFT),
        (Verdict.MISSED, Action.NONE, Outcome.MISSED_DRIFT),
        (Verdict.MISSED, Action.WARN, Outcome.NOTE),
        (Verdict.NOTE, Action.WARN, Outcome.NOTE),
        (Verdict.MISSED, None, Outcome.MISSED_DRIFT),
        (Verdict.WRONG, None, Outcome.NOTE),
    ],
)
def test_outcome(verdict: Verdict, action: Action | None, expected: Outcome) -> None:
    assert outcome(verdict, action) is expected


def test_confirmation_uses_bens_words() -> None:
    assert confirmation(Outcome.FALSE_POSITIVE, "banana bread?") == "Recorded: guard was wrong on 'banana bread?'. Drift reset."
    assert confirmation(Outcome.TRUE_POSITIVE, "x") == "Recorded: guard was right on 'x'."
    assert "no guard judgment" in confirmation(Outcome.NOTE, "")
