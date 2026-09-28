"""Command line entry point.

drift hook user-prompt   UserPromptSubmit hook (reads the payload on stdin)
drift hook stop          Stop hook
drift report [--json]    drift per session and adherence, live (guard on) vs backfill (pre-guard baseline)
drift backfill [...]     judge a sample of past transcripts to build the baseline (cost-capped)
drift status <session>   current intent and drift for one session
drift summarize <session>  write the intent recap a warning repeats back (started detached by the hook)
"""

import argparse
import contextlib
import json
import signal
import sys
from collections.abc import Callable
from dataclasses import asdict
from types import FrameType
from typing import Any

from drift_guard import codex, hooks, synthesis
from drift_guard import config as config_mod
from drift_guard.config import Config, Mode, Runtime
from drift_guard.judge import JevJudge
from drift_guard.store import Event, Store


class DeadlineExceeded(Exception):
    """The hook ran past its wall-clock budget; it exits silently and Claude Code proceeds."""


def _on_alarm(_signum: int, _frame: FrameType | None) -> None:
    raise DeadlineExceeded


def _handle(kind: str, raw: dict[str, Any], cfg: Config, store: Store) -> hooks.Output:
    judge = JevJudge(cfg.model, cfg.jev_timeout_s)
    try:
        if cfg.runtime == "codex":
            return codex.handle(kind, raw, cfg, store, judge)
        if kind == "user-prompt":
            return hooks.handle_user_prompt(hooks.PromptPayload.parse(raw), cfg, store, judge)
        assert kind == "stop", f"unknown hook kind {kind}"
        return hooks.handle_stop(hooks.StopPayload.parse(raw), cfg, store, judge)
    finally:
        judge.close()


def run_hook(
    kind: str,
    stdin: str,
    cfg: Config,
    handle: Callable[..., hooks.Output] = _handle,
    spawn_recap: synthesis.Spawner | None = None,
) -> str:
    """Run one hook and return what to print. Fails open: any error or timeout is logged and yields no output.

    This is the single fail-open boundary (a deliberate broad catch, Power-of-10 rule 7 deviation): a guard that
    can break Ben's prompt is worse than a guard that misses one turn.
    """
    store = Store(cfg.state_dir)
    session_id, event = "unknown", "human" if kind == "user-prompt" else "agent"
    previous = signal.signal(signal.SIGALRM, _on_alarm)
    signal.setitimer(signal.ITIMER_REAL, cfg.hook_deadline_s)
    try:
        raw = json.loads(stdin)
        session_id = str(raw.get("session_id", "unknown"))
        output = handle(kind, raw, cfg, store)
        if cfg.runtime == "claude" and spawn_recap is not None and kind == "user-prompt":
            synthesis.request_recap(store, session_id, spawn_recap)
    except (Exception, DeadlineExceeded) as exc:  # noqa: BLE001 - the fail-open boundary
        signal.setitimer(signal.ITIMER_REAL, 0)
        _log_failure(store, cfg, session_id, event, exc)
        return ""
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)
    return "" if output is None else json.dumps(output)


def _log_failure(store: Store, cfg: Config, session_id: str, event: str, exc: BaseException) -> None:
    safe_id = session_id if session_id.replace("-", "").isalnum() else "unknown"
    with contextlib.suppress(OSError):  # the log itself is unwritable; still fail open
        store.append(
            Event(
                session_id=safe_id, event=event, action="error", mode=cfg.mode.value, error=f"{type(exc).__name__}: {exc}"[:300]
            )
        )


def _hook_main(kind: str, runtime: Runtime = "claude") -> int:
    try:
        cfg = config_mod.from_env(runtime=runtime)
    except ValueError as exc:
        cfg = Config(runtime=runtime, state_dir=config_mod.state_dir_from(runtime=runtime))
        _log_failure(Store(cfg.state_dir), cfg, "unknown", "human", exc)
        return 0
    if cfg.mode is Mode.OFF:
        return 0
    out = run_hook(kind, sys.stdin.read(), cfg, spawn_recap=synthesis.spawn_summarize)
    if out:
        print(out)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="drift")
    sub = parser.add_subparsers(dest="command", required=True)
    hook = sub.add_parser("hook")
    hook.add_argument("kind", choices=["user-prompt", "stop"])
    codex_hook = sub.add_parser("codex-hook")
    codex_hook.add_argument("kind", choices=["user-prompt", "stop"])
    report = sub.add_parser("report")
    report.add_argument("--json", action="store_true")
    backfill = sub.add_parser("backfill")
    backfill.add_argument("--sessions", type=int, default=40)
    backfill.add_argument("--max-turns", type=int, default=25)
    backfill.add_argument("--budget-usd", type=float, default=1.0)
    status = sub.add_parser("status")
    status.add_argument("session_id")
    summarize = sub.add_parser("summarize")
    summarize.add_argument("session_id")
    args = parser.parse_args(argv)

    if args.command == "hook":
        return _hook_main(args.kind)
    if args.command == "codex-hook":
        return _hook_main(args.kind, "codex")
    cfg = config_mod.from_env()
    if args.command == "report":
        from drift_guard import report as report_mod

        print(report_mod.render(Store(cfg.state_dir), as_json=args.json))
        return 0
    if args.command == "backfill":
        from drift_guard import backfill as backfill_mod

        return backfill_mod.run(cfg, args.sessions, args.max_turns, args.budget_usd)
    if args.command == "summarize":
        return 0 if synthesis.write_recap(Store(cfg.state_dir), args.session_id) else 1
    state = Store(cfg.state_dir).load(args.session_id)
    print(json.dumps(asdict(state), indent=2) if state else f"no state for session {args.session_id}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
