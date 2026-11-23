from src.listening import ListenTracker


def test_counts_at_half_the_track():
    tracker = ListenTracker()
    tracker.begin(200_000, playing=True, now=0)
    assert not tracker.check(now=99.9)
    assert tracker.check(now=100)


def test_counts_only_once_per_play():
    tracker = ListenTracker()
    tracker.begin(200_000, playing=True, now=0)
    assert tracker.check(now=150)
    assert not tracker.check(now=180)


def test_at_most_four_minutes_are_needed():
    tracker = ListenTracker()
    tracker.begin(1_000_000, playing=True, now=0)
    assert tracker.threshold_seconds() == 240
    assert not tracker.check(now=239)
    assert tracker.check(now=240)


def test_very_short_tracks_never_count():
    tracker = ListenTracker()
    tracker.begin(500, playing=True, now=0)
    assert tracker.threshold_seconds() is None
    assert not tracker.check(now=1000)


def test_pausing_stops_the_clock():
    tracker = ListenTracker()
    tracker.begin(200_000, playing=True, now=0)
    tracker.set_playing(False, now=50)
    assert tracker.listened_seconds(now=1000) == 50
    tracker.set_playing(True, now=1000)
    assert tracker.listened_seconds(now=1030) == 80


def test_begin_paused_measures_nothing_until_play():
    tracker = ListenTracker()
    tracker.begin(200_000, playing=False, now=0)
    assert tracker.started_at is None
    assert tracker.listened_seconds(now=500) == 0
    tracker.set_playing(True, now=500)
    assert tracker.started_at is not None
    assert tracker.listened_seconds(now=510) == 10


def test_reset_means_nothing_counts():
    tracker = ListenTracker()
    tracker.begin(200_000, playing=True, now=0)
    tracker.reset()
    assert not tracker.check(now=1000)


def test_begin_starts_again_from_zero():
    tracker = ListenTracker()
    tracker.begin(200_000, playing=True, now=0)
    assert tracker.check(now=150)
    tracker.begin(200_000, playing=True, now=150)
    assert not tracker.check(now=200)
    assert tracker.check(now=250)
