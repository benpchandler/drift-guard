"""Pi's local intent/task bridge. No model calls, credentials or Claude/Codex hooks.

One JSON request on stdin, one state record on stdout. Pi branch snapshots are
canonical on restore; the Store mirror makes `drift status` usable outside Pi.
"""

import fcntl
import json
import os
import sys
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any

from drift_guard.store import SessionState, Store, SupportingTask, new_id, now_iso, session_from_record


def _text(request: dict[str, Any], key: str) -> str:
    value = request.get(key)
    if not isinstance(value, str) or not value.strip() or len(value) > 100_000:
        raise ValueError(f"{key} must be nonempty text (at most 100000 characters)")
    return value.strip()


def apply(request: dict[str, Any], state: SessionState | None) -> SessionState | None:
    sid = _text(request, "session_id")
    operation = request.get("operation")
    if operation == "restore":
        return _restore(request, sid)
    if operation == "observe":
        prompt = _text(request, "prompt")
        if state is None:
            return SessionState(sid, prompt, "first_prompt", now_iso(), last_request=prompt)
        return replace(state, last_request=prompt)
    if operation == "status":
        return state
    if operation == "intent":
        original = _text(request, "original")
        history = state.supporting_task_history if state else ()
        if state and state.supporting_task:
            archived = replace(
                state.supporting_task, status="canceled", evidence="User changed original intent", updated_at=now_iso()
            )
            history = (*history, archived)
        return SessionState(sid, original, "restated", now_iso(), last_request=original, supporting_task_history=history)
    return _apply_task(request, state)


def _restore(request: dict[str, Any], sid: str) -> SessionState | None:
    snapshot = request.get("snapshot")
    if snapshot is not None:
        return session_from_record({**snapshot, "session_id": sid})
    # No branch state means no state: never inherit a mirror from an abandoned branch.
    if not request.get("original"):
        return None
    return SessionState(sid, _text(request, "original"), "transcript", now_iso())


def _apply_task(request: dict[str, Any], state: SessionState | None) -> SessionState:
    operation = request.get("operation")
    if state is None:
        raise ValueError("record the user's original intent before assigning a supporting task")
    if operation == "set":
        if state.supporting_task is not None:
            raise ValueError("complete or cancel the current supporting task before replacing it")
        assigned = SupportingTask(
            new_id(),
            _text(request, "text"),
            _text(request, "rationale"),
            _text(request, "owner"),
            _text(request, "completion_condition"),
        )
        return replace(state, supporting_task=assigned)
    if operation not in {"complete", "block", "resume", "cancel"}:
        raise ValueError("unknown Pi state operation")
    task = state.supporting_task
    if task is None or request.get("task_id") != task.id:
        raise ValueError("task_id must match the active supporting task")
    evidence = _text(request, "evidence")
    status = {"complete": "done", "block": "blocked", "resume": "active", "cancel": "canceled"}[operation]
    updated = replace(task, status=status, evidence=evidence, updated_at=now_iso())
    if operation in {"complete", "cancel"}:
        return replace(state, supporting_task=None, supporting_task_history=(*state.supporting_task_history, updated))
    return replace(state, supporting_task=updated)


def handle(request: dict[str, Any], store: Store) -> SessionState | None:
    sid = _text(request, "session_id")
    # Validate the session id before creating a lock or writing a record.
    path = store._session_path(sid)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix(".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state = apply(request, None if request.get("operation") == "restore" else store.load(sid))
        if state is not None:
            store.save(state)
        elif request.get("operation") == "restore":
            path.unlink(missing_ok=True)
        return state


def main() -> int:
    root = Path(os.environ.get("DRIFT_PI_STATE_DIR") or Path.home() / ".local/state/drift-pi").expanduser()
    try:
        request = json.loads(sys.stdin.read())
        if not isinstance(request, dict):
            raise ValueError("expected one JSON object")
        state = handle(request, Store(root))
    except (ValueError, TypeError, AssertionError, KeyError, OSError):
        # Do not echo arbitrary state/request text into Pi's error logs.
        print("Drift Pi state request failed", file=sys.stderr)
        return 1
    print(json.dumps(None if state is None else asdict(state)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
