"""The intent recap: a few synthesized lines that a warning repeats back to say what the session is for.

Jev scores; it does not write. The recap is written once per intent by Claude (Haiku) through the `claude` CLI, in
a detached `drift summarize` process, because it takes seconds and a hook has under two. Until it lands, or if
it never does, warnings fall back to headline(). Display only: the judge always gets the full intent.

The child `claude` must not re-enter this guard or any other hook: it skips user settings (where the hooks live),
has no tools, persists no session, and runs with DRIFT_MODE=off as a second lock.
"""

import hashlib
import os
import re
import subprocess
import sys
import tempfile
from collections.abc import Callable

from drift_guard.store import Recap, SessionState, Store

RECAP_LINES = 3
RECAP_LINE_CHARS = 110
SYNTHESIS_TIMEOUT_S = 60
RETRY_AFTER_S = 600  # a pending or failed recap is not requested again before this
MODEL = "haiku"
HEADLINE_CHARS = 120
PASTE_TAG = re.compile(r"</?pasted_content[^>]*>")
FIRST_SENTENCE = re.compile(r"^(.+?[.!?])(?:\s|$)")

PROMPT = """Below is the opening request of a coding-agent session. Restate what the session is for in 2 or 3 short \
lines, so the person can be reminded of it later: the goal first, then the scope limits or the finish line that \
matter most. Plain text, one idea per line, no bullets, no preamble. Reply with only the lines.

<request>
{intent}
</request>"""

Runner = Callable[[str], str | None]
Spawner = Callable[[str], None]


def intent_key(intent: str) -> str:
    """Which intent a recap was written for, so a changed intent never shows a stale recap."""
    return hashlib.sha256(intent.encode()).hexdigest()[:16]


def run_claude(prompt: str) -> str | None:
    """One headless Haiku call; None on any failure or timeout."""
    env = {**os.environ, "DRIFT_MODE": "off"}
    command = [
        "claude",
        "-p",
        "--model",
        MODEL,
        "--setting-sources",
        "",
        "--tools",
        "",
        "--no-session-persistence",
    ]
    with tempfile.TemporaryDirectory() as scratch:  # no project CLAUDE.md for it to read
        try:
            done = subprocess.run(
                command,
                input=prompt,
                capture_output=True,
                text=True,
                cwd=scratch,
                env=env,
                timeout=SYNTHESIS_TIMEOUT_S,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
    return done.stdout if done.returncode == 0 else None


def recap(intent: str, run: Runner = run_claude) -> list[str] | None:
    """The synthesized recap lines for `intent`, or None if the model gave nothing usable."""
    reply = run(PROMPT.format(intent=intent))
    if reply is None:
        return None
    lines = [line.strip().lstrip("-*• ").strip() for line in reply.splitlines()]
    lines = [_fit(line) for line in lines if line][:RECAP_LINES]
    return lines or None


def _fit(line: str) -> str:
    if len(line) <= RECAP_LINE_CHARS:
        return line
    return line[: RECAP_LINE_CHARS - 3].rsplit(" ", 1)[0] + "..."


def headline(intent: str, limit: int = HEADLINE_CHARS) -> str:
    """The intent as Ben would say it back: its opening sentence, without paste wrappers.

    Display only. A long pasted intent clipped head-and-tail opens on a tag and ends mid-instruction, so the
    warning meant to remind him what the session is for says nothing readable. The judge still gets it all.
    """
    lines = [line.strip() for line in PASTE_TAG.sub("", intent).splitlines()]
    first = next((line for line in lines if line), "")
    sentence = FIRST_SENTENCE.match(first)
    text = sentence.group(1) if sentence else first
    if len(text) <= limit:
        return text
    return text[: limit - 3].rsplit(" ", 1)[0] + "..."


def recap_lines(store: Store, state: SessionState) -> list[str]:
    """What a warning repeats back: the synthesized recap of this intent once written, else its headline."""
    recap = store.load_recap(state.session_id)
    if recap is not None and recap.status == "ready" and recap.intent_key == intent_key(state.intent):
        return list(recap.lines)
    return [headline(state.intent)]


def needs_recap(store: Store, state: SessionState) -> bool:
    """True when no recap of the current intent exists or is on its way (a stuck or failed one retries later)."""
    recap = store.load_recap(state.session_id)
    if recap is None or recap.intent_key != intent_key(state.intent):
        return True
    return recap.status != "ready" and recap.age_s() >= RETRY_AFTER_S


def request_recap(store: Store, session_id: str, spawn: Spawner) -> None:
    """After a prompt hook: if the current intent has no recap on its way, mark one pending and start the writer."""
    state = store.load(session_id)
    if state is None or not needs_recap(store, state):
        return
    store.save_recap(session_id, Recap(intent_key(state.intent), "pending"))
    spawn(session_id)


def write_recap(store: Store, session_id: str, run: Runner = run_claude) -> bool:
    """The detached writer: synthesize the recap and save it, unless the intent changed while it was written."""
    state = store.load(session_id)
    if state is None:
        return False
    key = intent_key(state.intent)
    lines = recap(state.intent, run)
    current = store.load(session_id)
    if current is None or intent_key(current.intent) != key:
        return False  # the newer intent requests its own recap
    store.save_recap(session_id, Recap(key, "ready", tuple(lines)) if lines else Recap(key, "failed"))
    return lines is not None


def spawn_summarize(session_id: str) -> None:
    """Start `drift summarize <session>` detached, so the hook returns at once."""
    subprocess.Popen(
        [sys.executable, "-m", "drift_guard.cli", "summarize", session_id],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
