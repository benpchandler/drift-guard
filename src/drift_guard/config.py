"""Runtime configuration, read once from the environment at the hook boundary."""

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Literal

from drift_guard.drift import Thresholds


class Mode(StrEnum):
    OFF = "off"  # hooks exit immediately; nothing is judged or logged
    WARN = "warn"  # default: judge, log, and warn; never block
    BLOCK = "block"  # opt-in: also block past the block threshold


# Jev answers in ~0.25s; the whole UserPromptSubmit hook must finish well inside 2s.
DEFAULT_JEV_TIMEOUT_S = 1.2
DEFAULT_HOOK_DEADLINE_S = 1.8
DEFAULT_STATE_DIR = Path.home() / ".local/state/drift"
DEFAULT_CODEX_STATE_DIR = Path.home() / ".local/state/drift-codex"
Runtime = Literal["claude", "codex"]


@dataclass(frozen=True)
class Config:
    runtime: Runtime = "claude"
    mode: Mode = Mode.WARN
    agent: bool = True  # judge agent replies on Stop
    thresholds: Thresholds = field(default_factory=Thresholds)
    jev_timeout_s: float = DEFAULT_JEV_TIMEOUT_S
    hook_deadline_s: float = DEFAULT_HOOK_DEADLINE_S
    state_dir: Path = DEFAULT_STATE_DIR
    model: str = "jev-latest"

    @property
    def blocking(self) -> bool:
        return self.mode is Mode.BLOCK


def _float(env: Mapping[str, str], key: str, default: float) -> float:
    raw = env.get(key)
    return default if raw is None or raw == "" else float(raw)


def state_dir_from(env: Mapping[str, str] | None = None, *, runtime: Runtime = "claude") -> Path:
    """The state dir alone, so a hook whose other settings are malformed still logs to the right place."""
    env = os.environ if env is None else env
    default = DEFAULT_CODEX_STATE_DIR if runtime == "codex" else DEFAULT_STATE_DIR
    return Path(env.get("DRIFT_STATE_DIR") or default).expanduser()


def from_env(env: Mapping[str, str] | None = None, *, runtime: Runtime = "claude") -> Config:
    """Build the config from DRIFT_* variables. Unknown or malformed values raise; callers fail open."""
    env = os.environ if env is None else env
    base = Thresholds()
    thresholds = Thresholds(
        alpha=_float(env, "DRIFT_ALPHA", base.alpha),
        warn=_float(env, "DRIFT_WARN", base.warn),
        block=_float(env, "DRIFT_BLOCK", base.block),
    )
    return Config(
        runtime=runtime,
        mode=Mode(env.get("DRIFT_MODE", Mode.WARN.value).lower()),
        agent=env.get("DRIFT_AGENT", "on").lower() not in {"off", "0", "false", "no"},
        thresholds=thresholds,
        jev_timeout_s=_float(env, "DRIFT_JEV_TIMEOUT", DEFAULT_JEV_TIMEOUT_S),
        hook_deadline_s=_float(env, "DRIFT_DEADLINE", DEFAULT_HOOK_DEADLINE_S),
        state_dir=state_dir_from(env, runtime=runtime),
        model=env.get("DRIFT_MODEL", "jev-latest"),
    )
