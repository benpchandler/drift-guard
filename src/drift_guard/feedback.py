"""Ben's accuracy labels on the guard's own judgments ("drift: feedback ..." prompts). Pure: vocabulary and semantics.

Vocabulary (first word after "drift: feedback", case-insensitive; anything after it is kept as a note):
  right | correct | accurate | good | yes   the guard's last call was correct
  wrong | incorrect | bad | no              the guard's last call was incorrect
  missed | miss                             I drifted and the guard said nothing
  (anything else)                           free-text note, stored but not scored

What a label means depends on what the guard did on the judgment it attaches to:
  warned/blocked + right   -> true_positive
  warned/blocked + wrong   -> false_positive   (also resets that drift track, so the same drift is not re-flagged)
  quiet          + right   -> true_negative
  quiet          + wrong   -> missed_drift
  quiet          + missed  -> missed_drift
  warned/blocked + missed  -> note             (contradictory: the guard did flag it; kept as text only)
  no judgment yet          -> note, or missed_drift for "missed"
"""

from dataclasses import dataclass
from enum import StrEnum

from drift_guard.drift import Action


class Verdict(StrEnum):
    RIGHT = "right"
    WRONG = "wrong"
    MISSED = "missed"
    NOTE = "note"


class Outcome(StrEnum):
    TRUE_POSITIVE = "true_positive"
    FALSE_POSITIVE = "false_positive"
    TRUE_NEGATIVE = "true_negative"
    MISSED_DRIFT = "missed_drift"
    NOTE = "note"


VERDICT_WORDS: dict[str, Verdict] = {
    **dict.fromkeys(("right", "correct", "accurate", "good", "yes"), Verdict.RIGHT),
    **dict.fromkeys(("wrong", "incorrect", "bad", "no"), Verdict.WRONG),
    **dict.fromkeys(("missed", "miss"), Verdict.MISSED),
}
_PUNCTUATION = ",.:;!?-"


@dataclass(frozen=True)
class Label:
    verdict: Verdict
    note: str


def parse(text: str) -> Label:
    """Split "wrong, this is the same task" into (WRONG, "this is the same task"). Unknown first word: a note."""
    stripped = text.strip()
    assert stripped, "a feedback escape always carries text"
    first, _, rest = stripped.partition(" ")
    verdict = VERDICT_WORDS.get(first.strip(_PUNCTUATION).lower())
    if verdict is None:
        return Label(Verdict.NOTE, stripped)
    return Label(verdict, rest.strip().lstrip(_PUNCTUATION).strip())


def outcome(verdict: Verdict, action: Action | None) -> Outcome:
    """Score a verdict against what the guard did (None: the session has no judgment to attach to)."""
    if verdict is Verdict.NOTE:
        return Outcome.NOTE
    if action is None:
        return Outcome.MISSED_DRIFT if verdict is Verdict.MISSED else Outcome.NOTE
    flagged = action is not Action.NONE
    if verdict is Verdict.RIGHT:
        return Outcome.TRUE_POSITIVE if flagged else Outcome.TRUE_NEGATIVE
    if verdict is Verdict.WRONG:
        return Outcome.FALSE_POSITIVE if flagged else Outcome.MISSED_DRIFT
    assert verdict is Verdict.MISSED
    return Outcome.NOTE if flagged else Outcome.MISSED_DRIFT


def confirmation(result: Outcome, excerpt: str) -> str:
    """The line Ben sees in place of a Claude turn."""
    if not excerpt:
        return "Recorded feedback (no guard judgment in this session yet)."
    quoted = f"'{excerpt}'"
    return {
        Outcome.TRUE_POSITIVE: f"Recorded: guard was right on {quoted}.",
        Outcome.FALSE_POSITIVE: f"Recorded: guard was wrong on {quoted}. Drift reset.",
        Outcome.TRUE_NEGATIVE: f"Recorded: guard was right to stay quiet on {quoted}.",
        Outcome.MISSED_DRIFT: f"Recorded: guard missed drift on {quoted}.",
        Outcome.NOTE: f"Recorded note on {quoted}.",
    }[result]
