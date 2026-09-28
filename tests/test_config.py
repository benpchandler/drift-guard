import pytest

from drift_guard.config import Mode, from_env


def test_defaults_are_warn_only() -> None:
    cfg = from_env({})
    assert cfg.mode is Mode.WARN and not cfg.blocking and cfg.agent
    assert cfg.hook_deadline_s < 2.0 and cfg.jev_timeout_s < cfg.hook_deadline_s


def test_blocking_is_opt_in() -> None:
    cfg = from_env({"DRIFT_MODE": "BLOCK", "DRIFT_AGENT": "off", "DRIFT_WARN": "0.4", "DRIFT_BLOCK": "0.9"})
    assert cfg.blocking and not cfg.agent and cfg.thresholds.warn == 0.4 and cfg.thresholds.block == 0.9


def test_invalid_values_raise() -> None:
    with pytest.raises(ValueError):
        from_env({"DRIFT_MODE": "loud"})
    with pytest.raises(ValueError):
        from_env({"DRIFT_WARN": "0.9", "DRIFT_BLOCK": "0.5"})
