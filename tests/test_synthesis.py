import json
from collections.abc import Callable
from dataclasses import replace
from typing import Any

from conftest import OFF, FakeJudge

from drift_guard import cli, synthesis
from drift_guard.config import Config
from drift_guard.hooks import PromptPayload, handle_user_prompt
from drift_guard.store import Recap, SessionState, Store, now_iso

Make = Callable[..., dict[str, Any]]
SID = "s1"
INTENT = '<pasted_content id="1">\nMake sbt\'s follow-selection real (TASK-359). Then a long tail.\n</pasted_content id="1">'
REPLY = "- Make sbt's follow-selection real (TASK-359).\n\n* Always on inside tmux.\nShip via PR.\nA fourth line.\n"


def seed(store: Store, intent: str = INTENT) -> SessionState:
    state = SessionState(SID, intent, "first_prompt", now_iso())
    store.save(state)
    return state


def test_recap_keeps_three_clean_lines() -> None:
    lines = synthesis.recap(INTENT, lambda _prompt: REPLY)
    assert lines == ["Make sbt's follow-selection real (TASK-359).", "Always on inside tmux.", "Ship via PR."]


def test_recap_fits_a_long_line_and_rejects_an_empty_reply() -> None:
    long = synthesis.recap("x", lambda _prompt: "word " * 60)
    assert long is not None and len(long[0]) <= synthesis.RECAP_LINE_CHARS and long[0].endswith("...")
    assert synthesis.recap("x", lambda _prompt: "  \n") is None
    assert synthesis.recap("x", lambda _prompt: None) is None


def test_headline_strips_the_paste_wrapper_and_keeps_the_first_sentence() -> None:
    assert synthesis.headline(INTENT) == "Make sbt's follow-selection real (TASK-359)."
    assert synthesis.headline("fix the flaky login test") == "fix the flaky login test"


def test_warnings_show_the_recap_only_for_the_intent_it_was_written_for(store: Store) -> None:
    state = seed(store)
    assert synthesis.recap_lines(store, state) == ["Make sbt's follow-selection real (TASK-359)."]  # not written yet
    store.save_recap(SID, Recap(synthesis.intent_key(INTENT), "ready", ("Goal line.", "Scope line.")))
    assert synthesis.recap_lines(store, state) == ["Goal line.", "Scope line."]
    changed = replace(state, intent="Write the quarterly report.")
    assert synthesis.recap_lines(store, changed) == ["Write the quarterly report."]


def test_write_recap_saves_ready_or_failed(store: Store) -> None:
    seed(store)
    assert synthesis.write_recap(store, SID, lambda _prompt: REPLY)
    saved = store.load_recap(SID)
    assert saved is not None and saved.status == "ready" and saved.lines[0].startswith("Make sbt's")
    assert not synthesis.write_recap(store, SID, lambda _prompt: None)
    failed = store.load_recap(SID)
    assert failed is not None and failed.status == "failed" and failed.lines == ()


def test_write_recap_drops_a_recap_whose_intent_changed_meanwhile(store: Store) -> None:
    state = seed(store)

    def slow_model(_prompt: str) -> str:
        store.save(replace(state, intent="new intent while writing"))
        return REPLY

    assert not synthesis.write_recap(store, SID, slow_model)
    assert store.load_recap(SID) is None


def test_request_recap_starts_one_writer_per_intent(store: Store) -> None:
    state = seed(store)
    started: list[str] = []
    synthesis.request_recap(store, SID, started.append)
    synthesis.request_recap(store, SID, started.append)  # pending: not again
    assert started == [SID]
    store.save(replace(state, intent="a different task"))
    synthesis.request_recap(store, SID, started.append)
    assert started == [SID, SID]


def test_a_stale_pending_recap_is_requested_again(store: Store) -> None:
    seed(store)
    store.save_recap(SID, Recap(synthesis.intent_key(INTENT), "pending", at="2000-01-01T00:00:00+00:00"))
    started: list[str] = []
    synthesis.request_recap(store, SID, started.append)
    assert started == [SID]


def test_warning_repeats_the_recap_lines(payload: Make, cfg: Config, store: Store) -> None:
    judge = FakeJudge(OFF)
    handle_user_prompt(PromptPayload.parse(payload(INTENT)), cfg, store, judge)
    sid = payload("x")["session_id"]
    intent = store.load(sid)
    assert intent is not None
    store.save_recap(sid, Recap(synthesis.intent_key(intent.intent), "ready", ("Goal line.", "Scope line.")))
    out = handle_user_prompt(PromptPayload.parse(payload("banana bread recipe?")), cfg, store, judge)
    assert out is not None and "  Goal line.\n  Scope line.\n" in out["systemMessage"]


def test_hook_requests_a_recap_after_prompts_only(payload: Make, cfg: Config) -> None:
    started: list[str] = []

    def ok(kind: str, raw: dict[str, Any], cfg: Config, store: Store) -> None:
        store.save(SessionState(str(raw["session_id"]), "intent", "first_prompt", now_iso()))

    cli.run_hook("user-prompt", json.dumps(payload("x")), cfg, handle=ok, spawn_recap=started.append)
    cli.run_hook("stop", json.dumps(payload("x")), cfg, handle=ok, spawn_recap=started.append)
    assert started == [payload("x")["session_id"]]
