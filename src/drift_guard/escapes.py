"""The user's explicit escapes, typed as a prompt prefix. Parsed here and nowhere else.

Every escape starts with "drift:" followed by a subcommand:
  drift: ack [note]                  keep the intent, clear the drift score
  drift: new intent <text>           replace the intent and reset drift
  drift: feedback <label> [note]     label the guard's accuracy
Anything else after "drift:" is a usage request, so a mistyped escape is answered rather than judged.
"""

import re
from dataclasses import dataclass
from enum import StrEnum

PREFIX = "drift:"
ACK_COMMAND = f"{PREFIX} ack"
NEW_INTENT_COMMAND = f"{PREFIX} new intent"
FEEDBACK_COMMAND = f"{PREFIX} feedback"
USAGE = (
    f"'{ACK_COMMAND}' clears the drift score, '{NEW_INTENT_COMMAND} <text>' switches tasks, "
    f"'{FEEDBACK_COMMAND} right|wrong|missed [note]' labels the guard's last call."
)

_PREFIX = re.compile(r"^\s*drift\s*:\s*(?P<rest>.*)\Z", re.IGNORECASE | re.DOTALL)
_NEW_INTENT = re.compile(r"^new\s+intent\b\s*:?\s*(?P<rest>.*)\Z", re.IGNORECASE | re.DOTALL)
_FEEDBACK = re.compile(r"^feedback\b\s*:?\s*(?P<rest>.*)\Z", re.IGNORECASE | re.DOTALL)
_ACK = re.compile(r"^ack\b\s*[:,.-]?\s*(?P<rest>.*)\Z", re.IGNORECASE | re.DOTALL)


class EscapeKind(StrEnum):
    NEW_INTENT = "new_intent"
    ACK_DRIFT = "ack_drift"
    FEEDBACK = "feedback"
    USAGE = "usage"


@dataclass(frozen=True)
class Escape:
    kind: EscapeKind
    text: str  # the new intent, the (optional) ack note, the feedback label and note, or the unrecognized rest


def parse(prompt: str) -> Escape | None:
    """Return the escape a prompt starts with, or None when it does not start with "drift:".

    "drift: new intent" or "drift: feedback" with no text, or an unknown subcommand, is a USAGE escape.
    """
    prefixed = _PREFIX.match(prompt)
    if prefixed is None:
        return None
    rest = prefixed.group("rest").strip()
    for kind, pattern in ((EscapeKind.NEW_INTENT, _NEW_INTENT), (EscapeKind.FEEDBACK, _FEEDBACK)):
        match = pattern.match(rest)
        if match:
            text = match.group("rest").strip()
            return Escape(kind, text) if text else Escape(EscapeKind.USAGE, rest)
    match = _ACK.match(rest)
    if match:
        return Escape(EscapeKind.ACK_DRIFT, match.group("rest").strip())
    return Escape(EscapeKind.USAGE, rest)


def is_slash_command(prompt: str) -> bool:
    """A "/command" prompt drives the harness, not the task; it is neither an intent nor a drift."""
    return prompt.lstrip().startswith("/")
