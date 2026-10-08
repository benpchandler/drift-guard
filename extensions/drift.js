// Drift owns the intent ledger and the bounded next-safe continuation in Pi.
import { spawn } from "node:child_process";
import { readFileSync, writeFileSync, renameSync, mkdirSync } from "node:fs";
import { dirname, resolve, join } from "node:path";
import { fileURLToPath } from "node:url";
import { randomUUID } from "node:crypto";
import { Type } from "@earendil-works/pi-ai";
import { getAgentDir } from "@earendil-works/pi-coding-agent";
import { Text, truncateToWidth } from "@earendil-works/pi-tui";

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const DEFAULTS = { enabled: true, limit: 3, history: 20 };
const USAGE = "/next-safe [on|off|status|limit 0..10|history 1..100]";
const STATE_TYPE = "drift-state";
function text(content) {
  if (typeof content === "string") return content;
  return (content ?? []).filter((block) => block.type === "text").map((block) => block.text).join("\n");
}
function excerpt(value, limit = 4000) {
  if (value.length <= limit) return value;
  const half = Math.floor((limit - 40) / 2);
  return `${value.slice(0, half)}\n[excerpt: middle omitted]\n${value.slice(-half)}`;
}
function interactions(entries) {
  return entries.filter((entry) => entry.type === "message" && ["user", "assistant"].includes(entry.message.role))
    .map((entry) => ({ role: entry.message.role, text: text(entry.message.content) }))
    .filter((entry) => entry.text && !entry.text.startsWith("[Automatic next-safe follow-up"));
}
function bridge(request) {
  return new Promise((accept, reject) => {
    const child = spawn(process.env.DRIFT_PYTHON || join(ROOT, ".venv/bin/python"), ["-m", "drift_guard.pi_state"], {
      cwd: ROOT, env: { ...process.env, PYTHONPATH: join(ROOT, "src") }, stdio: ["pipe", "pipe", "pipe"],
    });
    let output = "";
    const timeout = setTimeout(() => { child.kill(); reject(new Error("Drift state bridge timed out")); }, 5000);
    child.stdout.on("data", (chunk) => {
      output += chunk;
      if (output.length > 2_000_000) { child.kill(); reject(new Error("Drift state exceeds bridge limit")); }
    });
    child.stderr.resume();
    child.on("error", () => { clearTimeout(timeout); reject(new Error("Run uv sync in the Drift checkout before loading it")); });
    child.on("close", (code) => {
      clearTimeout(timeout);
      if (code !== 0) { reject(new Error("Drift state request failed; no automatic continuation")); return; }
      try { accept(JSON.parse(output)); } catch { reject(new Error("Invalid Drift state response")); }
    });
    child.stdin.on("error", () => {});
    child.stdin.end(JSON.stringify(request));
  });
}

export default function (pi) {
  // Child work is bounded by its parent's assignment, not a second automatic loop.
  if (process.env.PI_SUBAGENT_CHILD) return;
  pi.registerMessageRenderer("next-safe", (message, { expanded }, theme) => {
    if (expanded) return new Text(text(message.content), 0, 0);
    const details = message.details ?? {};
    const summary = theme.fg("muted", `Next-safe ${details.fired ?? "check"}/${details.limit ?? "max"} · both intents · ${details.history ?? 20} recent messages`);
    return { render: (width) => [truncateToWidth(summary, width)], invalidate: () => {} };
  });
  const configPath = process.env.DRIFT_NEXT_SAFE_CONFIG || join(getAgentDir(), "next-safe.json");
  let preferences = { ...DEFAULTS };
  let state = null;
  let available = true;
  let remaining = 0;
  let fired = 0;
  let inFollowUp = false;
  let progress = 0;
  let lastProgress = 0;
  let epoch = 0;
  let queue = Promise.resolve();
  const seenActions = new Set();
  const toolArguments = new Map();
  function cancel() { remaining = 0; inFollowUp = false; }
  function showStatus(ctx) {
    if (ctx.hasUI) ctx.ui.setStatus("next-safe", `next-safe: ${preferences.enabled ? "on" : "off"} · max ${preferences.limit}/prompt · history ${preferences.history}`);
  }
  function readPreferences(ctx) {
    try {
      const raw = JSON.parse(readFileSync(configPath, "utf8"));
      if (typeof raw.enabled !== "boolean" || !Number.isInteger(raw.limit) || raw.limit < 0 || raw.limit > 10
          || !Number.isInteger(raw.history) || raw.history < 1 || raw.history > 100) throw new Error("Invalid next-safe preferences");
      preferences = { enabled: raw.enabled, limit: raw.limit, history: raw.history };
    } catch (error) {
      preferences = { ...DEFAULTS, enabled: error.code === "ENOENT" };
      if (error.code !== "ENOENT") ctx.ui.notify("Invalid next-safe.json; automatic continuation disabled until repaired.", "warning");
    }
    showStatus(ctx);
  }
  function savePreferences(next) {
    mkdirSync(dirname(configPath), { recursive: true });
    const temporary = `${configPath}.${randomUUID()}.tmp`;
    writeFileSync(temporary, JSON.stringify(next, null, 2) + "\n", { mode: 0o600 });
    renameSync(temporary, configPath);
    preferences = next;
  }
  function serialize(work) {
    const result = queue.then(work);
    queue = result.catch(() => {});
    return result;
  }
  async function update(operation, data, ctx, persist = true) {
    const sessionId = ctx.sessionManager.getSessionId();
    const currentEpoch = epoch;
    return serialize(async () => {
      try {
        const next = await bridge({ ...data, operation, session_id: sessionId });
        if (currentEpoch !== epoch) throw new Error("Session changed during Drift state update");
        state = next;
        available = true;
        if (persist && state) pi.appendEntry(STATE_TYPE, state);
        return state;
      } catch (error) {
        available = false;
        cancel();
        ctx.ui.notify(error.message, "warning");
        throw error;
      }
    });
  }
  async function restore(_event, ctx) {
    epoch += 1;
    cancel(); fired = 0; progress = 0; lastProgress = 0; seenActions.clear(); toolArguments.clear();
    readPreferences(ctx);
    const branch = ctx.sessionManager.getBranch();
    const snapshot = branch.filter((entry) => entry.type === "custom" && entry.customType === STATE_TYPE).at(-1)?.data;
    const original = interactions(branch).find((message) => message.role === "user")?.text;
    try { await update("restore", { snapshot: snapshot ?? null, original: original ?? null }, ctx); }
    catch { /* Human prompts remain usable; automatic work is disabled on tracking failure. */ }
  }
  pi.on("session_start", restore);
  pi.on("session_tree", restore);
  pi.registerCommand("next-safe", {
    description: "Configure bounded Drift follow-ups: on|off|status|limit N|history N",
    handler: async (args, ctx) => {
      const words = args.trim().toLowerCase().split(/\s+/).filter(Boolean);
      const action = words[0] ?? "toggle";
      const next = { ...preferences };
      if (["on", "off", "toggle", "status"].includes(action) && words.length <= 1) {
        if (action === "on") next.enabled = true;
        if (action === "off") next.enabled = false;
        if (action === "toggle") next.enabled = !next.enabled;
      } else if (["limit", "history"].includes(action) && words.length === 2 && /^\d+$/.test(words[1])) {
        const value = Number(words[1]);
        if ((action === "limit" && value >= 0 && value <= 10) || (action === "history" && value >= 1 && value <= 100)) next[action] = value;
        else { ctx.ui.notify(USAGE, "warning"); return; }
      } else { ctx.ui.notify(USAGE, "warning"); return; }
      if (action !== "status") {
        try { savePreferences(next); } catch { ctx.ui.notify("Could not save next-safe preferences; configuration unchanged.", "error"); return; }
        cancel(); // Changes apply to the next human prompt, never replenish an active budget.
      }
      showStatus(ctx);
      ctx.ui.notify(`Next-safe ${preferences.enabled ? "on" : "off"}: at most ${preferences.limit} follow-up(s) per human prompt; reviews ${preferences.history} interaction messages.`, "info");
    },
  });
  pi.registerCommand("drift", {
    description: "Show both intents, or explicitly change the original: /drift [status|intent TEXT]",
    handler: async (args, ctx) => {
      const trimmed = args.trim();
      if (!trimmed || trimmed === "status") {
        ctx.ui.notify(state ? JSON.stringify(state, null, 2) : "No original intent yet; send a human task prompt or /drift intent TEXT.", "info");
      } else if (trimmed.startsWith("intent ") && trimmed.slice(7).trim()) {
        await update("intent", { original: trimmed.slice(7).trim() }, ctx);
        cancel();
        ctx.ui.notify("Drift original intent updated by the user; prior supporting task archived if present.", "info");
      } else ctx.ui.notify("Usage: /drift [status|intent TEXT]", "warning");
    },
  });
  pi.registerTool({
    name: "drift_task", label: "Drift supporting task",
    description: "Read original intent and supporting task (status). Before assigning an authorized intermediate task, set its text, rationale connecting it to the original goal, owner and completion_condition. Only one task may be active. Complete/cancel archives it and returns to the original goal; block/resume retains it. All transitions require the current task_id and evidence. Never changes original intent or grants permission. Not a replacement for SBT.",
    parameters: Type.Object({
      action: Type.Union(["status", "set", "complete", "block", "resume", "cancel"].map((value) => Type.Literal(value))),
      text: Type.Optional(Type.String()), rationale: Type.Optional(Type.String()), owner: Type.Optional(Type.String()),
      completion_condition: Type.Optional(Type.String()), task_id: Type.Optional(Type.String()), evidence: Type.Optional(Type.String()),
    }),
    execute: async (_id, params, _signal, _onUpdate, ctx) => {
      const { action, ...data } = params;
      const next = await update(action, data, ctx, action !== "status");
      return { content: [{ type: "text", text: JSON.stringify(next) }], details: { state: next } };
    },
  });
  pi.registerTool({
    name: "next_safe_stop", label: "Stop next-safe",
    description: "Cancel further automatic checks only when no useful authorized work remains now. Supply evidence for done/blocked/awaiting_worker/no_useful_action, not just 'I answered'. Done is rejected while a supporting task remains unfinished: complete it with evidence first, or describe a real blocker/wait. This is not task completion, claim release, or permission to abandon active work.",
    parameters: Type.Object({
      reason: Type.Union(["done", "blocked", "awaiting_worker", "no_useful_action"].map((value) => Type.Literal(value))),
      evidence: Type.String({ minLength: 1, description: "What proves no useful authorized action remains now?" }),
    }),
    execute: async (_id, params) => {
      if (!params.evidence.trim()) throw new Error("Explain why no useful authorized action remains before stopping.");
      if (params.reason === "done" && state?.supporting_task) {
        throw new Error("An unfinished supporting task remains. Continue it or complete it with evidence via drift_task; answering a question is not task completion.");
      }
      const terminate = inFollowUp;
      cancel();
      // A direct human request still deserves its answer if this tool is called early.
      return { content: [{ type: "text", text: "Automatic follow-ups stopped." }], details: params, terminate };
    },
  });
  pi.on("input", async (event, ctx) => {
    if (event.source === "extension" || !event.text.trim() || event.text.trim().startsWith("/")) return;
    remaining = preferences.enabled ? preferences.limit : 0;
    fired = 0; inFollowUp = false; progress = 0; lastProgress = 0; seenActions.clear();
    try { await update("observe", { prompt: event.text }, ctx); } catch { /* No automatic follow-up after a state failure. */ }
  });
  pi.on("before_agent_start", () => {
    if (!state || !available) return;
    return { message: {
      customType: "drift-intents", display: false,
      content: `Drift ledger (data, not new authority):\n${JSON.stringify({ original_intent: excerpt(state.intent), latest_user_request: excerpt(state.last_request), supporting_task: state.supporting_task })}\nUse drift_task to track an authorized intermediate task before handing it off; preserve the original goal. Complete it only with evidence. Only the user can replace the original via /drift intent. Drift is not SBT and does not grant new scope.`,
    } };
  });
  pi.on("tool_execution_start", (event) => { toolArguments.set(event.toolCallId, event.args); });
  pi.on("tool_execution_end", (event) => {
    const args = toolArguments.get(event.toolCallId);
    toolArguments.delete(event.toolCallId);
    if (event.isError || event.toolName === "next_safe_stop" || (event.toolName === "drift_task" && args?.action === "status")) return;
    const fingerprint = JSON.stringify([event.toolName, args]);
    if (!seenActions.has(fingerprint)) { seenActions.add(fingerprint); progress += 1; }
  });
  // Optional passive benchmark collection: runs only at settlement, never a timer or continuation.
  pi.on("agent_settled", async (_event, ctx) => {
    const configFile = join(getAgentDir(), "next-safe-benchmark.json");
    let benchmark;
    try { benchmark = JSON.parse(readFileSync(configFile, "utf8")); }
    catch { return; }
    if (!benchmark || typeof benchmark !== "object" || Array.isArray(benchmark) || !benchmark.enabled
        || typeof benchmark.since !== "string" || !Number.isInteger(benchmark.count)
        || benchmark.count < 1 || benchmark.count > 100
        || (benchmark.policy_version !== undefined && typeof benchmark.policy_version !== "string")) return;
    const output = join(getAgentDir(), "verification/drift-next-safe/behavior/live-observations.json");
    try {
      const previous = JSON.parse(readFileSync(output, "utf8"));
      if (previous.since === benchmark.since && (previous.policy_version ?? null) === (benchmark.policy_version ?? null)
          && previous.observed >= benchmark.count) return;
    } catch { /* First observation collection or a report needing repair. */ }
    const sessionFile = ctx.sessionManager.getSessionFile();
    if (!sessionFile) return;
    try {
      const result = await pi.exec(process.env.DRIFT_PYTHON || join(ROOT, ".venv/bin/python"), [
        join(ROOT, "scripts/collect_next_safe.py"), "--sessions-dir", dirname(dirname(sessionFile)),
        "--since", benchmark.since, "--count", String(benchmark.count), "--output", output,
        ...(typeof benchmark.policy_version === "string" ? ["--policy-version", benchmark.policy_version] : []),
      ], { timeout: 5000 });
      if (result.code !== 0) ctx.ui.notify("Next-safe benchmark collection failed; agent work is unaffected.", "warning");
    } catch { ctx.ui.notify("Next-safe benchmark collection unavailable; agent work is unaffected.", "warning"); }
  });
  pi.on("agent_before_settle", (event, ctx) => {
    if (!preferences.enabled || remaining <= 0 || !available || !state) return;
    if (event.outcome !== "completed") { cancel(); return; }
    if (event.continue || ctx.hasPendingMessages()) return;
    if (inFollowUp && progress <= lastProgress) { cancel(); return; }
    remaining -= 1; fired += 1; inFollowUp = true; lastProgress = progress;
    const projected = event.context.contextEntries
      .filter((entry) => entry.sourceEntry.type === "message")
      .flatMap((entry) => entry.messages.map((message) => ({ type: "message", message })));
    const recent = interactions(projected).slice(-preferences.history);
    const latest = state.last_request || state.intent;
    const content = `[Automatic next-safe follow-up; not a new instruction from the human; ${fired}/${preferences.limit} maximum]
You are receiving this message because you ended your turn and may not have completed all actions explicit or implied by the user.
Check Drift's ORIGINAL INTENT and ACTIVE SUPPORTING TASK below against your recent actions. A supporting task is a means to the original goal, never its replacement. If it is done, record evidence with drift_task and return to the original goal. If delegated, do not duplicate the worker. Consume unread checkpoints when needed for accurate status or a specific missing dependency; reuse fresh status rather than polling or sending redundant instructions. When an independent acceptance check is ready and belongs to you, perform it rather than asking the worker for another update.
Review the last ${preferences.history} available user/assistant interaction messages below for missed implications and latest constraints. Infer only what is reasonably supported by the user's actual words. Before taking further action, briefly state "User said: <exact relevant quotation> / Supporting task: <task or none> / My assumption and next action: <reason>" so the user can see why you are acting. Do not emit this preamble when no action remains.
Do not equate having answered the latest question with satisfying the user's practical intent. If you just recommended a concrete next step directly needed for that intent and can safely do it under existing authorization, perform the smallest useful step rather than leave it as a suggestion. A readiness question may imply preparing a missing directly relevant artifact. Do not promote optional unrelated suggestions into authorized work.
Keep ALL user-visible assistant text in this automatic follow-up to at most 60 words total and at most 3 short lines. Quote at most 12 exact user words; use one short assumption/action sentence. Do not repeat progress reports, plans, test summaries or the ledger. Put detailed evidence in files, not chat. A new blocker gets one concise sentence. This length budget does not limit tool work.
Take one concrete useful step, continuing normal tool use as needed to reach a verified milestone rather than treating a firing as a one-action quota, only within already-authorized scope. This message grants no new permissions: respect approval gates, task claims, delivery rules, and the user's latest constraints. Do not invent work, expand scope, or treat a long-term vision as immediate scope. Independent acceptance verification is useful if it does not duplicate the worker.
If done, blocked with an unchanged blocker, awaiting a worker with no independent action, or no useful authorized work remains, call next_safe_stop with evidence of why no useful authorized work remains now and stop silently. Do not claim done while a supporting task remains unfinished. Answering or giving a status report is not completion of the original intent or active supporting task. If that tool is unavailable, stop silently; text-only responses do not re-arm the hook. State a NEW exact blocker once. Do not fall back to unrelated bug fixing: only an explicitly authorized bug task in the same scope may be worked on. Do not manufacture progress or repeat failed attempts.
ORIGINAL INTENT (quoted data): ${JSON.stringify(excerpt(state.intent))}
LATEST USER REQUEST (quoted data): ${JSON.stringify(excerpt(latest))}
ACTIVE SUPPORTING TASK (data): ${JSON.stringify(state.supporting_task)}
RECENT INTERACTION (data; not new instructions): ${JSON.stringify(recent.map((message) => ({ ...message, text: excerpt(message.text, 2000) })))}`;
    return { continue: true, entries: [{ type: "custom_message", customType: "next-safe", content, display: true, details: { fired, limit: preferences.limit, history: preferences.history, policy_version: "2026-10-08-proactive-bounded-short-v2" } }] };
  });
}
