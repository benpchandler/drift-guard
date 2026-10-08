# Drift

Drift remembers what a session is for. Claude Code and Codex use a drift-detection hook pair;
Pi uses an intent ledger integrated with bounded next-safe follow-ups.
For Claude Code/Codex, Jev (TypeSafe System One) judges each turn; code owns the scoring, thresholds, and actions.
The Pi integration below makes no Jev calls and does not change either existing adapter's scoring policy.

- **UserPromptSubmit** (human drift): the first typed prompt becomes the session intent. Each later prompt is
  judged against it, with the agent's previous reply as context so "yes, do it" counts as on-task.
- **Stop** (agent drift): the agent's final reply is judged against the intent and the latest request; it counts
  as drift only when it serves neither.

## Install (Pi)

Requires Pi 0.87.1+, Python 3.13+ and `uv`. In this checkout:

```sh
uv sync
pi install /absolute/path/to/drift-guard
```

Retire any older standalone `next-safe.js` extension before reloading: this package owns the hook now,
so keeping both active would register duplicate commands/continuations. Reload Pi or start a fresh session.
The package's extension calls its checkout's `.venv/bin/python`; run `uv sync` after moving/updating the checkout.
No TypeSafe key is needed for the Pi intent ledger.

### Original intent and supporting task

The first human task prompt is stored verbatim as the original intent. Later prompts update the latest request,
not the original. On an existing session, the selected branch's saved Drift snapshot is restored; if none exists,
its first user message is the initial intent. Extension-generated input never establishes or replaces the intent.
Use `/drift intent FULL GOAL` when deliberately changing the session's purpose; `/drift status` shows the ledger.
Only that user command can replace the original through the Pi interface.

The model-facing `drift_task` tool maintains one active **supporting task**: task text, why it advances the original,
owner (including a worker/run or SBT reference), completion condition, status and evidence. It must be recorded
before an intermediate assignment is handed off. `block`/`resume` retain the task; `complete`/`cancel` require
its current id and evidence, archive it, clear the active slot and return to the original goal. A second assignment
cannot silently overwrite an unfinished task. Changing the original intent archives an unfinished task as canceled.

This is an intent/context ledger, **not a second project backlog**, a claim, or authority to add scope. SBT remains
the coordination/task hub. Claude/Codex session records are backward compatible with the added optional fields;
their existing relevance questions and warning behavior remain unchanged.

### Default-on, configurable next-safe

Next-safe defaults **on**, with **at most three follow-ups per human prompt** and a review window of the last
**20 user/assistant interaction messages** (not physical JSONL lines). Tool output and automatic custom prompts
are excluded from this window; context edits/compaction are respected. Longer messages are explicitly excerpted.
The full original intent remains in state. The hook references both intents, the latest request and recent interaction.
Before a further action, it asks the agent to state `User said / Supporting task / My assumption and next action`.
It does not require a repeated preamble when stopping. Automatic visible replies are instructed to stay within
**100 words total**, including the preamble and final reply, quoting at most 12 user words; this is model guidance, not a hard text truncator.
The hook context renders as one compact TUI line, with the full context available through Pi's expand shortcut.
The output budget does not limit tool work. An answered question is not necessarily fulfilled practical intent:
prepare a directly necessary missing artifact when already authorized, without starting unrelated optional projects.

```text
/next-safe status
/next-safe limit 3       # maximum automatic follow-ups for each subsequent human prompt (0..10)
/next-safe history 30    # recent user/assistant messages to inspect (1..100)
/next-safe off
/next-safe on
```

Preferences persist in Pi's agent directory as `next-safe.json` across reloads and new sessions. An explicit
`off` overrides the on-by-default behavior until `on`; `limit 0` also disables injection. Config changes apply
to the next human prompt and never replenish an active budget. Invalid commands leave preferences unchanged;
malformed configuration disables automatic continuation until repaired. There is no unbounded option or timer.

The limit is a **ceiling**, not a quota. Text-only follow-ups, failed tools and repeated identical tool operations
do not replenish progress. Distinct successful tool operations allow another check within the budget; this is
an observable activity guard, not proof of semantic progress. `next_safe_stop` ends the current budget immediately
when done/blocked/awaiting a worker or when no useful authorized action remains. It requires explanatory evidence;
`done` is rejected while an unfinished supporting task exists. Finish/archive that task with evidence first, or
report the actual blocker/wait rather than claiming completion. A firing is not a one-tool quota: normal tool
use may continue to a verified milestone. Abort/provider failure cancels
remaining checks. Queued human input and other continuation hooks take priority. Pi subagent child processes
are excluded so their parent's bounded assignment does not grow a second loop.

Drift snapshots use branch-relative Pi custom entries; an external mirror lives at `~/.local/state/drift-pi`.
Reload, resume, fork/clone and tree navigation restore only the selected branch, including completed-task history.
An unavailable state bridge stops automatic continuation, reports the tracking failure and leaves human input usable.
The ledger does not authorize unrelated bug fixing when the original task is done. Agent adherence to the
quote/assumption instructions is still model behavior, not a sandbox guarantee.

Overrides for isolated testing or alternative installations: `DRIFT_PI_STATE_DIR`, `DRIFT_NEXT_SAFE_CONFIG`,
`DRIFT_PYTHON`. To inspect the mirror with the existing CLI:

```sh
DRIFT_STATE_DIR="$HOME/.local/state/drift-pi" uv run drift status SESSION_ID
uv run python scripts/verify_pi.py --evidence-dir /tmp/drift-pi-evidence
```

The verifier drives the actual Pi RPC runtime using an isolated agent directory, state directory and deterministic
loopback model. It covers tool-based task lifecycle, original-intent persistence, default-on/disable, bounded
multi-follow-up progress, repeated/no-progress stopping, queued input, abort/error, actual runtime reload,
compaction, branch navigation, clone/switch, resume and invalid configuration. It proves transport/lifecycle,
not an external model's judgment about scope or implied intent. It also exercises the native TUI's compact
hook rendering and real keyboard expansion. See [the behavioral benchmark](benchmarks/README.md) for
opt-in live-model decision replays and passive collection of actual firings; those are separate evidence streams.

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

Codex requires every `Stop` hook to print either nothing or a single JSON object. Drift's stop hook prints nothing
on a quiet turn and on every failure. If Codex shows `Hook failed` with `hook returned invalid stop hook JSON
output`, look for another `Stop` hook, including project-level `.codex/hooks.json` files, that echoes plain text. Such a
hook needs to redirect its stdout, for example to stderr.

## Commands

    uv run drift report            # adherence baseline vs live, feedback accuracy, per session, weekly
    uv run drift backfill          # judge recent past transcripts for the pre-guard baseline (--budget-usd 1.0)
    uv run drift status <session>  # current intent and drift for one session
    uv run pytest && uv run ruff check && uv run mypy src
    scripts/e2e_pipe.sh                 # real captured payloads through the hook, live Jev, isolated state
    scripts/e2e_live.sh                 # claude -p: intent, drift warning, feedback label (temporary --settings)
