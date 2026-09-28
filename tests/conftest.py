import json
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import pytest
from typesafe_sdk import Score

from drift_guard.config import Config, Mode
from drift_guard.judge import Judgment
from drift_guard.store import Store

FIXTURES = Path(__file__).parent / "fixtures"

# Level distributions (unrelated, tangential, supporting, direct).
ON = (0.0, 0.0, 0.1, 0.9)
OFF = (1.0, 0.0, 0.0, 0.0)
TANGENT = (0.0, 1.0, 0.0, 0.0)


class FakeJudge:
    """Returns scripted distributions in order and records every state it was asked about."""

    def __init__(self, *answers: Mapping[str, tuple[float, ...]] | tuple[float, ...]) -> None:
        self.answers = list(answers)
        self.states: list[dict[str, str]] = []

    def __call__(self, state: dict[str, str], questions: Mapping[str, Score]) -> Judgment:
        self.states.append(state)
        answer = self.answers.pop(0)
        probs = answer if isinstance(answer, Mapping) else {q: answer for q in questions}
        return Judgment(dict(probs), input_tokens=100, model="fake", latency_ms=5)


@pytest.fixture(autouse=True)
def isolated_state(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """No test may touch the real ~/.local/state/drift, even through a default Config()."""
    monkeypatch.setenv("DRIFT_STATE_DIR", str(tmp_path / "default-state"))
    monkeypatch.setattr("drift_guard.config.DEFAULT_STATE_DIR", tmp_path / "default-state")


@pytest.fixture
def store(tmp_path: Path) -> Store:
    return Store(tmp_path / "state")


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    return Config(state_dir=tmp_path / "state")


@pytest.fixture
def block_cfg(tmp_path: Path) -> Config:
    return Config(mode=Mode.BLOCK, state_dir=tmp_path / "state")


@pytest.fixture
def payload() -> Callable[..., dict[str, Any]]:
    """A real captured UserPromptSubmit payload with the prompt swapped in."""
    base = json.loads((FIXTURES / "user_prompt_submit.json").read_text())

    def make(prompt: str, **overrides: Any) -> dict[str, Any]:
        return {**base, "prompt": prompt, "transcript_path": "/nonexistent", **overrides}

    return make


@pytest.fixture
def stop_payload() -> Callable[..., dict[str, Any]]:
    base = json.loads((FIXTURES / "stop.json").read_text())

    def make(message: str, **overrides: Any) -> dict[str, Any]:
        return {**base, "last_assistant_message": message, **overrides}

    return make


def events(store: Store) -> list[dict[str, Any]]:
    return [json.loads(line) for line in store.log_path.read_text().splitlines()]
