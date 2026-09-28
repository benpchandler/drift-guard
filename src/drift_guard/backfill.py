"""Pre-guard baseline: replay the live judge over a sample of past transcripts, cost-capped.

Each sampled session is judged turn by turn exactly as the hooks would have judged it, and the replay runs the same
drift policy, so `report` can compare adherence before the guard (source=backfill) with after (source=live).
"""

import asyncio
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from typesafe_sdk import Score

from drift_guard import drift, escapes, transcript
from drift_guard.config import Config
from drift_guard.judge import (
    AGENT_QUESTIONS,
    HUMAN_QUESTIONS,
    PRICE_PER_INPUT_TOKEN_USD,
    QUESTION_SET_VERSION,
    agent_state,
    human_state,
)
from drift_guard.store import Event, Store, now_iso

PROJECTS = Path.home() / ".claude/projects"
MIN_EXCHANGES = 3
MAX_CANDIDATES = 2000
CONCURRENCY = 8
CHARS_PER_TOKEN = 2.5  # measured ~2.7 on the first baseline run; 2.5 keeps the estimate an over-estimate


@dataclass(frozen=True)
class Job:
    session_id: str
    project: str
    index: int  # exchange index within the session; replay order
    kind: str  # human | agent
    state: dict[str, str]
    questions: dict[str, Score]
    ts: str  # when the turn happened, so weekly adherence reflects the session's date, not the backfill's

    @property
    def estimated_tokens(self) -> int:
        size = len(json.dumps(self.state)) + sum(len(str(q)) for q in self.questions.values())
        return int(size / CHARS_PER_TOKEN)


# The guard's own sessions are excluded from the baseline; older transcripts keep the repo's former name.
OWN_PROJECT_MARKERS = ("drift-guard", "adhd-guard")


def session_jobs(path: Path, max_turns: int) -> list[Job]:
    """Every judgment one past session needs: human drift for prompts after the first, agent drift for replies."""
    turns = [x for x in transcript.exchanges(path) if not escapes.is_slash_command(x.prompt)][: max_turns + 1]
    if len(turns) < MIN_EXCHANGES:
        return []
    sid, project, intent = path.stem, path.parent.name, turns[0].prompt
    jobs: list[Job] = []
    for i, turn in enumerate(turns):
        if i > 0:
            state = human_state(intent, turn.prompt, turns[i - 1].reply)
            jobs.append(Job(sid, project, i, "human", state, dict(HUMAN_QUESTIONS), turn.ts))
        if turn.reply:
            state = agent_state(intent, turn.prompt, turn.reply)
            jobs.append(Job(sid, project, i, "agent", state, dict(AGENT_QUESTIONS), turn.ts))
    return jobs


def _already_backfilled(store: Store) -> set[str]:
    if not store.log_path.exists():
        return set()
    done: set[str] = set()
    with store.log_path.open() as handle:
        for line in handle:
            entry = json.loads(line)
            if entry.get("source") == "backfill" and entry.get("questions_version", 1) == QUESTION_SET_VERSION:
                done.add(entry["session_id"])
    return done


def sample(store: Store, sessions: int, max_turns: int, budget_usd: float) -> list[Job]:
    """Most recent top-level transcripts not yet backfilled, until the session count or estimated budget is hit."""
    candidates = sorted(PROJECTS.glob("*/*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)[:MAX_CANDIDATES]
    skip = _already_backfilled(store)
    jobs: list[Job] = []
    taken = 0
    for path in candidates:
        if taken >= sessions or path.stem in skip or any(m in path.parent.name for m in OWN_PROJECT_MARKERS):
            continue
        found = session_jobs(path, max_turns)
        cost = sum(j.estimated_tokens for j in jobs + found) * PRICE_PER_INPUT_TOKEN_USD
        if not found or cost > budget_usd:
            continue
        jobs.extend(found)
        taken += 1
    return jobs


async def _ask_all(jobs: list[Job], model: str) -> list[tuple[Job, dict[str, tuple[float, ...]], int]]:
    from typesafe_sdk import AsyncTypeSafeClient

    gate = asyncio.Semaphore(CONCURRENCY)
    async with AsyncTypeSafeClient(model=model) as client:

        async def one(job: Job) -> tuple[Job, dict[str, tuple[float, ...]], int]:
            async with gate:
                response = await client.system_one(state=job.state, questions=job.questions)
            probs = {
                q: tuple(response.scores[q].probabilities.get(i, 0.0) for i in range(len(drift.LEVELS))) for q in job.questions
            }
            return job, probs, response.usage.input_tokens or 0

        results = await asyncio.gather(*(one(j) for j in jobs), return_exceptions=True)
    ok = [r for r in results if not isinstance(r, BaseException)]
    failures = [r for r in results if isinstance(r, BaseException)]
    if failures:
        print(f"{len(failures)} requests failed; their turns are left out of the baseline. First: {failures[0]!r}"[:400])
    return ok


def _iso(ts: str) -> str:
    """Normalize a transcript timestamp ("...Z", milliseconds) to the log's second-precision UTC form."""
    if not ts:
        return now_iso()
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(UTC).isoformat(timespec="seconds")


def replay(results: list[tuple[Job, dict[str, tuple[float, ...]], int]], cfg: Config) -> list[Event]:
    """Run the live drift policy over each session's turns in order, producing the events the guard would have logged."""
    tracks: dict[tuple[str, str], drift.DriftTrack] = {}
    events: list[Event] = []
    for job, probs, tokens in sorted(results, key=lambda r: (r[0].session_id, r[0].index, r[0].kind != "human")):
        turn = min(drift.turn_drift(p) for p in probs.values())
        key = (job.session_id, job.kind)
        track, action = drift.step(tracks.get(key, drift.DriftTrack()), turn, cfg.thresholds, blocking=False)
        tracks[key] = track
        events.append(
            Event(
                session_id=job.session_id,
                event=job.kind,
                action=action.value,
                source="backfill",
                ts=_iso(job.ts),
                mode="baseline",
                project=job.project,
                turn_drift=round(turn, 4),
                relevance=round(1 - turn, 4),
                drift_score=round(track.score, 4),
                probabilities={q: list(p) for q, p in probs.items()},
                input_tokens=tokens,
            )
        )
    return events


def run(cfg: Config, sessions: int, max_turns: int, budget_usd: float) -> int:
    assert budget_usd > 0 and sessions > 0 and max_turns > 0
    store = Store(cfg.state_dir)
    jobs = sample(store, sessions, max_turns, budget_usd)
    estimate = sum(j.estimated_tokens for j in jobs) * PRICE_PER_INPUT_TOKEN_USD
    print(
        f"{len({j.session_id for j in jobs})} sessions, {len(jobs)} judgments, estimated ${estimate:.4f} (cap ${budget_usd:.2f})"
    )
    results = asyncio.run(_ask_all(jobs, cfg.model))
    events = replay(results, cfg)
    for event in events:
        store.append(event)
    tokens = sum(r[2] for r in results)
    print(f"logged {len(events)} baseline events; {tokens} input tokens, actual ${tokens * PRICE_PER_INPUT_TOKEN_USD:.4f}")
    return 0
