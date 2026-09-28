"""Codex native hook adapter. Shared judgments, no Claude transcript or recap process."""

from typing import Any

from drift_guard import hooks
from drift_guard.config import Config
from drift_guard.judge import Judge
from drift_guard.store import Store

CONTINUATION_PREFIX = "<drift_guard_continuation>"


def handle(kind: str, raw: dict[str, Any], cfg: Config, store: Store, judge: Judge) -> hooks.Output:
    if not isinstance(raw.get("session_id"), str) or not raw["session_id"]:
        raise ValueError("invalid Codex session_id")
    if kind == "stop":
        if raw.get("last_assistant_message") is not None and not isinstance(raw["last_assistant_message"], str):
            raise ValueError("invalid Codex last_assistant_message")
        if "stop_hook_active" in raw and not isinstance(raw["stop_hook_active"], bool):
            raise ValueError("invalid Codex stop_hook_active")
    # Codex explicitly documents transcript format as unstable. Never hand it to the Claude parser.
    direct = {**raw, "transcript_path": ""}
    if kind == "user-prompt":
        payload = hooks.PromptPayload.parse(direct)
        if payload.prompt.lstrip().startswith((CONTINUATION_PREFIX, "[Jev Stop review]")):
            return None  # hook-generated continuation, not a human request or new intent
        return hooks.handle_user_prompt(payload, cfg, store, judge)
    if kind != "stop":
        raise ValueError("unknown Codex hook kind")
    output = hooks.handle_stop(hooks.StopPayload.parse(direct), cfg, store, judge)
    if output is not None and "hookSpecificOutput" in output:
        # Shared Claude block output supplies context. Codex requires a native continuation decision.
        reason = output["hookSpecificOutput"]["additionalContext"]
        return {
            "systemMessage": output["systemMessage"],
            "decision": "block",
            "reason": f"{CONTINUATION_PREFIX}\n{reason}\n</drift_guard_continuation>",
        }
    return output
