import json
import time
from collections.abc import Callable
from typing import Any

from conftest import events

from drift_guard import cli
from drift_guard.config import Config
from drift_guard.store import Store

Make = Callable[..., dict[str, Any]]


def test_judge_error_yields_no_output_and_is_logged(payload: Make, cfg: Config, store: Store) -> None:
    def boom(kind: str, raw: dict[str, Any], cfg: Config, store: Store) -> None:
        raise ConnectionError("jev down")

    assert cli.run_hook("user-prompt", json.dumps(payload("x")), cfg, handle=boom) == ""
    logged = events(store)[-1]
    assert logged["action"] == "error" and "jev down" in logged["error"] and logged["session_id"] == payload("x")["session_id"]


def test_deadline_cuts_a_slow_hook(payload: Make, tmp_path: Any) -> None:
    cfg = Config(state_dir=tmp_path / "s", hook_deadline_s=0.2)

    def slow(kind: str, raw: dict[str, Any], cfg: Config, store: Store) -> None:
        time.sleep(5)

    started = time.monotonic()
    assert cli.run_hook("user-prompt", json.dumps(payload("x")), cfg, handle=slow) == ""
    assert time.monotonic() - started < 1.0
    assert events(Store(cfg.state_dir))[-1]["error"].startswith("DeadlineExceeded")


def test_malformed_stdin_fails_open(cfg: Config, store: Store) -> None:
    assert cli.run_hook("stop", "not json", cfg) == ""
    assert events(store)[-1]["action"] == "error"


def test_wrong_event_payload_fails_open(payload: Make, cfg: Config, store: Store, monkeypatch: Any) -> None:
    monkeypatch.setenv("TYPESAFE_API_KEY", "unused")
    assert cli.run_hook("stop", json.dumps(payload("x")), cfg) == ""
    assert "not a Stop payload" in events(store)[-1]["error"]


def test_success_prints_json(payload: Make, cfg: Config) -> None:
    def ok(kind: str, raw: dict[str, Any], cfg: Config, store: Store) -> dict[str, Any]:
        return {"systemMessage": "hi"}

    assert json.loads(cli.run_hook("user-prompt", json.dumps(payload("x")), cfg, handle=ok)) == {"systemMessage": "hi"}


def test_off_mode_does_nothing(monkeypatch: Any, tmp_path: Any, capsys: Any) -> None:
    monkeypatch.setenv("DRIFT_MODE", "off")
    monkeypatch.setenv("DRIFT_STATE_DIR", str(tmp_path / "s"))
    assert cli.main(["hook", "user-prompt"]) == 0
    assert capsys.readouterr().out == "" and not (tmp_path / "s").exists()


def test_bad_config_fails_open_and_logs_to_configured_dir(monkeypatch: Any, capsys: Any, tmp_path: Any) -> None:
    monkeypatch.setenv("DRIFT_MODE", "loud")
    monkeypatch.setenv("DRIFT_STATE_DIR", str(tmp_path / "s"))
    assert cli.main(["hook", "user-prompt"]) == 0
    assert capsys.readouterr().out == ""
    assert "not a valid Mode" in events(Store(tmp_path / "s"))[-1]["error"]
