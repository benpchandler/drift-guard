"""Load optional GUI-friendly credentials without exposing file content in errors."""

import os
import stat
from collections.abc import Mapping
from pathlib import Path

MAX_KEY_BYTES = 4096


def api_key(env: Mapping[str, str] | None = None) -> str | None:
    """An explicit private file wins over TYPESAFE_API_KEY; otherwise preserve SDK env handling."""
    env = os.environ if env is None else env
    filename = env.get("DRIFT_API_KEY_FILE")
    if not filename:
        return env.get("TYPESAFE_API_KEY") or None
    try:
        path = Path(filename).expanduser()
        if not path.is_absolute():
            raise ValueError
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise ValueError
            if info.st_size > MAX_KEY_BYTES:
                raise ValueError
            raw = handle.read(MAX_KEY_BYTES + 1)
        key = raw.decode("ascii").rstrip("\r\n")
        if not key or len(raw) > MAX_KEY_BYTES or any(c.isspace() or not c.isprintable() for c in key):
            raise ValueError
    except (OSError, ValueError):
        raise ValueError(
            "DRIFT_API_KEY_FILE must be an absolute path to an owned private regular file containing one non-whitespace key"
        ) from None
    return key
