import json
import os
from pathlib import Path
from typing import Any

import pytest
from conftest import events

from drift_guard import cli, credentials
from drift_guard.config import Config
from drift_guard.judge import JevJudge
from drift_guard.store import Store


def key_file(tmp_path: Path, content: bytes = b"private-test-key") -> Path:
    path = tmp_path / "key"
    path.write_bytes(content)
    path.chmod(0o600)
    return path


def test_environment_fallback_and_file_precedence(tmp_path: Path) -> None:
    assert credentials.api_key({}) is None
    assert credentials.api_key({"TYPESAFE_API_KEY": "env-key"}) == "env-key"
    path = key_file(tmp_path, b"file-key\r\n")
    assert credentials.api_key({"DRIFT_API_KEY_FILE": str(path), "TYPESAFE_API_KEY": "env-key"}) == "file-key"


@pytest.mark.parametrize("content", [b"", b"\n", b"has space", b"two\nlines", b" leading", b"x" * 4097, b"bad\xff", b"bad\x00"])
def test_rejects_bad_key_without_exposing_contents(tmp_path: Path, content: bytes) -> None:
    path = key_file(tmp_path, content)
    with pytest.raises(ValueError, match="owned private regular file"):
        credentials.api_key({"DRIFT_API_KEY_FILE": str(path)})


@pytest.mark.parametrize("kind", ["public", "symlink", "directory", "fifo", "missing", "wrong_owner"])
def test_rejects_unsafe_file(tmp_path: Path, kind: str, monkeypatch: pytest.MonkeyPatch) -> None:
    path = key_file(tmp_path)
    if kind == "public":
        path.chmod(0o644)
    elif kind == "symlink":
        link = tmp_path / "link"
        link.symlink_to(path)
        path = link
    elif kind == "directory":
        path = tmp_path
    elif kind == "fifo":
        path = tmp_path / "fifo"
        os.mkfifo(path, 0o600)
    elif kind == "missing":
        path = tmp_path / "missing"
    else:
        monkeypatch.setattr(os, "getuid", lambda: -1)
    with pytest.raises(ValueError, match="owned private regular file"):
        credentials.api_key({"DRIFT_API_KEY_FILE": str(path)})


def test_bad_credentials_fail_open_without_secret_logging(
    cfg: Config, store: Store, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = key_file(tmp_path, b"secret-with embedded-whitespace")
    monkeypatch.setenv("DRIFT_API_KEY_FILE", str(path))
    raw = {"hook_event_name": "UserPromptSubmit", "session_id": "secret-test", "prompt": "goal"}
    assert cli.run_hook("user-prompt", json.dumps(raw), cfg) == ""
    assert events(store)[-1]["action"] == "error"
    assert "secret-with" not in store.log_path.read_text()


def test_sdk_receives_file_key_and_errors_do_not_echo_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = key_file(tmp_path)
    monkeypatch.setenv("DRIFT_API_KEY_FILE", str(path))

    class Client:
        def __init__(self, **kwargs: Any) -> None:
            assert kwargs["api_key"] == "private-test-key"

        def system_one(self, **kwargs: Any) -> None:
            raise RuntimeError("private-test-key")

        def close(self) -> None:
            pass

    monkeypatch.setattr("typesafe_sdk.TypeSafeClient", Client)
    judge = JevJudge("jev-latest", 1)
    with pytest.raises(RuntimeError, match=r"^Jev request failed$"):
        judge({}, {})
    judge.close()


def test_relative_key_path_is_rejected() -> None:
    with pytest.raises(ValueError, match="absolute path"):
        credentials.api_key({"DRIFT_API_KEY_FILE": "key"})
