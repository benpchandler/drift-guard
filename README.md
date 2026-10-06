# Drift

**Keep your coding session on track.** Drift remembers what you set out to do and warns when you or your AI assistant veer off-task. Works with **Claude Code and Codex**.

You start with a bug fix. A few turns later, you're redesigning something the fix never needed. Drift gives you a reminder of the original goal before the side quest takes over.

It checks both sides of the conversation:

- **Your prompts:** is this request helping with the session's goal, or starting a different task?
- **The assistant's replies:** is the assistant addressing the goal or your latest request?

Warnings are advisory by default. You can acknowledge a detour, change the goal, or tell Drift it got the warning wrong. Blocking off-topic prompts is opt-in.

## What it looks like

An illustrative session:

```text
You: Fix the login timeout without changing the authentication flow.
     → Drift records the session goal.

You: Add a regression test for slow connections.
     → A step toward the goal; no warning expected.

You: Let's also redesign the billing dashboard.
     → Drift warns that the conversation is moving away from the login fix.

You: drift: new intent Redesign the billing dashboard.
     → Drift adopts the new goal and resets the drift scores.
```

Short replies like “yes, do it” are evaluated with the assistant's previous reply as context, rather than treated as unrelated messages.

Drift is a relevance check, not a code reviewer or a guarantee that the work is correct. It checks submitted prompts and final replies—not every tool call or code change—and its judgments can be wrong.

## Before you install

You'll need **Python 3.13+**, [uv](https://docs.astral.sh/uv/), and a [TypeSafe](https://typesafe.ai) API key. The hook runner uses Unix signal timers; use macOS or Linux rather than native Windows.

**Data and API use:** Drift sends excerpts of the session goal, prompt or reply, and relevant conversational context to TypeSafe's Jev model to judge relevance. Session state and feedback are saved locally and can contain conversation text. Claude Code can also use the `claude` CLI to generate a short goal recap. These model calls use your provider accounts; Drift is not an offline tool.

On errors or timeouts, Drift **fails open**: it lets the session continue without a warning and records an error locally.

## Quickstart

### 1. Install Drift

```bash
git clone https://github.com/benpchandler/drift-guard.git
cd drift-guard
uv sync
export TYPESAFE_API_KEY="your-typesafe-api-key"
```

Launch your coding agent from a shell that has the key set. For apps that don't inherit your shell environment, use a private key file instead; see [Configuration](#configuration).

### 2. Connect your coding agent

Use the instructions for your agent below. Replace `/path/to/drift-guard` with the **absolute path** to your clone. Merge the hooks into any existing configuration; don't replace your other settings or hooks.

<details open>
<summary><strong>Claude Code</strong></summary>

Add these hooks to `~/.claude/settings.json`:

```json
{
  "hooks": {
    "UserPromptSubmit": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "/path/to/drift-guard/.venv/bin/drift hook user-prompt",
            "timeout": 5
          }
        ]
      }
    ],
    "Stop": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "/path/to/drift-guard/.venv/bin/drift hook stop",
            "timeout": 5
          }
        ]
      }
    ]
  }
}
```

The `claude` CLI is optional for short warning recaps. Without it, warnings repeat the goal's opening sentence.

**Optional status line:** requires `jq`. Add this setting, or pipe your existing status-line payload to `scripts/statusline.sh`:

```json
{
  "statusLine": {
    "type": "command",
    "command": "/path/to/drift-guard/scripts/statusline.sh"
  }
}
```

</details>

<details>
<summary><strong>Codex</strong></summary>

Add these native hooks to `~/.codex/hooks.json`:

```json
{
  "hooks": {
    "UserPromptSubmit": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "DRIFT_MODE=warn /path/to/drift-guard/.venv/bin/drift codex-hook user-prompt",
            "timeout": 5
          }
        ]
      }
    ],
    "Stop": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "DRIFT_MODE=warn /path/to/drift-guard/.venv/bin/drift codex-hook stop",
            "timeout": 5
          }
        ]
      }
    ]
  }
}
```

Use `/hooks` in the Codex CLI to review and trust the hooks. Hook enablement and trust are controlled by Codex, not Drift; see the [official hook documentation](https://learn.chatgpt.com/docs/hooks). The explicit `DRIFT_MODE=warn` keeps these hooks advisory even if your shell has another mode set.

For a GUI without your shell's API key, prefix each hook command with `DRIFT_API_KEY_FILE=/absolute/path/to/private-key-file`.

Codex state is stored separately at `~/.local/state/drift-codex`. To inspect it from your clone:

```bash
DRIFT_STATE_DIR="$HOME/.local/state/drift-codex" uv run drift report
DRIFT_STATE_DIR="$HOME/.local/state/drift-codex" uv run drift status <session-id>
```

Codex uses the first prompt seen after enabling Drift as the goal, even in a resumed chat. If that prompt is only “continue,” set the full goal with `drift: new intent <full goal>`. The adapter does not read earlier Codex history, generate Claude recaps, or add a Codex status line.

</details>

### 3. Start with a clear goal

Start a new session and describe what you want done. Your first typed task prompt becomes the session's goal (called its **intent** in Drift's state and commands). Later prompts and final replies are checked against it.

When Drift warns, it repeats the goal and shows commands for continuing or changing direction. In an existing Claude Code session, Drift can recover the first prompt from the transcript.

## You're in control

Type these at the **start of a prompt**:

| Command | What it does |
| --- | --- |
| `drift: new intent <goal>` | Switch goals and reset both drift scores. |
| `drift: ack [note]` | Acknowledge a detour, keep the goal, and reset both scores. This prompt goes through to the assistant. |
| `drift: feedback right [note]` | Record that Drift's latest judgment was correct. |
| `drift: feedback wrong [note]` | Record a mistaken warning or missed drift. A mistaken warning resets the affected score. |
| `drift: feedback missed [note]` | Record drift that wasn't flagged. |

Feedback is handled by Drift without sending a turn to the assistant or making a Jev judgment call. A mistyped `drift:` command is also intercepted and answered with usage instructions.

To disable checks, set `DRIFT_MODE=off` in the agent's environment (or hook command). To remove Drift, remove its hook entries and optional status-line setting.

## Configuration

Set environment variables where your agent's hooks can inherit them, or prefix the hook commands with them.

| Variable | Default | Purpose |
| --- | --- | --- |
| `TYPESAFE_API_KEY` | — | API key for relevance judgments. |
| `DRIFT_API_KEY_FILE` | — | Absolute path to a private key file; takes precedence over the environment key. |
| `DRIFT_MODE` | `warn` | `off`, `warn`, or opt-in `block`. |
| `DRIFT_AGENT` | `on` | Set to `off` to skip assistant-reply judgments. |
| `DRIFT_ALPHA` | `0.6` | Weight given to the latest judgment in the running score. |
| `DRIFT_WARN` / `DRIFT_BLOCK` | `0.5` / `0.8` | Warning and blocking thresholds. |
| `DRIFT_JEV_TIMEOUT` / `DRIFT_DEADLINE` | `1.2` / `1.8` seconds | Model-request timeout and whole-hook deadline. |
| `DRIFT_MODEL` | `jev-latest` | Jev model to use. |
| `DRIFT_STATE_DIR` | `~/.local/state/drift` (Claude Code); `~/.local/state/drift-codex` (Codex) | Local state, judgment logs, recaps, and feedback. |

If you override `DRIFT_STATE_DIR`, use different directories for Claude Code and Codex so their session state and reports stay separate.

A key file must be a regular, non-symlink file owned by your user, with no group or other permissions (`chmod 600`) and at most 4096 bytes. A trailing newline is allowed; empty keys, embedded whitespace, and non-printable or non-ASCII content are rejected. Keys are passed directly to the SDK and are not logged; key-validation and SDK errors use generic messages.

**Block mode:** off-topic human prompts can be blocked once the score reaches the blocking threshold. Claude Code's Stop hook adds refocusing context; Codex's Stop hook requests a refocused continuation rather than halting the session. The Codex quickstart explicitly sets `warn`; change that prefix if you want block mode.

## Reports and feedback

Run these from your clone:

```bash
uv run drift report                         # Session/weekly relevance and feedback accuracy
uv run drift status <session-id>             # One session's goal and scores
uv run drift backfill --budget-usd 1.0       # Judge past Claude transcripts for a comparison baseline
```

Backfill calls Jev and uses a cost budget; it is not required to use Drift. Reports compare historical judgments with live ones—they are not proof that Drift caused an improvement.

<details>
<summary>Feedback labels and reporting details</summary>

| Label | After a warning | After a quiet turn |
| --- | --- | --- |
| `right` (`correct`, `accurate`, `good`, `yes`) | True positive | True negative |
| `wrong` (`incorrect`, `bad`, `no`) | False positive; resets the affected score | Missed drift |
| `missed` (`miss`) | Kept as a note, since Drift did flag it | Missed drift |
| Any other text | Note | Note |

Feedback attaches to the latest exchange: its most recent warning on your prompt or the assistant's reply, if either warned; otherwise your latest judged prompt. Text after the label word is kept as a note.

Labels are saved in `labels.jsonl` alongside `judgments.jsonl`, linked by judgment ID, session ID, and question-set version. The latest label per judgment counts. `drift report` shows labeled count, warning precision (correct / labeled warnings), and missed drifts.

</details>

## How it works

Drift runs at two hook events: `UserPromptSubmit` for your messages and `Stop` for the assistant's final replies. TypeSafe's Jev model estimates relevance; Drift's code decides whether the resulting score calls for a warning or a block.

A prompt is judged against the goal with the previous reply as context. An assistant reply is judged against both the goal and the latest request, and counts as drift only when it serves neither.

<details>
<summary>Scoring and warning behavior</summary>

Per-turn drift is `P(unrelated) + 0.5 × P(tangential)`. Separate human and assistant tracks use an exponential moving average with weight `0.6` for the latest turn.

With defaults, a fully unrelated turn takes a zero score to `0.6` (warning); a second takes it to `0.84` (blocking threshold). Real judgments are probabilities, so not every off-topic turn produces these exact scores.

Warnings fire once per threshold zone per excursion, rather than repeating on every turn. In opt-in block mode, prompt blocks repeat while the score remains in the blocking zone. See [`drift.py`](src/drift_guard/drift.py) and [`judge.py`](src/drift_guard/judge.py).

</details>

<details>
<summary>Claude Code warning recaps</summary>

After a prompt, Drift can start `drift summarize <session-id>` in the background, once per intent, to generate a two- or three-line recap through Claude Haiku. Until it is ready—or if it fails—the warning uses the goal's opening sentence.

The recap is saved in `recaps/<session-id>.json`. The child `claude` skips user settings, has no tools, persists no session, and runs with `DRIFT_MODE=off`. Recaps are display-only: Jev judges the goal text, not the recap, with long inputs clipped to bounded excerpts. See [`synthesis.py`](src/drift_guard/synthesis.py).

</details>

<details>
<summary>Codex adapter details and troubleshooting</summary>

The adapter uses native `prompt` and `last_assistant_message` fields and deliberately ignores `transcript_path`, whose format is not a stable hook interface. Direct replies are saved as context for later short approvals.

`stop_hook_active` prevents repeat judgment. The adapter skips its own marked continuation prompt and the `[Jev Stop review]` continuation prefix when tracking human drift. Warn mode never requests a continuation. Only the two top-level events are handled—not `SubagentStop` or subagent transcript parsing.

Codex requires every Stop hook to print nothing or a single JSON object. Drift prints nothing on quiet turns and failures. If you see `Hook failed` with `hook returned invalid stop hook JSON output`, check other Stop hooks, including project-level `.codex/hooks.json` files, for plain-text output. Redirect that output to stderr. See [`codex.py`](src/drift_guard/codex.py).

</details>

## Development

```bash
uv sync
uv run pytest
uv run ruff check
uv run mypy src
```

Live integration checks use provider accounts and isolated state:

```bash
scripts/e2e_pipe.sh     # Captured hook payloads, live Jev
scripts/e2e_live.sh     # claude -p session, warning, and feedback with temporary settings
```

## License

[MIT](LICENSE).
