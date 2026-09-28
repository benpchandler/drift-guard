import pytest

from drift_guard import drift
from drift_guard.drift import Action, DriftTrack, Thresholds

T = Thresholds()


def test_turn_drift_weights_levels() -> None:
    assert drift.turn_drift((1, 0, 0, 0)) == 1.0
    assert drift.turn_drift((0, 1, 0, 0)) == 0.5
    assert drift.turn_drift((0, 0, 1, 0)) == 0.0
    assert drift.turn_drift((0, 0, 0, 1)) == 0.0
    assert drift.turn_drift((0.5, 0.5, 0, 0)) == pytest.approx(0.75)


def test_turn_drift_normalizes_and_rejects_empty() -> None:
    assert drift.turn_drift((2, 0, 0, 2)) == pytest.approx(0.5)
    with pytest.raises(ValueError):
        drift.turn_drift((0, 0, 0, 0))
    with pytest.raises(AssertionError):
        drift.turn_drift((1, 0, 0))


def test_one_off_topic_turn_warns_two_block() -> None:
    once, action = drift.step(DriftTrack(), 1.0, T, blocking=True)
    assert once.score == pytest.approx(0.6) and action is Action.WARN
    twice, action = drift.step(once, 1.0, T, blocking=True)
    assert twice.score == pytest.approx(0.84) and action is Action.BLOCK and twice.blocks == 1


def test_block_repeats_while_in_block_zone() -> None:
    track, _ = drift.step(DriftTrack(), 1.0, T, blocking=True)
    track, _ = drift.step(track, 1.0, T, blocking=True)
    track, action = drift.step(track, 1.0, T, blocking=True)
    assert action is Action.BLOCK and track.blocks == 2


def test_warnings_are_edge_triggered() -> None:
    track, first = drift.step(DriftTrack(), 1.0, T, blocking=False)
    track, second = drift.step(track, 0.6, T, blocking=False)  # still in the warn zone
    assert (first, second) == (Action.WARN, Action.NONE) and track.warnings == 1
    track, deeper = drift.step(track, 1.0, T, blocking=False)  # into the block zone: one more, stronger warning
    assert deeper is Action.WARN and track.alerted == 2
    track, again = drift.step(track, 1.0, T, blocking=False)
    assert again is Action.NONE


def test_dropping_below_warn_rearms() -> None:
    track, _ = drift.step(DriftTrack(), 1.0, T, blocking=False)
    track, _ = drift.step(track, 0.0, T, blocking=False)
    assert track.alerted == 0
    _, action = drift.step(track, 1.0, T, blocking=False)
    assert action is Action.WARN


def test_tangents_alone_never_warn() -> None:
    """A tangent counts half, so a run of pure tangents converges to 0.5 from below: noted, never flagged alone."""
    track = DriftTrack()
    for _ in range(20):
        track, action = drift.step(track, 0.5, T, blocking=True)
        assert action is Action.NONE
    _, action = drift.step(track, 1.0, T, blocking=True)
    assert action is Action.WARN


def test_on_topic_turns_recover() -> None:
    track, _ = drift.step(DriftTrack(), 1.0, T, blocking=True)
    track, _ = drift.step(track, 1.0, T, blocking=True)
    track, action = drift.step(track, 0.0, T, blocking=True)
    assert action is Action.NONE and drift.zone(track.score, T) == 0 and track.alerted == 0


def test_warn_only_never_blocks() -> None:
    track = DriftTrack()
    for _ in range(5):
        track, action = drift.step(track, 1.0, T, blocking=False)
        assert action is not Action.BLOCK


def test_reset_clears_score_and_alert_keeps_counters() -> None:
    track, _ = drift.step(DriftTrack(), 1.0, T, blocking=False)
    cleared = drift.reset(track)
    assert cleared.score == 0.0 and cleared.alerted == 0 and cleared.turns == 1 and cleared.warnings == 1


@pytest.mark.parametrize(("alpha", "warn", "block"), [(0, 0.5, 0.8), (0.6, 0.8, 0.5), (0.6, 0.5, 1.2), (0.6, 0, 0.8)])
def test_thresholds_validate(alpha: float, warn: float, block: float) -> None:
    with pytest.raises(ValueError):
        Thresholds(alpha=alpha, warn=warn, block=block)
