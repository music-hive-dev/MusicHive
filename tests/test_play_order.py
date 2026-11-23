import random

import pytest

from src.play_order import PlayOrder


@pytest.fixture
def order():
    """A library of five tracks, nothing playing."""
    play_order = PlayOrder()
    play_order.reset(5)
    return play_order


def test_nothing_playing_after_reset(order):
    assert order.current_row == -1
    assert order.upcoming_rows() == []
    assert order.next() is None


def test_start_at_plays_the_rest_in_order(order):
    order.start_at(2)
    assert order.current_row == 2
    assert order.upcoming_rows() == [2, 3, 4]


def test_start_at_ignores_rows_outside_the_library(order):
    order.start_at(7)
    assert order.current_row == -1


def test_start_at_with_context(order):
    order.start_at(2, [4, 2, 0])
    assert order.upcoming_rows() == [2, 0]


def test_next_and_previous(order):
    order.start_at(3)
    assert order.next() == 4
    assert order.next() is None
    assert order.current_row == 4
    assert order.previous() == 3
    order.start_at(0)
    assert order.previous() is None


def test_repeat_one_only_when_the_track_ends_by_itself(order):
    order.set_repeat("one")
    order.start_at(1)
    assert order.next(ended_naturally=True) == 1
    assert order.next() == 2


def test_repeat_all_wraps_around(order):
    order.set_repeat("all")
    order.start_at(4)
    assert order.next() == 0
    assert order.upcoming_rows() == [0, 1, 2, 3, 4]


def test_unknown_repeat_mode_is_ignored(order):
    order.set_repeat("sometimes")
    assert order.repeat == "off"


def test_queue_order(order):
    order.start_at(0)
    order.add_to_queue(3)
    order.add_to_queue(4)
    order.play_next(2)
    assert order.upcoming_rows() == [0, 2, 3, 4, 1, 2, 3, 4]
    order.add_to_end(1)
    assert order.upcoming_rows()[-1] == 1


def test_queue_needs_something_playing(order):
    order.add_to_queue(1)
    order.play_next(1)
    order.add_to_end(1)
    order.insert_upcoming(1, [1])
    assert order.upcoming_rows() == []


def test_queue_survives_starting_another_track(order):
    order.start_at(0)
    order.add_to_queue(4)
    order.start_at(2)
    assert order.upcoming_rows() == [2, 4, 3, 4]


def test_insert_upcoming_never_above_the_playing_track(order):
    order.start_at(0)
    order.insert_upcoming(0, [4, 4])
    assert order.upcoming_rows() == [0, 4, 4, 1, 2, 3, 4]
    order.insert_upcoming(100, [3])
    assert order.upcoming_rows()[-1] == 3


def test_move_upcoming(order):
    order.start_at(0)
    assert order.move_upcoming([3], 1)
    assert order.upcoming_rows() == [0, 3, 1, 2, 4]


def test_move_upcoming_to_the_same_place_changes_nothing(order):
    order.start_at(0)
    assert not order.move_upcoming([1], 1)
    assert not order.move_upcoming([0], 3)  # the playing track stays put
    assert order.upcoming_rows() == [0, 1, 2, 3, 4]


def test_remove_upcoming(order):
    order.start_at(0)
    assert not order.remove_upcoming(0)
    assert order.remove_upcoming(1)
    assert order.upcoming_rows() == [0, 2, 3, 4]
    assert not order.remove_upcoming(10)


def test_jump_to_upcoming(order):
    order.start_at(1)
    assert order.jump_to_upcoming(2) == 3
    assert order.current_row == 3
    assert order.jump_to_upcoming(10) == -1


def test_stop_after(order):
    order.start_at(0)
    assert order.toggle_stop_after(2)
    assert order.stop_after_offset() == 2
    assert not order.ends_after_current()
    order.next()
    assert not order.take_stop_after()
    order.next()
    assert order.ends_after_current()
    assert order.take_stop_after()
    assert not order.take_stop_after()
    assert order.stop_after_offset() == -1


def test_toggle_stop_after_twice_unmarks(order):
    order.start_at(0)
    assert order.toggle_stop_after(1)
    assert not order.toggle_stop_after(1)
    assert order.stop_after_offset() == -1


def test_removing_the_stop_after_entry_clears_it(order):
    order.start_at(0)
    order.toggle_stop_after(1)
    order.remove_upcoming(1)
    assert order.stop_after_offset() == -1


def test_ends_after_current(order):
    order.start_at(4)
    assert order.ends_after_current()
    order.set_repeat("all")
    assert not order.ends_after_current()


def test_library_row_removed_renumbers(order):
    order.start_at(2)
    order.library_row_removed(1)
    assert order.current_row == 1
    assert order.upcoming_rows() == [1, 2, 3]


def test_removing_the_playing_track_stops(order):
    order.start_at(2)
    order.library_row_removed(2)
    assert order.current_row == -1


def test_context_changed_follows_the_edited_list(order):
    order.start_at(1)
    order.context_changed([1, 4, 3])
    assert order.upcoming_rows() == [1, 4, 3]


def test_context_changed_keeps_stop_after(order):
    order.start_at(1)
    order.toggle_stop_after(3)  # row 4
    order.context_changed([1, 4, 3])
    assert order.stop_after_offset() == 1


def test_snapshot_and_restore(order):
    order.start_at(1)
    order.add_to_queue(4)
    order.toggle_stop_after(1)
    saved = order.snapshot()
    assert saved == ([0, 1, 4, 2, 3, 4], [False, False, True, False, False, False], 1, 2)

    restored = PlayOrder()
    restored.reset(5)
    assert restored.restore(*saved)
    assert restored.upcoming_rows() == [1, 4, 2, 3, 4]
    assert restored.stop_after_offset() == 1


def test_restore_refuses_a_saved_order_that_does_not_fit():
    play_order = PlayOrder()
    play_order.reset(3)
    assert not play_order.restore([0, 4], [False, False], 0, -1)
    assert not play_order.restore([0, 1], [False], 0, -1)
    assert not play_order.restore([0, 1], [False, False], 2, -1)
    assert play_order.current_row == -1


def test_shuffle_keeps_the_playing_track_and_the_queue(order):
    random.seed(1)
    order.start_at(2)
    order.add_to_queue(4)
    order.set_shuffle(True)
    upcoming = order.upcoming_rows()
    assert upcoming[0] == 2
    assert upcoming[1] == 4
    assert sorted(upcoming) == [0, 1, 2, 3, 4, 4]


def test_shuffled_start_plays_every_track_once(order):
    random.seed(2)
    order.set_shuffle(True)
    order.start_at(3)
    upcoming = order.upcoming_rows()
    assert upcoming[0] == 3
    assert sorted(upcoming) == [0, 1, 2, 3, 4]


def test_shuffled_repeat_all_never_plays_a_track_twice_in_a_row():
    for seed in range(50):
        random.seed(seed)
        play_order = PlayOrder()
        play_order.reset(5)
        play_order.set_shuffle(True)
        play_order.set_repeat("all")
        play_order.start_at(0)
        for _ in range(4):
            play_order.next()
        last = play_order.current_row
        assert play_order.next() != last
