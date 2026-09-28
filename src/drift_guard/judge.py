"""The Jev boundary: what Jev is asked about a turn, and the typed answer that comes back.

Jev returns a probability distribution over the relevance levels in drift.LEVELS; drift.py owns what that means.
"""

import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

from typesafe_sdk import Score

from drift_guard.credentials import api_key
from drift_guard.drift import LEVELS

# Keep state small: Jev's accuracy drops as irrelevant detail grows, and cost is per input token.
MAX_INTENT_CHARS = 1500
MAX_TURN_CHARS = 2000
MAX_CONTEXT_CHARS = 800
# Bump whenever a question's wording or criteria change: judgments from different wordings are not comparable, so
# report and backfill only use events carrying the current version.
QUESTION_SET_VERSION = 2
PRICE_PER_INPUT_TOKEN_USD = 0.042 / 1_000_000  # jev-1.13 list price; output tokens are free

_LEVEL_CRITERIA = [
    "Unrelated: starts a different task or topic that does nothing for `intent`",
    "Tangential: same general area, but a side quest that `intent` does not need",
    "Supporting: a step toward `intent`, such as a clarification, an answer to a question, an approval, or a follow-up",
    "Direct: squarely works on `intent` itself",
]
assert len(_LEVEL_CRITERIA) == len(LEVELS)

HUMAN_QUESTIONS: dict[str, Score] = {
    "serves_intent": Score(
        instructions=(
            "`intent` is the task this coding-agent session was started for. `turn` is the user's newest message to "
            "the agent, and `previous_assistant_message` is what the agent said just before it. How much does `turn` "
            "serve `intent`? A short reply (yes, go ahead, continue, an answer to the agent's question) serves "
            "`intent` as much as the exchange in `previous_assistant_message` does."
        ),
        criteria=_LEVEL_CRITERIA,
    ),
}

AGENT_QUESTIONS: dict[str, Score] = {
    "serves_intent": Score(
        instructions=(
            "`intent` is the task this coding-agent session was started for. `response` is the agent's final message "
            "for its latest turn. How much does the work reported in `response` serve `intent`?"
        ),
        criteria=_LEVEL_CRITERIA,
    ),
    "serves_request": Score(
        instructions=(
            "`request` is the user's latest message to a coding agent. `response` is the agent's final message for "
            "that turn. How much does the work reported in `response` do what `request` asked?"
        ),
        # Level names deliberately avoid words like "unrelated" that a request itself may contain: Jev reads
        # literally, and "Unrelated: give me a recipe" was scored as unrelated to its own correct answer.
        criteria=[
            "Ignores `request`: the work is about something else",
            "Partial: touches `request` but mostly does something else",
            "Mostly does what `request` asked",
            "Does exactly what `request` asked",
        ],
    ),
}


@dataclass(frozen=True)
class Judgment:
    """Jev's answers for one turn: per question, one probability per level in drift.LEVELS."""

    probabilities: Mapping[str, tuple[float, ...]]
    input_tokens: int
    model: str
    latency_ms: int

    @property
    def cost_usd(self) -> float:
        return self.input_tokens * PRICE_PER_INPUT_TOKEN_USD


class Judge(Protocol):
    def __call__(self, state: dict[str, str], questions: Mapping[str, Score]) -> Judgment: ...


def clip(text: str, limit: int) -> str:
    """Keep the head and tail of long text; the middle of a paste is the least informative part."""
    assert limit > 20, "limit too small to clip meaningfully"
    if len(text) <= limit:
        return text
    half = (limit - 5) // 2
    return f"{text[:half]} [...] {text[-half:]}"


def human_state(intent: str, turn: str, previous_assistant_message: str) -> dict[str, str]:
    return {
        "intent": clip(intent, MAX_INTENT_CHARS),
        "previous_assistant_message": clip(previous_assistant_message or "(none)", MAX_CONTEXT_CHARS),
        "turn": clip(turn, MAX_TURN_CHARS),
    }


def agent_state(intent: str, request: str, response: str) -> dict[str, str]:
    return {
        "intent": clip(intent, MAX_INTENT_CHARS),
        "request": clip(request or intent, MAX_CONTEXT_CHARS),
        "response": clip(response, MAX_TURN_CHARS),
    }


def _distribution(answer_probabilities: Mapping[int, float]) -> tuple[float, ...]:
    dist = tuple(float(answer_probabilities.get(i, 0.0)) for i in range(len(LEVELS)))
    assert len(dist) == len(LEVELS)
    return dist


class JevJudge:
    """Synchronous Jev client with a hard timeout and no retries: a hook would rather skip a turn than wait."""

    def __init__(self, model: str, timeout_s: float) -> None:
        from typesafe_sdk import RetryPolicy, TypeSafeClient

        assert timeout_s > 0, "timeout must be positive"
        key = api_key()
        try:
            self._client = TypeSafeClient(
                api_key=key, model=model, timeout=timeout_s, retry=RetryPolicy(max_retries=0, timeout=timeout_s)
            )
        except Exception:  # noqa: BLE001 - SDK exceptions may echo credentials; hooks log exception messages
            raise RuntimeError("Jev client initialization failed") from None

    def __call__(self, state: dict[str, str], questions: Mapping[str, Score]) -> Judgment:
        started = time.monotonic()
        try:
            response = self._client.system_one(state=state, questions=dict(questions))
        except Exception:  # noqa: BLE001 - never persist SDK exception text that may contain credentials
            raise RuntimeError("Jev request failed") from None
        latency_ms = int((time.monotonic() - started) * 1000)
        probabilities = {qid: _distribution(response.scores[qid].probabilities) for qid in questions}
        return Judgment(probabilities, response.usage.input_tokens or 0, response.model, latency_ms)

    def close(self) -> None:
        try:
            self._client.close()
        except Exception:  # noqa: BLE001 - closing the SDK must not expose credential-bearing errors either
            raise RuntimeError("Jev client close failed") from None
