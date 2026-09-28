"""Persistence: per-session state (one JSON file per session_id) and the append-only judgment log.

Layout under the state dir (default ~/.local/state/drift):
  sessions/<session_id>.json   current intent and both drift tracks
  recaps/<session_id>.json     the synthesized recap of the current intent (synthesis.py); kept apart so the
                               detached writer never races a hook saving the session file
  judgments.jsonl              one Event per line, never rewritten; the pre/post measurement reads this
  labels.jsonl                 one LabelRecord per "drift: feedback" prompt, never rewritten; linked by judgment id
"""

import json
import os
import re
import tempfile
import uuid
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from drift_guard.drift import DriftTrack
from drift_guard.judge import QUESTION_SET_VERSION

GUARD_VERSION = "0.1.0"
LOG_NAME = "judgments.jsonl"
LABELS_NAME = "labels.jsonl"
_SESSION_ID = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def new_id() -> str:
    return uuid.uuid4().hex[:16]


@dataclass(frozen=True)
class JudgmentRef:
    """Enough of a logged judgment to attach a label to it and quote it back."""

    id: str
    event: str  # human | agent
    action: str  # none | warn | block
    seq: int  # judgments so far in the session; orders human vs agent refs without clock ties
    excerpt: str  # the judged prompt or reply, clipped
    questions_version: int = QUESTION_SET_VERSION


@dataclass(frozen=True)
class SessionState:
    session_id: str
    intent: str
    intent_source: str  # first_prompt | restated | transcript
    intent_set_at: str
    human: DriftTrack = field(default_factory=DriftTrack)
    agent: DriftTrack = field(default_factory=DriftTrack)
    last_request: str = ""  # the latest human prompt, the reference for agent drift
    last_reply: str = ""  # the agent's latest final message, context for judging a short human reply
    last_human: JudgmentRef | None = None  # the most recent judgment of a human prompt
    last_agent: JudgmentRef | None = None  # the most recent judgment of an agent reply

    def __post_init__(self) -> None:
        assert self.intent.strip(), "a session state always carries an intent"
        assert _SESSION_ID.match(self.session_id), f"unsafe session id: {self.session_id!r}"


@dataclass(frozen=True)
class Event:
    """One line of the judgment log. `source` separates live (guard on) from backfill (pre-guard baseline)."""

    session_id: str
    event: str  # human | agent
    action: str  # none | warn | block | intent_set | new_intent | ack | feedback | skip | error
    source: str = "live"
    id: str = field(default_factory=new_id)
    ts: str = field(default_factory=now_iso)
    mode: str = ""
    project: str = ""
    turn_drift: float | None = None
    relevance: float | None = None
    drift_score: float | None = None
    probabilities: dict[str, list[float]] | None = None
    input_tokens: int = 0
    latency_ms: int | None = None
    model: str = ""
    error: str = ""
    guard_version: str = GUARD_VERSION
    questions_version: int = QUESTION_SET_VERSION


@dataclass(frozen=True)
class LabelRecord:
    """Ben's accuracy label on one judgment. judgment_id is empty when the session had nothing to label."""

    session_id: str
    verdict: str  # feedback.Verdict
    outcome: str  # feedback.Outcome
    note: str
    judgment_id: str = ""
    judgment_event: str = ""
    judgment_action: str = ""
    excerpt: str = ""
    questions_version: int = QUESTION_SET_VERSION
    id: str = field(default_factory=new_id)
    ts: str = field(default_factory=now_iso)
    guard_version: str = GUARD_VERSION


@dataclass(frozen=True)
class Recap:
    """The synthesized recap of one intent, or a note that one is being written or could not be."""

    intent_key: str  # synthesis.intent_key of the intent it recaps
    status: str  # pending | ready | failed
    lines: tuple[str, ...] = ()
    at: str = field(default_factory=now_iso)

    def __post_init__(self) -> None:
        assert self.status in {"pending", "ready", "failed"}, f"unknown recap status {self.status!r}"
        assert (self.status == "ready") == bool(self.lines), "only a ready recap carries lines"

    def age_s(self) -> float:
        return (datetime.now(UTC) - datetime.fromisoformat(self.at)).total_seconds()


def _ref(raw: dict[str, Any] | None) -> JudgmentRef | None:
    return None if raw is None else JudgmentRef(**raw)


class Store:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.sessions = root / "sessions"
        self.recaps = root / "recaps"
        self.log_path = root / LOG_NAME
        self.labels_path = root / LABELS_NAME

    def _session_path(self, session_id: str) -> Path:
        if not _SESSION_ID.match(session_id):
            raise ValueError(f"unsafe session id: {session_id!r}")
        return self.sessions / f"{session_id}.json"

    def load(self, session_id: str) -> SessionState | None:
        path = self._session_path(session_id)
        if not path.exists():
            return None
        raw: dict[str, Any] = json.loads(path.read_text())
        raw["human"] = DriftTrack(**raw["human"])
        raw["agent"] = DriftTrack(**raw["agent"])
        raw["last_human"] = _ref(raw.get("last_human"))
        raw["last_agent"] = _ref(raw.get("last_agent"))
        return SessionState(**raw)

    def save(self, state: SessionState) -> None:
        self._write_atomic(self._session_path(state.session_id), asdict(state))

    def load_recap(self, session_id: str) -> Recap | None:
        path = self.recaps / self._session_path(session_id).name
        if not path.exists():
            return None
        raw: dict[str, Any] = json.loads(path.read_text())
        return Recap(raw["intent_key"], raw["status"], tuple(raw["lines"]), raw["at"])

    def save_recap(self, session_id: str, recap: Recap) -> None:
        self._write_atomic(self.recaps / self._session_path(session_id).name, asdict(recap))

    def _write_atomic(self, path: Path, record: dict[str, Any]) -> None:
        """Atomic replace, so a process killed mid-write never leaves a torn file."""
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
        with os.fdopen(fd, "w") as handle:
            json.dump(record, handle)
        os.replace(tmp, path)

    def append(self, event: Event) -> None:
        self._append_line(self.log_path, asdict(event))

    def append_label(self, label: LabelRecord) -> None:
        self._append_line(self.labels_path, asdict(label))

    def _append_line(self, path: Path, record: dict[str, Any]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        line = json.dumps(record, separators=(",", ":"))
        assert "\n" not in line, "one record per line"
        with path.open("a") as handle:
            handle.write(line + "\n")
