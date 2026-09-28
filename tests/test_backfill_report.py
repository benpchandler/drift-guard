import json
from pathlib import Path

from conftest import OFF, ON

from drift_guard import backfill, report
from drift_guard.config import Config
from drift_guard.store import Event, Store


def test_session_jobs_and_replay(tmp_path: Path) -> None:
    entries = [
        {"type": "user", "timestamp": "2026-08-01T10:00:00.123Z", "message": {"content": "Fix the login test"}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "Done"}]}},
        {"type": "user", "message": {"content": "banana bread?"}},
        {"type": "user", "message": {"content": "flights to Lisbon?"}},
    ]
    path = tmp_path / "abc.jsonl"
    path.write_text("\n".join(json.dumps(e) for e in entries))
    jobs = backfill.session_jobs(path, max_turns=25)
    assert [(j.index, j.kind) for j in jobs] == [(0, "agent"), (1, "human"), (2, "human")]
    assert all(j.state["intent"] == "Fix the login test" for j in jobs)
    answers = {"agent": {"serves_intent": ON, "serves_request": ON}, "human": {"serves_intent": OFF}}
    results = [(j, answers[j.kind], 10) for j in reversed(jobs)]
    events = backfill.replay(results, Config(state_dir=tmp_path))
    human = [e for e in events if e.event == "human"]
    assert [e.action for e in human] == ["warn", "warn"] and human[1].drift_score == 0.84
    assert all(e.source == "backfill" for e in events)
    assert next(e for e in events if e.event == "agent").ts == "2026-08-01T10:00:00+00:00"


def test_too_short_sessions_are_skipped(tmp_path: Path) -> None:
    path = tmp_path / "s.jsonl"
    path.write_text(json.dumps({"type": "user", "message": {"content": "only one"}}))
    assert backfill.session_jobs(path, 25) == []


def test_report_separates_baseline_from_live(tmp_path: Path) -> None:
    store = Store(tmp_path)
    for source, rel in (("backfill", 0.4), ("backfill", 0.6), ("live", 0.9)):
        store.append(
            Event(
                "s1" if source == "backfill" else "s2",
                "human",
                "none",
                source=source,
                relevance=rel,
                turn_drift=1 - rel,
                drift_score=0.1,
                input_tokens=1000,
                latency_ms=200,
            )
        )
    store.append(Event("s2", "human", "error", error="timeout"))
    rows = report._load(store)
    base, live = report.summarize(rows, "backfill", "human"), report.summarize(rows, "live", "human")
    assert (base.turns, base.mean_relevance, base.off_intent_rate) == (2, 0.5, 0.5)
    assert (live.turns, live.errors, live.p95_latency_ms) == (1, 1, 200)
    text = report.render(store)
    assert "backfill" in text and "live" in text
    assert json.loads(report.render(store, as_json=True))["summaries"][0]["source"] == "backfill"


def test_report_ignores_judgments_from_older_question_wording(tmp_path: Path) -> None:
    store = Store(tmp_path)
    store.append(
        Event("old", "human", "warn", source="backfill", relevance=0.0, turn_drift=1.0, drift_score=0.6, questions_version=1)
    )
    store.append(Event("new", "human", "none", source="backfill", relevance=1.0, turn_drift=0.0, drift_score=0.0))
    summary = report.summarize(report._load(store), "backfill", "human")
    assert (summary.turns, summary.mean_relevance) == (1, 1.0)


def test_report_accuracy_from_labels(tmp_path: Path) -> None:
    from drift_guard.store import LabelRecord

    store = Store(tmp_path)
    for judgment, result in (
        ("j1", "true_positive"),
        ("j2", "false_positive"),
        ("j2", "true_positive"),
        ("j3", "false_positive"),
        ("j4", "missed_drift"),
        ("", "note"),
    ):
        store.append_label(LabelRecord("s", "x", result, "", judgment_id=judgment))
    store.append_label(LabelRecord("s", "x", "false_positive", "", judgment_id="old", questions_version=1))
    acc = report.accuracy(report._load_labels(store))
    assert (acc.labels, acc.true_positive, acc.false_positive, acc.missed_drift, acc.notes) == (5, 2, 1, 1, 1)
    assert acc.warning_precision == 0.667
    assert "precision 0.67" in report.render(store)
    assert json.loads(report.render(store, as_json=True))["accuracy"]["missed_drift"] == 1
