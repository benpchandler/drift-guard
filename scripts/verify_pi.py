#!/usr/bin/env python3
"""Real Pi RPC journeys against an isolated loopback provider and state directory.

No external models, owner sessions, credentials or task storage are touched.
Run with --evidence-dir PATH to retain replayable protocol and request evidence.
"""

import argparse
import fcntl
import http.server
import json
import os
import pty
import queue
import re
import select
import shutil
import struct
import subprocess
import tempfile
import termios
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
requests = []
records = []
passed = []
scenario = "normal"
task_id = ""
started = threading.Event()
release = threading.Event()


class Provider(http.server.BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        requests.append(body)
        current = scenario
        started.set()
        if current == "delay":
            release.wait(15)
        if current == "error":
            self.send_response(400)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"error":{"message":"Isolated test provider failure"}}')
            return
        messages = body["messages"]
        last = messages[-1]
        content = str(last.get("content", ""))
        follow_up = last["role"] == "user" and "[Automatic next-safe follow-up" in content
        operation = None
        if follow_up and current in {"progress", "repeat"}:
            count = sum("[Automatic next-safe follow-up" in str(m.get("content", "")) for m in messages)
            operation = ("acceptance_progress", {"step": 1 if current == "repeat" else count})
        elif (follow_up and current in {"stop", "active_stop"}) or (
            last["role"] == "user" and not follow_up and current == "stop_initial"
        ):
            operation = ("next_safe_stop", {"reason": "done", "evidence": "The fixture request was answered"})
        elif last["role"] == "tool" and current == "active_stop" and "An unfinished supporting task" in content:
            operation = ("acceptance_progress", {"step": 1})
        elif last["role"] == "tool" and current == "active_stop" and "Verified step 1" in content:
            operation = ("drift_task", {"action": "complete", "task_id": task_id, "evidence": "Fixture verification passed"})
        elif last["role"] == "user" and not follow_up and current == "set":
            operation = (
                "drift_task",
                {
                    "action": "set",
                    "text": "Fix depreciation rounding",
                    "rationale": "Needed for workbook validation",
                    "owner": "worker-123",
                    "completion_condition": "Focused rounding and render controls pass",
                },
            )
        elif last["role"] == "user" and not follow_up and current == "complete":
            operation = (
                "drift_task",
                {"action": "complete", "task_id": task_id, "evidence": "Focused rounding and render controls passed"},
            )
        elif last["role"] == "user" and not follow_up and current == "bad_task":
            operation = ("drift_task", {"action": "complete", "task_id": "stale", "evidence": "not valid"})
        delta = {"role": "assistant", "content": "Completed the bounded acceptance request."}
        finish = "stop"
        if operation:
            name, arguments = operation
            delta = {
                "role": "assistant",
                "content": (
                    "User said: publish the workbook. Supporting task: rounding. "
                    "My assumption and next action: verify the authorized step."
                ),
                "tool_calls": [
                    {
                        "index": 0,
                        "id": f"test-{len(requests)}",
                        "type": "function",
                        "function": {"name": name, "arguments": json.dumps(arguments)},
                    }
                ],
            }
            finish = "tool_calls"
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        chunks = [
            {"id": "test", "object": "chat.completion.chunk", "choices": [{"index": 0, "delta": delta, "finish_reason": None}]},
            {
                "id": "test",
                "object": "chat.completion.chunk",
                "choices": [{"index": 0, "delta": {}, "finish_reason": finish}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 12, "total_tokens": 112},
            },
        ]
        try:
            for chunk in chunks:
                self.wfile.write(("data: " + json.dumps(chunk) + "\n\n").encode())
            self.wfile.write(b"data: [DONE]\n\n")
        except (BrokenPipeError, ConnectionResetError):
            pass


class Client:
    def __init__(self, agent, directory, extra=()):
        env = {key: value for key, value in os.environ.items() if not key.startswith(("PI_", "DRIFT_"))}
        env.update(PI_CODING_AGENT_DIR=str(agent), PI_OFFLINE="1", DRIFT_PI_STATE_DIR=str(directory / "drift-state"))
        self.environment = env
        self.process = subprocess.Popen(
            [
                shutil.which("pi"),
                "--mode",
                "rpc",
                "--session-dir",
                str(directory / "sessions"),
                "--no-skills",
                "--no-context-files",
                "--no-prompt-templates",
                "--provider",
                "acceptance",
                "--model",
                "deterministic",
                "--tools",
                "drift_task,next_safe_stop,acceptance_progress",
                "--thinking",
                "off",
                *extra,
            ],
            cwd=directory,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.inbox = queue.Queue()
        self.serial = 0
        threading.Thread(target=self.read, daemon=True).start()

    def read(self):
        for line in self.process.stdout:
            record = json.loads(line)
            records.append(record)
            self.inbox.put(record)

    def send(self, kind, **fields):
        self.serial += 1
        request_id = str(self.serial)
        self.process.stdin.write((json.dumps({"id": request_id, "type": kind, **fields}) + "\n").encode())
        self.process.stdin.flush()
        return request_id

    def wait(self, predicate):
        deadline = time.monotonic() + 40
        while time.monotonic() < deadline:
            record = self.inbox.get(timeout=max(0.1, deadline - time.monotonic()))
            if predicate(record):
                return record
        raise AssertionError("RPC deadline exceeded")

    def command(self, kind, **fields):
        request_id = self.send(kind, **fields)
        response = self.wait(lambda item: item.get("id") == request_id and item.get("type") == "response")
        assert response["success"], response
        return response.get("data", {})

    def prompt(self, value, expected_calls=None):
        before = len(requests)
        self.send("prompt", message=value)
        self.wait(lambda item: item.get("type") == "agent_settled")
        if expected_calls is not None:
            assert len(requests) - before == expected_calls, (value, len(requests) - before, expected_calls)
        assert not self.command("get_state")["isStreaming"]
        return requests[before:]

    def entries(self):
        return self.command("get_entries")["entries"]

    def state(self):
        return [e["data"] for e in self.entries() if e.get("customType") == "drift-state"][-1]

    def close(self):
        self.process.stdin.close()
        self.process.wait(timeout=15)
        return self.process.stderr.read().decode()


def follow_ups(calls):
    messages = []
    for call in calls:
        content = call["messages"][-1].get("content", "")
        value = content if isinstance(content, str) else "\n".join(b.get("text", "") for b in content)
        if "[Automatic next-safe follow-up" in value:
            messages.append(value)
    return messages


def verify_native_tui(client, evidence_dir):
    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 80, 0, 0))
    arguments = list(client.process.args)
    arguments.remove("--mode")
    arguments.remove("rpc")
    if "--session" in arguments:
        index = arguments.index("--session")
        del arguments[index : index + 2]
    environment = dict(client.environment, TERM="xterm-256color", COLUMNS="80", LINES="24")
    process = subprocess.Popen(
        arguments,
        env=environment,
        stdin=slave,
        stdout=slave,
        stderr=slave,
        cwd=Path(client.environment["DRIFT_PI_STATE_DIR"]).parent,
        start_new_session=True,
    )
    os.close(slave)
    captured = bytearray()
    ansi = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07]*(?:\x07|\x1b\\)")

    def until(marker):
        start = len(captured)
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if select.select([master], [], [], 0.1)[0]:
                captured.extend(os.read(master, 65536))
                plain = ansi.sub("", captured[start:].decode(errors="replace"))
                if marker in plain:
                    return plain
        raise AssertionError(f"Native TUI did not show {marker!r}")

    try:
        until("[Extensions]")
        os.write(master, b"Native TUI fixture request")
        until("Native TUI fixture request")
        os.write(master, b"\r")
        collapsed = until("Next-safe 1/3")
        assert "ORIGINAL INTENT" not in collapsed
        os.write(master, b"\x0f")
        assert "ORIGINAL INTENT" in until("ORIGINAL INTENT")
        passed.append("native TUI shows compact one-line hook; keyboard expansion reveals original full context")
    finally:
        if evidence_dir:
            evidence_dir.mkdir(parents=True, exist_ok=True)
            (evidence_dir / "tui-evidence.txt").write_bytes(captured)
        os.close(master)
        process.kill()
        process.wait(timeout=5)


def main():  # noqa: PLR0915 - one ordered end-to-end user journey, sharing the same RPC session
    global scenario, task_id  # noqa: PLW0603 - the loopback fixture reads the scripted scenario
    parser = argparse.ArgumentParser()
    parser.add_argument("--evidence-dir", type=Path)
    parser.add_argument("--legacy-extension", type=Path, help="Also load a retired standalone hook to check migration")
    args = parser.parse_args()
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Provider)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    stderr = ""
    with tempfile.TemporaryDirectory(prefix="drift-pi-acceptance-") as folder:
        directory = Path(folder)
        agent = directory / "agent"
        agent.mkdir()
        extensions = agent / "extensions"
        extensions.mkdir()
        if args.legacy_extension:
            (extensions / "legacy-next-safe.js").write_text(args.legacy_extension.read_text())
        (extensions / "probe.js").write_text("""import { Type } from "@earendil-works/pi-ai";
export default function(pi) {
  pi.registerCommand("test-generated", {
    description: "Fixture extension input", handler: async () => pi.sendUserMessage("Extension-only fixture prompt")
  });
  pi.registerCommand("test-reload", {
    description: "Fixture actual runtime reload", handler: async (_args, ctx) => ctx.reload()
  });
  pi.registerCommand("test-tree", {
    description: "Fixture tree navigation", handler: async (id, ctx) => ctx.navigateTree(id.trim(), {summarize: false})
  });
  pi.registerTool({name: "acceptance_progress", label: "Acceptance progress", description: "Isolated verification probe",
    parameters: Type.Object({step: Type.Number()}),
    execute: async (_id, args) => ({content: [{type: "text", text: `Verified step ${args.step}`}], details: args})
  });
}""")
        (agent / "models.json").write_text(
            json.dumps(
                {
                    "providers": {
                        "acceptance": {
                            "baseUrl": f"http://127.0.0.1:{server.server_port}/v1",
                            "api": "openai-completions",
                            "apiKey": "local-test",
                            "models": [
                                {
                                    "id": "deterministic",
                                    "reasoning": False,
                                    "input": ["text"],
                                    "contextWindow": 100000,
                                    "maxTokens": 1024,
                                }
                            ],
                        }
                    }
                }
            )
        )
        (agent / "settings.json").write_text(
            json.dumps(
                {
                    "packages": [str(ROOT)],
                    "cacheWarming": "off",
                    "retry": {"enabled": False},
                    "compaction": {"keepRecentTokens": 200, "reserveTokens": 1000},
                }
            )
        )
        (agent / "next-safe-benchmark.json").write_text(
            json.dumps(
                {
                    "enabled": True,
                    "since": "2026-01-01T00:00:00Z",
                    "count": 2,
                    "policy_version": "2026-10-08-proactive-bounded-short-v2",
                }
            )
        )
        client = Client(agent, directory)
        try:
            commands = [c["name"] for c in client.command("get_commands")["commands"]]
            assert commands.count("next-safe") == 1 and "drift" in commands
            calls = client.prompt("Regenerate and publish the corrected workbook", 2)
            original = client.state()["intent"]
            assert original == "Regenerate and publish the corrected workbook"
            assert "ORIGINAL INTENT" in follow_ups(calls)[0]
            assert "1/3 maximum" in follow_ups(calls)[0]
            assert "User said:" in follow_ups(calls)[0]
            assert "RECENT INTERACTION" in follow_ups(calls)[0]
            passed.append("package discovery, one hook, default on, original intent and visible-assumption instruction delivered")

            before = len(requests)
            client.command("prompt", message="/next-safe status")
            assert len(requests) == before
            client.command("prompt", message="/next-safe limit 999")
            client.prompt("What is my status?", 2)
            assert client.state()["intent"] == original
            passed.append("status and invalid configuration do not re-arm; later human request preserves original goal")
            observations = json.loads((agent / "verification/drift-next-safe/behavior/live-observations.json").read_text())
            assert observations["observed"] == 2 and observations["remaining_future_firings"] == 0
            assert observations["policy_version"] == "2026-10-08-proactive-bounded-short-v2"
            assert all(row["behavioral_review"] == "pending_agent_review" for row in observations["observations"])
            passed.append("passive settlement collector records actual bounded firings without inventing behavioral pass labels")
            (agent / "next-safe-benchmark.json").write_text("null")
            client.prompt("Malformed optional collector must not interrupt agent work", 2)
            passed.append("malformed optional benchmark configuration does not interrupt normal work")

            scenario = "set"
            calls = client.prompt("Fix the rounding so publication can proceed", 3)
            state = client.state()
            task_id = state["supporting_task"]["id"]
            assert state["intent"] == original and state["supporting_task"]["owner"] == "worker-123"
            assert "Fix depreciation rounding" in follow_ups(calls)[0]
            passed.append("real model tool call persists supporting task and supplies both intents to hook")

            scenario = "normal"
            client.command("prompt", message="/test-reload")
            client.prompt("Continue the supporting task", 2)
            assert client.state()["supporting_task"]["id"] == task_id
            passed.append("reload preserves original and active supporting task without retroactive continuation")

            # Select the actual branch snapshots, not the newest append-only entry.
            state_entries = [e for e in client.entries() if e.get("customType") == "drift-state"]
            before_task = [e for e in state_entries if e["data"]["supporting_task"] is None][-1]["id"]
            active_task = state_entries[-1]["id"]
            session_before_fork = client.command("get_state")["sessionFile"]
            client.command("prompt", message=f"/test-tree {before_task}")
            assert client.state()["supporting_task"] is None
            client.command("prompt", message=f"/test-tree {active_task}")
            assert client.state()["supporting_task"]["id"] == task_id
            client.command("clone")
            client.prompt("Continue the chosen branch in this clone", 2)
            assert client.state()["supporting_task"]["id"] == task_id
            client.command("switch_session", sessionPath=session_before_fork)
            assert client.state()["supporting_task"]["id"] == task_id
            passed.append("tree navigation removes abandoned task state; clone and switch inherit only selected branch")
            client.command("compact")
            client.command("prompt", message="/test-reload")
            client.prompt("Continue after compacting", 2)
            assert client.state()["intent"] == original and client.state()["supporting_task"]["id"] == task_id
            passed.append("real compaction and reload preserve both intent slots")

            scenario = "complete"
            calls = client.prompt("Validation has passed; return to publication", 3)
            state = client.state()
            assert state["intent"] == original and state["supporting_task"] is None
            assert state["supporting_task_history"][-1]["status"] == "done"
            assert "ACTIVE SUPPORTING TASK (data): null" in follow_ups(calls)[0]
            passed.append("completion requires matching task id/evidence, archives history and returns hook to original")
            scenario = "bad_task"
            client.prompt("An invalid task transition must not restart work", 2)
            scenario = "normal"
            client.prompt("Recover from invalid task transition", 2)
            assert client.state()["intent"] == original and client.state()["supporting_task"] is None
            passed.append("invalid task transition preserves state and suppresses automatic work until next human input")

            scenario = "normal"
            client.command("prompt", message="/next-safe history 3")
            calls = client.prompt("Check the last interaction", 2)
            recent = json.loads(follow_ups(calls)[0].split("RECENT INTERACTION (data; not new instructions): ", 1)[1])
            assert 1 <= len(recent) <= 3 and recent[-1]["role"] == "assistant"
            passed.append("configurable recent-message window excludes tools and automatic prompts")

            client.command("prompt", message="/next-safe limit 3")
            scenario = "progress"
            calls = client.prompt("Perform three bounded verification steps", 7)
            assert len(follow_ups(calls)) == 3
            passed.append("three successful distinct tool-action follow-ups stop at configured ceiling")
            scenario = "normal"
            assert len(follow_ups(client.prompt("Answer without action", 2))) == 1
            passed.append("text-only continuation stops early rather than wasting remaining budget")
            scenario = "repeat"
            calls = client.prompt("Do not keep repeating the same verification", 5)
            assert len(follow_ups(calls)) == 2
            passed.append("identical successful tool polls do not keep the continuation loop alive")
            scenario = "stop"
            client.prompt("The work is already done", 2)
            passed.append("explicit next_safe_stop terminates without repeating an assistant summary")
            scenario = "stop_initial"
            client.prompt("A direct human question still needs an answer", 2)
            assert requests[-1]["messages"][-1]["role"] == "tool"
            passed.append("early stop tool cancels automation but preserves the direct human answer")

            scenario = "normal"
            client.command("prompt", message="/next-safe off")
            client.prompt("Disabled journey", 1)
            client.command("prompt", message="/test-reload")
            client.prompt("Disabled after reload", 1)
            client.command("new_session")
            client.prompt("Disabled in a new session", 1)
            passed.append("explicit off persists through reload and new sessions")
            client.command("prompt", message="/next-safe on")
            client.command("prompt", message="/next-safe limit 0")
            client.prompt("Zero limit journey", 1)
            client.command("prompt", message="/next-safe limit 1")
            client.prompt("Re-enabled journey", 2)
            client.prompt("/test-generated", 1)
            passed.append("zero limit, explicit on and extension-origin non-rearming")

            scenario = "error"
            client.prompt("Provider failure journey", 1)
            scenario = "normal"
            client.prompt("Recovery after failure", 2)
            passed.append("provider error consumes budget; next human request recovers")

            started.clear()
            release.clear()
            scenario = "delay"
            before = len(requests)
            client.send("prompt", message="Abort this journey")
            assert started.wait(10)
            client.command("clear_queue")
            client.command("abort")
            release.set()
            scenario = "normal"
            assert len(requests) - before == 1
            client.prompt("Recover after abort", 2)
            passed.append("abort never restarts; new human prompt recovers")

            started.clear()
            release.clear()
            scenario = "delay"
            before = len(requests)
            client.send("prompt", message="Disable during a run")
            assert started.wait(10)
            client.command("prompt", message="/next-safe off")
            scenario = "normal"
            release.set()
            client.wait(lambda r: r.get("type") == "agent_settled")
            assert len(requests) - before == 1
            passed.append("off cancels remaining follow-ups during a running request")

            client.command("prompt", message="/next-safe on")
            started.clear()
            release.clear()
            scenario = "delay"
            before = len(requests)
            client.send("prompt", message="Queued human-input journey")
            assert started.wait(10)
            client.command("prompt", message="Human follow-up takes priority", streamingBehavior="followUp")
            scenario = "normal"
            release.set()
            client.wait(lambda r: r.get("type") == "agent_settled")
            assert len(requests) - before == 3
            assert "Human follow-up takes priority" in str(requests[-2]["messages"][-1])
            assert "Human follow-up takes priority" in follow_ups(requests[before:])[0]
            passed.append("queued human input wins and hook uses latest user constraint")

            client.command("prompt", message="/drift intent A deliberately changed goal")
            client.prompt("Continue that new goal", 2)
            assert client.state()["intent"] == "A deliberately changed goal"
            session_file = client.command("get_state")["sessionFile"]
            stderr += client.close()
            client = Client(agent, directory, ["--session", session_file])
            client.prompt("Resume with the same goal", 2)
            assert client.state()["intent"] == "A deliberately changed goal"
            passed.append("explicit user intent replacement and process restart/resume persistence")
            client.command("prompt", message="/next-safe limit 3")
            scenario = "set"
            client.prompt("Track the fixture supporting work", 3)
            task_id = client.state()["supporting_task"]["id"]
            scenario = "active_stop"
            client.prompt("Do not abandon unfinished supporting work after answering", 6)
            assert client.state()["supporting_task"] is None
            assert client.state()["supporting_task_history"][-1]["evidence"] == "Fixture verification passed"
            passed.append("done stop rejected for active task; tool work and evidence-backed completion proceed before stopping")
            scenario = "normal"
            (agent / "next-safe.json").write_text('{"enabled": true, "limit": 999, "history": 20}')
            client.command("prompt", message="/test-reload")
            client.prompt("Malformed preferences must fail safe", 1)
            client.command("prompt", message="/next-safe on")
            client.prompt("Valid preferences recover", 2)
            passed.append("malformed preferences disable automatic work; explicit valid configuration repairs them")
            verify_native_tui(client, args.evidence_dir)
            assert not any(r.get("type") == "extension_error" for r in records)
        finally:
            release.set()
            stderr += client.close()
            server.shutdown()
            if args.evidence_dir:
                args.evidence_dir.mkdir(parents=True, exist_ok=True)
                (args.evidence_dir / "rpc-evidence.jsonl").write_text("".join(json.dumps(r) + "\n" for r in records))
                (args.evidence_dir / "provider-evidence.json").write_text(json.dumps(requests, indent=2))
                (args.evidence_dir / "stderr.txt").write_text(stderr)
                (args.evidence_dir / "summary.json").write_text(
                    json.dumps(
                        {
                            "passed": passed,
                            "package": str(ROOT),
                            "provider": "hermetic deterministic loopback; no external model judgment tested",
                        },
                        indent=2,
                    )
                )
    print("PASS: " + "; ".join(passed))


if __name__ == "__main__":
    main()
