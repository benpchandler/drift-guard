"""The two hook handlers. Each takes a validated payload and returns the JSON Claude Code should receive (or None).

UserPromptSubmit judges the human's prompt against the session intent (human drift).
Stop judges the agent's final reply against the intent and the latest request (agent drift).
Neither handler catches Jev failures; cli.run_hook owns fail-open so there is exactly one place that decides it.
"""

from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from drift_guard import drift, escapes, feedback, synthesis, transcript
from drift_guard.config import Config
from drift_guard.drift import Action, DriftTrack
from drift_guard.judge import (
    AGENT_QUESTIONS,
    HUMAN_QUESTIONS,
    QUESTION_SET_VERSION,
    Judge,
    Judgment,
    agent_state,
    clip,
    human_state,
)
from drift_guard.store import Event, JudgmentRef, LabelRecord, SessionState, Store, now_iso

Output = dict[str, Any] | None
ESCAPE_HINT = (
    f"Start a prompt with '{escapes.ACK_COMMAND}' to clear this, '{escapes.NEW_INTENT_COMMAND} ...' to switch tasks, "
    f"or '{escapes.FEEDBACK_COMMAND} wrong' if this warning is mistaken."
)
EXCERPT_CHARS = 80


def _reminder(store: Store, state: SessionState) -> str:
    return "\n".join(f"  {line}" for line in synthesis.recap_lines(store, state))


@dataclass(frozen=True)
class PromptPayload:
    session_id: str
    prompt: str
    transcript_path: str
    cwd: str

    @classmethod
    def parse(cls, raw: dict[str, Any]) -> "PromptPayload":
        if raw.get("hook_event_name") != "UserPromptSubmit" or not isinstance(raw.get("prompt"), str):
            raise ValueError("not a UserPromptSubmit payload")
        return cls(str(raw["session_id"]), raw["prompt"], str(raw.get("transcript_path", "")), str(raw.get("cwd", "")))


@dataclass(frozen=True)
class StopPayload:
    session_id: str
    last_assistant_message: str
    stop_hook_active: bool
    transcript_path: str
    cwd: str

    @classmethod
    def parse(cls, raw: dict[str, Any]) -> "StopPayload":
        if raw.get("hook_event_name") != "Stop":
            raise ValueError("not a Stop payload")
        return cls(
            str(raw["session_id"]),
            str(raw.get("last_assistant_message") or ""),
            bool(raw.get("stop_hook_active")),
            str(raw.get("transcript_path", "")),
            str(raw.get("cwd", "")),
        )


def _event(cfg: Config, session_id: str, kind: str, action: str, cwd: str, **fields: Any) -> Event:
    return Event(session_id=session_id, event=kind, action=action, mode=cfg.mode.value, project=Path(cwd).name, **fields)


def _judged_event(cfg: Config, sid: str, kind: str, action: Action, cwd: str, *, j: Judgment, d: float, t: DriftTrack) -> Event:
    probabilities = {qid: list(p) for qid, p in j.probabilities.items()}
    return _event(
        cfg,
        sid,
        kind,
        action.value,
        cwd,
        turn_drift=round(d, 4),
        relevance=round(1 - d, 4),
        drift_score=round(t.score, 4),
        probabilities=probabilities,
        input_tokens=j.input_tokens,
        latency_ms=j.latency_ms,
        model=j.model,
    )


def _message(text: str) -> dict[str, Any]:
    return {"systemMessage": text}


# ---- UserPromptSubmit -------------------------------------------------------------------------------------------


def _start_session(p: PromptPayload, store: Store, cfg: Config) -> tuple[SessionState | None, Output]:
    """No state yet: adopt the transcript's first prompt (a resumed session) or make this prompt the intent."""
    path = Path(p.transcript_path)
    earlier = transcript.first_prompt(path) if p.transcript_path and path.exists() else None
    if earlier and earlier.strip() != p.prompt.strip():
        return SessionState(p.session_id, earlier, "transcript", now_iso()), None
    state = SessionState(p.session_id, p.prompt.strip(), "first_prompt", now_iso(), last_request=p.prompt.strip())
    store.save(state)
    store.append(_event(cfg, p.session_id, "human", "intent_set", p.cwd))
    return None, _message(
        f"Drift: intent recorded - \"{synthesis.headline(state.intent)}\". Change it with '{escapes.NEW_INTENT_COMMAND} ...'."
    )


def _apply_escape(p: PromptPayload, state: SessionState | None, esc: escapes.Escape, store: Store, cfg: Config) -> Output:
    if esc.kind is escapes.EscapeKind.USAGE:  # a mistyped escape: answer it, never send it to Claude or judge it
        return {"decision": "block", "reason": f"Drift: unrecognized '{escapes.PREFIX} {esc.text}'. {escapes.USAGE}"}
    if esc.kind is escapes.EscapeKind.NEW_INTENT:
        fresh = SessionState(p.session_id, esc.text, "restated", now_iso(), last_request=esc.text)
        if state is not None:  # keep counters and the judgments a later "drift: feedback" may label
            fresh = replace(
                fresh,
                human=drift.reset(state.human),
                agent=drift.reset(state.agent),
                last_reply=state.last_reply,
                last_human=state.last_human,
                last_agent=state.last_agent,
            )
        store.save(fresh)
        store.append(_event(cfg, p.session_id, "human", "new_intent", p.cwd))
        return _message(f'Drift: new intent - "{synthesis.headline(esc.text)}". Drift reset.')
    if esc.kind is escapes.EscapeKind.FEEDBACK:
        return _apply_feedback(p, state, esc.text, store, cfg)
    assert esc.kind is escapes.EscapeKind.ACK_DRIFT
    if state is None:
        return None
    store.save(replace(state, human=drift.reset(state.human), agent=drift.reset(state.agent), last_request=p.prompt))
    store.append(_event(cfg, p.session_id, "human", "ack", p.cwd))
    return _message("Drift: acknowledged; score reset. Intent unchanged.")


def feedback_target(state: SessionState) -> JudgmentRef | None:
    """The judgment a "drift: feedback" prompt labels: the latest exchange's warning if there was one, else its prompt.

    The latest exchange is the last judged prompt plus the agent reply judged after it. Preferring a warning matters
    because Stop judges the reply after the warning Ben just saw, so the newest judgment is usually a quiet one.
    """
    human, agent = state.last_human, state.last_agent
    if human is not None and agent is not None and agent.seq < human.seq:
        agent = None  # that reply belongs to an earlier exchange
    candidates = [ref for ref in (human, agent) if ref is not None]
    flagged = [ref for ref in candidates if ref.action != Action.NONE.value]
    if flagged:
        return max(flagged, key=lambda ref: ref.seq)
    return human or agent


def _apply_feedback(p: PromptPayload, state: SessionState | None, text: str, store: Store, cfg: Config) -> Output:
    """Record Ben's label and swallow the prompt: a block shows him the confirmation and never reaches Claude."""
    label = feedback.parse(text)
    target = feedback_target(state) if state is not None else None
    result = feedback.outcome(label.verdict, Action(target.action) if target else None)
    store.append_label(
        LabelRecord(
            session_id=p.session_id,
            verdict=label.verdict.value,
            outcome=result.value,
            note=label.note,
            judgment_id=target.id if target else "",
            judgment_event=target.event if target else "",
            judgment_action=target.action if target else "",
            excerpt=target.excerpt if target else "",
            questions_version=target.questions_version if target else QUESTION_SET_VERSION,
        )
    )
    store.append(_event(cfg, p.session_id, "human", "feedback", p.cwd))
    if state is not None and target is not None and result is feedback.Outcome.FALSE_POSITIVE:
        if target.event == "human":
            store.save(replace(state, human=drift.reset(state.human)))
        else:
            store.save(replace(state, agent=drift.reset(state.agent)))
    return {"decision": "block", "reason": feedback.confirmation(result, target.excerpt if target else "")}


def _judgment_ref(state: SessionState, event: Event, excerpt: str) -> JudgmentRef:
    seq = state.human.turns + state.agent.turns
    return JudgmentRef(event.id, event.event, event.action, seq, clip(excerpt, EXCERPT_CHARS), event.questions_version)


def _human_output(action: Action, state: SessionState, score: float, cfg: Config, store: Store) -> Output:
    if action is Action.BLOCK:
        return {
            "decision": "block",
            "reason": f"Drift blocked this prompt: drift {score:.2f} >= {cfg.thresholds.block:.2f} from what "
            f"this session is for:\n{_reminder(store, state)}\nResend it starting with '{escapes.ACK_COMMAND}' to go "
            f"ahead anyway, or '{escapes.NEW_INTENT_COMMAND} ...' to switch tasks.",
        }
    if action is Action.WARN:
        how = "sustained drift" if score >= cfg.thresholds.block else "drifting"
        return _message(
            f"Drift: {how} (drift {score:.2f}) from what this session is for:\n{_reminder(store, state)}\n{ESCAPE_HINT}"
        )
    return None


def _previous_reply(state: SessionState, p: PromptPayload) -> str:
    if state.last_reply:
        return state.last_reply
    path = Path(p.transcript_path)
    return transcript.last_reply(path) if p.transcript_path and path.exists() else ""


def handle_user_prompt(p: PromptPayload, cfg: Config, store: Store, judge: Judge) -> Output:
    state = store.load(p.session_id)
    esc = escapes.parse(p.prompt)
    if esc is not None:
        return _apply_escape(p, state, esc, store, cfg)
    if escapes.is_slash_command(p.prompt) or not transcript.is_typed_by_human(p.prompt):
        store.append(_event(cfg, p.session_id, "human", "skip", p.cwd, error="not a typed task prompt"))
        return None
    if state is None:
        state, output = _start_session(p, store, cfg)
        if state is None:
            return output
    judgment = judge(human_state(state.intent, p.prompt, _previous_reply(state, p)), HUMAN_QUESTIONS)
    turn = drift.turn_drift(judgment.probabilities["serves_intent"])
    track, action = drift.step(state.human, turn, cfg.thresholds, cfg.blocking)
    request = state.last_request if action is Action.BLOCK else p.prompt  # a blocked prompt is erased from context
    event = _judged_event(cfg, p.session_id, "human", action, p.cwd, j=judgment, d=turn, t=track)
    updated = replace(state, human=track, last_request=request)
    store.save(replace(updated, last_human=_judgment_ref(updated, event, p.prompt)))
    store.append(event)
    return _human_output(action, state, track.score, cfg, store)


# ---- Stop ---------------------------------------------------------------------------------------------------------


def _agent_output(action: Action, state: SessionState, score: float, store: Store) -> Output:
    if action is Action.NONE:
        return None
    intent = clip(state.intent, 120)  # the agent's context keeps the head-and-tail clip
    note = f"Drift: the agent is drifting (agent drift {score:.2f}) from what this session is for:\n{_reminder(store, state)}"
    if action is Action.BLOCK:
        return {
            "systemMessage": note,
            "hookSpecificOutput": {
                "hookEventName": "Stop",
                "additionalContext": f'Your recent replies have drifted from the session intent: "{intent}" and '
                f"the user's latest request. Refocus on that intent, or ask the user whether the new direction is wanted.",
            },
        }
    return _message(note)


def handle_stop(p: StopPayload, cfg: Config, store: Store, judge: Judge) -> Output:
    state = store.load(p.session_id)
    if state is None:
        return None
    state = replace(state, last_reply=p.last_assistant_message or state.last_reply)
    if not cfg.agent or p.stop_hook_active or not p.last_assistant_message.strip():
        store.save(state)
        return None
    judgment = judge(agent_state(state.intent, state.last_request, p.last_assistant_message), AGENT_QUESTIONS)
    # The agent drifts only if its reply serves neither the session intent nor what it was just asked.
    turn = min(drift.turn_drift(judgment.probabilities[qid]) for qid in AGENT_QUESTIONS)
    track, action = drift.step(state.agent, turn, cfg.thresholds, cfg.blocking)
    event = _judged_event(cfg, p.session_id, "agent", action, p.cwd, j=judgment, d=turn, t=track)
    updated = replace(state, agent=track)
    store.save(replace(updated, last_agent=_judgment_ref(updated, event, p.last_assistant_message)))
    store.append(event)
    return _agent_output(action, state, track.score, store)
