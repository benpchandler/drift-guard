"""No network or owner state: original goal plus one supporting task."""

import json
from dataclasses import asdict
from typing import Any

import pytest

from drift_guard.pi_state import handle
from drift_guard.store import Store

SID = "pi-session-test"


def request(store: Store, operation: str, **fields: Any) -> Any:
    return handle({"session_id": SID, "operation": operation, **fields}, store)


def start(store: Store) -> Any:
    return request(store, "observe", prompt="Regenerate and publish the corrected workbook")


def assign(store: Store) -> Any:
    return request(
        store,
        "set",
        text="Fix depreciation rounding",
        rationale="Required for workbook validation",
        owner="worker-123",
        completion_condition="Focused validation passes",
    )


def test_task_lifecycle_preserves_original_and_archives_evidence(store: Store) -> None:
    original = start(store)
    assigned = assign(store)
    task_id = assigned.supporting_task.id
    assert assigned.intent == original.intent
    assert assigned.supporting_task.owner == "worker-123"
    request(store, "observe", prompt="status??")
    blocked = request(store, "block", task_id=task_id, evidence="Renderer fixture failed")
    assert blocked.supporting_task.status == "blocked"
    assert blocked.intent == original.intent
    resumed = request(store, "resume", task_id=task_id, evidence="Fixture corrected")
    assert resumed.supporting_task.status == "active"
    done = request(store, "complete", task_id=task_id, evidence="Rounding tests pass; renderer validated")
    assert done.intent == original.intent
    assert done.last_request == "status??"
    assert done.supporting_task is None
    assert done.supporting_task_history[0].status == "done"
    assert "validated" in done.supporting_task_history[0].evidence
    assert store.load(SID) == done
    assign(store)
    assert len(store.load(SID).supporting_task_history) == 1


def test_legacy_state_loads_without_new_fields(store: Store) -> None:
    state = start(store)
    record = asdict(state)
    del record["supporting_task"]
    del record["supporting_task_history"]
    store._session_path(SID).write_text(json.dumps(record))
    assert store.load(SID) == state


def test_restore_uses_active_branch_not_abandoned_mirror(store: Store) -> None:
    before_task = start(store)
    assigned = assign(store)
    # Tree navigation to before the task must remove the abandoned task.
    restored = request(store, "restore", snapshot=asdict(before_task))
    assert restored == before_task
    # A fork gets its own session id but inherits the chosen branch snapshot.
    fork = handle({"session_id": "fork-test", "operation": "restore", "snapshot": asdict(assigned)}, store)
    assert fork.session_id == "fork-test"
    assert fork.supporting_task == assigned.supporting_task
    assert request(store, "restore", snapshot=None, original=None) is None
    assert store.load(SID) is None


def test_explicit_new_intent_archives_task_and_never_uses_status_prompt_as_intent(store: Store) -> None:
    start(store)
    assigned = assign(store)
    status = request(store, "observe", prompt="What is taking so long?")
    assert status.intent == assigned.intent
    changed = request(store, "intent", original="Prepare an Appliances review workbook")
    assert changed.intent_source == "restated"
    assert changed.supporting_task is None
    assert changed.supporting_task_history[0].status == "canceled"


def test_task_validation_does_not_change_state(store: Store) -> None:
    with pytest.raises(ValueError, match="original intent"):
        assign(store)
    start(store)
    assigned = assign(store)
    with pytest.raises(ValueError, match="replacing"):
        assign(store)
    with pytest.raises(ValueError, match="task_id"):
        request(store, "complete", task_id="stale", evidence="pass")
    with pytest.raises(ValueError, match="evidence"):
        request(store, "complete", task_id=assigned.supporting_task.id)
    assert store.load(SID) == assigned
    with pytest.raises(ValueError, match="unsafe session"):
        handle({"session_id": "../../escape", "operation": "status"}, store)
