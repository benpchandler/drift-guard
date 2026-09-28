# Drift

A hook pair for Claude Code and Codex that remembers what a session is for and flags when the conversation drifts away from it.
Jev (TypeSafe System One) judges each turn; code owns the scoring, thresholds, and actions.

- **UserPromptSubmit** (human drift): the first typed prompt becomes the session intent. Each later prompt is
  judged against it, with the agent's previous reply as context so "yes, do it" counts as on-task.
- **Stop** (agent drift): the agent's final reply is judged against the intent and the latest request; it counts
  as drift only when it serves neither.

## Install (Claude Code)

Requires Python 3.13+, [uv](https://docs.astral.sh/uv/), `jq` (for the status line), and a TypeSafe API key.
The `claude` CLI is used for warning recaps; without it, warnings fall back to the intent's opening sentence.

    git clone https://github.com/benpchandler/drift-guard.git
    cd drift-guard
    uv sync
    export TYPESAFE_API_KEY=...   # or DRIFT_API_KEY_FILE, see Configuration

Add both hooks to `~/.claude/settings.json`, using the absolute path to your clone:

    {
      "hooks": {
        "UserPromptSubmit": [
          { "hooks": [{ "type": "command", "command": "/path/to/drift-guard/.venv/bin/drift hook user-prompt", "timeout": 5 }] }
        ],
        "Stop": [
          { "hooks": [{ "type": "command", "command": "/path/to/drift-guard/.venv/bin/drift hook stop", "timeout": 5 }] }
        ]
      }
    }

Optional status line. Use `scripts/statusline.sh` on its own, or pipe the status-line payload to it from your
existing script:

    { "statusLine": { "type": "command", "command": "/path/to/drift-guard/scripts/statusline.sh" } }

Start a new session; your first prompt becomes its intent.

## What a warning repeats back (`src/drift_guard/synthesis.py`)

For Claude Code, every warning restates what the session is for as a 2-3 line recap synthesized from the intent by Claude Haiku,
through the `claude` CLI. The hook cannot wait seconds for a model, so after a prompt it starts
`drift summarize <session>` detached, once per intent, and the recap lands in `recaps/<session>.json`. Until
then, or if the call fails, a warning shows the intent's opening sentence instead. The child `claude` skips user
settings (so no hooks run), has no tools, persists no session, and runs with `DRIFT_MODE=off`. Display only:
Jev always judges against the full intent.

## Escapes (type at the start of a prompt)

Every escape starts with `drift:`.

- `drift: new intent <text>` - replace the intent and reset drift.
- `drift: ack` (optionally `drift: ack <note>`) - keep the intent, clear the drift score, let this prompt through.
- `drift: feedback <label> [note]` - label the guard's accuracy. The prompt is blocked, so it never reaches Claude and
  costs no Claude turn (verified: 0 API calls); you see a confirmation such as
  `Recorded: guard was wrong on '<prompt>'. Drift reset.` No Jev call either.
- `drift:` with anything else (a typo, or `drift: new intent` with no text) is blocked with a usage line rather than
  being sent to Claude or judged.

## Feedback labels (`src/drift_guard/feedback.py`)

| You type | After a warning | After a quiet turn |
| --- | --- | --- |
| `drift: feedback right` (correct, accurate, good, yes) | true positive | true negative |
| `drift: feedback wrong` (incorrect, bad, no) | false positive; resets that drift so it is not flagged again | missed drift |
| `drift: feedback missed` (miss) | kept as a note (it did flag) | missed drift |
| `drift: feedback <any other text>` | note | note |

A label attaches to the latest exchange: the warning on your last prompt or the agent's reply to it if either
warned, otherwise your last judged prompt. Text after the label word is kept as a note. Labels go to
`labels.jsonl` next to `judgments.jsonl`, linked by judgment id, session id and question-set version; the latest
label per judgment counts. `drift report` shows labeled count, warning precision (right / labeled warnings),
and missed drifts.

## Policy (`src/drift_guard/drift.py`)

Per turn, drift = P(unrelated) + 0.5 x P(tangential). The running score is an EMA (alpha 0.6).
Warn at 0.5 (one clearly off-topic prompt), block zone at 0.8 (two in a row). Warnings are edge-triggered: once
per zone per excursion. Blocking is opt-in and repeats while in the block zone.

## Configuration (environment)

| Variable | Default | Meaning |
| --- | --- | --- |
| `DRIFT_MODE` | `warn` | `off`, `warn`, or `block` (opt-in) |
| `DRIFT_AGENT` | `on` | judge agent replies on Stop |
| `DRIFT_ALPHA` / `_WARN` / `_BLOCK` | 0.6 / 0.5 / 0.8 | policy knobs |
| `DRIFT_JEV_TIMEOUT` / `_DEADLINE` | 1.2 / 1.8 s | Jev timeout, whole-hook wall clock |
| `DRIFT_STATE_DIR` | `~/.local/state/drift` | session state, `judgments.jsonl`, `labels.jsonl` |

Set `TYPESAFE_API_KEY`, or set `DRIFT_API_KEY_FILE` to an absolute path to an existing private key file.
The file takes precedence when configured. It must be a regular non-symlink file, owned by the current user,
with no group/other permission bits and at most 4096 bytes. Terminal CR/LF is allowed; embedded whitespace,
empty keys and non-printable/non-ASCII content are rejected. The key is passed directly to the SDK and is
never logged. File-validation and SDK errors use generic messages. Any error or timeout fails open: no output,
one `error` line in the log.

## Codex native hooks

The adapter uses the native `UserPromptSubmit` and `Stop` payloads described in the
[official hook documentation](https://learn.chatgpt.com/docs/hooks). Configure the native command hooks to run:

    /absolute/path/to/drift codex-hook user-prompt
    /absolute/path/to/drift codex-hook stop

Hook enablement and trust are configured in Codex; this package does not change those settings. For a GUI
that does not inherit `TYPESAFE_API_KEY`, the hook command can set `DRIFT_API_KEY_FILE` to the existing private
key path. Set `DRIFT_MODE=warn` explicitly in the command to keep warnings advisory.

Codex stores its state separately at `~/.local/state/drift-codex` by default. An explicit `DRIFT_STATE_DIR`
overrides that location, so use a separate directory to keep the two runtimes' session state and reports apart.
To inspect Codex state, set that same directory for the existing commands:

    DRIFT_STATE_DIR="$HOME/.local/state/drift-codex" drift report
    DRIFT_STATE_DIR="$HOME/.local/state/drift-codex" drift status <session>

Codex's transcript format is not a stable hook interface. The adapter deliberately ignores `transcript_path`
and uses `prompt` and `last_assistant_message` directly. The first prompt observed after enabling the adapter
becomes its intent, including in resumed chats; it cannot recover the original goal from earlier history.
Use `drift: new intent <full goal>` if that first message is only a short continuation. Later direct replies
are saved as context for subsequent short approvals. No Claude recap subprocess is spawned: warning text
uses the intent's opening sentence. No custom Codex status line is added.

The scoring questions, thresholds, warn default and fail-open policy are shared with Claude Code. In optional
block mode, human prompt blocks use the native `decision: block` response. Stop blocks ask Codex for a refocused
continuation with `decision: block` and `reason`; they do not halt the session. `stop_hook_active` suppresses
repeat judgment, and the adapter ignores its own marked continuation prompt and the existing
`[Jev Stop review]` continuation prefix when tracking human drift. Warn mode never requests continuation.
The adapter handles only these two top-level events, not `SubagentStop` or subagent transcript parsing.

## Commands

    uv run drift report            # adherence baseline vs live, feedback accuracy, per session, weekly
    uv run drift backfill          # judge recent past transcripts for the pre-guard baseline (--budget-usd 1.0)
    uv run drift status <session>  # current intent and drift for one session
    uv run pytest && uv run ruff check && uv run mypy src
    scripts/e2e_pipe.sh                 # real captured payloads through the hook, live Jev, isolated state
    scripts/e2e_live.sh                 # claude -p: intent, drift warning, feedback label (temporary --settings)
