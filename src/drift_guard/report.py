"""Summarize the judgment log: drift per session, and adherence before (backfill) vs after (live) the guard."""

import json
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime
from statistics import mean
from typing import Any

from drift_guard.feedback import Outcome
from drift_guard.judge import PRICE_PER_INPUT_TOKEN_USD, QUESTION_SET_VERSION
from drift_guard.store import Store

OFF_INTENT = 0.5  # a turn whose own drift is at least this counts as off-intent
MAX_LOG_LINES = 1_000_000
MAX_SESSION_ROWS = 15


@dataclass(frozen=True)
class Summary:
    source: str
    event: str
    sessions: int
    turns: int
    mean_relevance: float
    off_intent_rate: float
    sessions_warned: int
    warnings: int
    blocks: int
    errors: int
    p95_latency_ms: int | None
    cost_usd: float


def _load(store: Store) -> list[dict[str, Any]]:
    """Events asked with the current question wording, plus unjudged events (errors, escapes) of any version."""
    if not store.log_path.exists():
        return []
    rows: list[dict[str, Any]] = []
    with store.log_path.open() as handle:
        for index, line in enumerate(handle):
            if index >= MAX_LOG_LINES:
                break
            row = json.loads(line)
            if row.get("relevance") is None or row.get("questions_version", 1) == QUESTION_SET_VERSION:
                rows.append(row)
    return rows


def _p95(values: list[int]) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(0.95 * len(ordered)))]


def summarize(rows: list[dict[str, Any]], source: str, event: str) -> Summary:
    subset = [r for r in rows if r.get("source", "live") == source and r["event"] == event]
    judged = [r for r in subset if r.get("relevance") is not None]
    warned = {r["session_id"] for r in judged if r["action"] in {"warn", "block"}}
    return Summary(
        source=source,
        event=event,
        sessions=len({r["session_id"] for r in judged}),
        turns=len(judged),
        mean_relevance=round(mean(r["relevance"] for r in judged), 3) if judged else 0.0,
        off_intent_rate=round(sum(r["turn_drift"] >= OFF_INTENT for r in judged) / len(judged), 3) if judged else 0.0,
        sessions_warned=len(warned),
        warnings=sum(r["action"] == "warn" for r in judged),
        blocks=sum(r["action"] == "block" for r in judged),
        errors=sum(r["action"] == "error" for r in subset),
        p95_latency_ms=_p95([r["latency_ms"] for r in judged if r.get("latency_ms") is not None]),
        cost_usd=round(sum(r.get("input_tokens", 0) for r in subset) * PRICE_PER_INPUT_TOKEN_USD, 5),
    )


def per_session(rows: list[dict[str, Any]], source: str) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        if r.get("source", "live") == source and r.get("relevance") is not None:
            grouped[r["session_id"]].append(r)
    table = []
    for sid, rs in grouped.items():
        human = [r for r in rs if r["event"] == "human"]
        agent = [r for r in rs if r["event"] == "agent"]
        table.append(
            {
                "session": sid[:8],
                "project": rs[0].get("project", ""),
                "human_turns": len(human),
                "human_relevance": round(mean(r["relevance"] for r in human), 2) if human else None,
                "max_drift": round(max(r["drift_score"] for r in human), 2) if human else None,
                "warns": sum(r["action"] in {"warn", "block"} for r in human),
                "agent_relevance": round(mean(r["relevance"] for r in agent), 2) if agent else None,
                "last": max(r["ts"] for r in rs),
            }
        )
    return sorted(table, key=lambda t: t["last"], reverse=True)


def weekly(rows: list[dict[str, Any]]) -> list[tuple[str, str, float, int]]:
    """Mean human relevance per ISO week and source: adherence over time."""
    buckets: dict[tuple[str, str], list[float]] = defaultdict(list)
    for r in rows:
        if r["event"] == "human" and r.get("relevance") is not None:
            year, week, _ = datetime.fromisoformat(r["ts"]).isocalendar()
            buckets[(f"{year}-W{week:02d}", r.get("source", "live"))].append(r["relevance"])
    return [(wk, src, round(mean(v), 3), len(v)) for (wk, src), v in sorted(buckets.items())]


@dataclass(frozen=True)
class Accuracy:
    """Ben's "drift: feedback" labels. The latest label per judgment wins; notes and unattached labels count as given."""

    labels: int
    true_positive: int
    false_positive: int
    true_negative: int
    missed_drift: int
    notes: int
    warning_precision: float | None  # right warnings / labeled warnings


def _load_labels(store: Store) -> list[dict[str, Any]]:
    if not store.labels_path.exists():
        return []
    latest: dict[str, dict[str, Any]] = {}
    with store.labels_path.open() as handle:
        for index, line in enumerate(handle):
            if index >= MAX_LOG_LINES:
                break
            row = json.loads(line)
            if row.get("questions_version") == QUESTION_SET_VERSION:
                latest[row["judgment_id"] or row["id"]] = row
    return list(latest.values())


def accuracy(labels: list[dict[str, Any]]) -> Accuracy:
    counts = {outcome.value: sum(r["outcome"] == outcome.value for r in labels) for outcome in Outcome}
    tp, fp = counts[Outcome.TRUE_POSITIVE.value], counts[Outcome.FALSE_POSITIVE.value]
    return Accuracy(
        labels=len(labels),
        true_positive=tp,
        false_positive=fp,
        true_negative=counts[Outcome.TRUE_NEGATIVE.value],
        missed_drift=counts[Outcome.MISSED_DRIFT.value],
        notes=counts[Outcome.NOTE.value],
        warning_precision=round(tp / (tp + fp), 3) if tp + fp else None,
    )


def _accuracy_lines(acc: Accuracy) -> list[str]:
    precision = "n/a" if acc.warning_precision is None else f"{acc.warning_precision:.2f}"
    return [
        "",
        f"Accuracy from 'drift: feedback' labels ({acc.labels} labeled)",
        f"  warnings: {acc.true_positive} right, {acc.false_positive} wrong -> precision {precision}",
        f"  quiet turns: {acc.true_negative} right, {acc.missed_drift} missed drifts",
        f"  notes: {acc.notes}",
    ]


def render(store: Store, as_json: bool = False) -> str:
    rows = _load(store)
    summaries = [summarize(rows, s, e) for s in ("backfill", "live") for e in ("human", "agent")]
    sessions = {s: per_session(rows, s)[:MAX_SESSION_ROWS] for s in ("live", "backfill")}
    acc = accuracy(_load_labels(store))
    if as_json:
        payload = {
            "summaries": [asdict(s) for s in summaries],
            "sessions": sessions,
            "weekly": weekly(rows),
            "accuracy": asdict(acc),
        }
        return json.dumps(payload, indent=2)
    lines = [f"Adherence: backfill = pre-guard baseline, live = guard on (question set v{QUESTION_SET_VERSION})", ""]
    lines.append(
        f"{'source':9} {'event':6} {'sess':>5} {'turns':>6} {'relev':>6} {'off%':>6} "
        f"{'warned':>7} {'warns':>6} {'blocks':>6} {'errs':>5} {'p95ms':>6} {'cost$':>8}"
    )
    for s in summaries:
        lines.append(
            f"{s.source:9} {s.event:6} {s.sessions:>5} {s.turns:>6} {s.mean_relevance:>6.3f} {s.off_intent_rate * 100:>5.1f}% "
            f"{s.sessions_warned:>7} {s.warnings:>6} {s.blocks:>6} {s.errors:>5} {s.p95_latency_ms or '-':>6} {s.cost_usd:>8.4f}"
        )
    lines += _accuracy_lines(acc)
    for source, table in sessions.items():
        lines += ["", f"Recent {source} sessions (human relevance, peak drift score, warnings, agent relevance)"]
        lines += [
            f"  {t['session']} {t['project'][:28]:28} turns={t['human_turns']:<3} relev={t['human_relevance']} "
            f"peak={t['max_drift']} warns={t['warns']} agent={t['agent_relevance']}"
            for t in table
        ]
    lines += ["", "Weekly human relevance"] + [f"  {wk} {src:8} {m:.3f} (n={n})" for wk, src, m, n in weekly(rows)]
    return "\n".join(lines)
