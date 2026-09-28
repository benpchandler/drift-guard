"""Drift scoring: the pure policy that turns Jev's per-turn relevance into a running drift score and an action.

Jev supplies one judgment per turn: a probability distribution over four relevance levels. Everything here is
policy, so thresholds and weights can change without re-asking Jev (the raw distribution is logged).
"""

from dataclasses import dataclass, replace
from enum import StrEnum

# Relevance levels, lowest first. The order is the Score scale Jev answers on (index 0..3).
LEVELS: tuple[str, ...] = ("unrelated", "tangential", "supporting", "direct")

# How much each level counts as drift for a single turn. A tangent is half a drift: one side question is normal,
# a run of them is the pattern the guard exists to catch.
LEVEL_DRIFT: tuple[float, ...] = (1.0, 0.5, 0.0, 0.0)


class Action(StrEnum):
    NONE = "none"
    WARN = "warn"
    BLOCK = "block"


@dataclass(frozen=True)
class Thresholds:
    """EMA weight of the newest turn, and the running-score levels at which the guard warns and blocks."""

    alpha: float = 0.6
    warn: float = 0.5
    block: float = 0.8

    def __post_init__(self) -> None:
        if not 0.0 < self.alpha <= 1.0:
            raise ValueError(f"alpha must be in (0, 1], got {self.alpha}")
        if not 0.0 < self.warn < self.block <= 1.0:
            raise ValueError(f"need 0 < warn < block <= 1, got warn={self.warn} block={self.block}")


@dataclass(frozen=True)
class DriftTrack:
    """One running drift score (the human's prompts, or the agent's replies) within a session."""

    score: float = 0.0
    turns: int = 0
    warnings: int = 0
    blocks: int = 0
    alerted: int = 0  # highest zone already alerted in the current excursion above the warn line (0, 1 or 2)

    def __post_init__(self) -> None:
        assert 0.0 <= self.score <= 1.0, f"drift score out of range: {self.score}"
        assert min(self.turns, self.warnings, self.blocks) >= 0, "counters are never negative"
        assert self.alerted in {0, 1, 2}, f"alerted zone out of range: {self.alerted}"


def turn_drift(probabilities: tuple[float, ...]) -> float:
    """Expected drift of one turn: P(unrelated) + 0.5 * P(tangential). Uses probabilities, never score interpolation."""
    assert len(probabilities) == len(LEVELS), f"expected {len(LEVELS)} level probabilities, got {len(probabilities)}"
    assert all(p >= 0.0 for p in probabilities), "probabilities are non-negative"
    total = sum(probabilities)
    if total <= 0.0:
        raise ValueError("probabilities sum to zero")
    drift = sum(p * w for p, w in zip(probabilities, LEVEL_DRIFT, strict=True)) / total
    return min(1.0, max(0.0, drift))


def relevance(probabilities: tuple[float, ...]) -> float:
    """Adherence of one turn in [0, 1]: the complement of its drift. The pre/post measurement reports this."""
    return 1.0 - turn_drift(probabilities)


def update(track: DriftTrack, drift: float, thresholds: Thresholds) -> DriftTrack:
    """Fold one turn's drift into the running score (exponential moving average)."""
    assert 0.0 <= drift <= 1.0, f"turn drift out of range: {drift}"
    score = thresholds.alpha * drift + (1.0 - thresholds.alpha) * track.score
    return replace(track, score=min(1.0, max(0.0, score)), turns=track.turns + 1)


def zone(score: float, thresholds: Thresholds) -> int:
    """0 below the warn line, 1 in the warn zone, 2 in the block zone."""
    assert 0.0 <= score <= 1.0, f"drift score out of range: {score}"
    if score >= thresholds.block:
        return 2
    return 1 if score >= thresholds.warn else 0


def decide(track: DriftTrack, thresholds: Thresholds, blocking: bool) -> Action:
    """Map an updated track to an action.

    Warnings are edge-triggered: one per zone per excursion, so a long tangent is flagged once (and again if it
    deepens into the block zone), not on every turn. A block, when opted in, repeats every turn in the block zone:
    a prompt that is simply resent would otherwise sail through.
    """
    current = zone(track.score, thresholds)
    if current == 2 and blocking:
        return Action.BLOCK
    if current > track.alerted:
        return Action.WARN
    return Action.NONE


def record(track: DriftTrack, action: Action, thresholds: Thresholds) -> DriftTrack:
    """Count the action and remember which zone has been alerted; dropping below the warn line re-arms warnings."""
    current = zone(track.score, thresholds)
    alerted = 0 if current == 0 else max(track.alerted, current if action is not Action.NONE else track.alerted)
    track = replace(track, alerted=alerted)
    if action is Action.WARN:
        return replace(track, warnings=track.warnings + 1)
    if action is Action.BLOCK:
        return replace(track, blocks=track.blocks + 1)
    return track


def step(track: DriftTrack, drift: float, thresholds: Thresholds, blocking: bool) -> tuple[DriftTrack, Action]:
    """One judged turn: fold in its drift, decide, and record. The only entry point the hooks and backfill use."""
    updated = update(track, drift, thresholds)
    action = decide(updated, thresholds, blocking)
    return record(updated, action, thresholds), action


def reset(track: DriftTrack) -> DriftTrack:
    """Clear the running score (intent restated or drift acknowledged); keep the counters for the record."""
    return replace(track, score=0.0, alerted=0)
