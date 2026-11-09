"""Deciding which track plays next: play order, queue, shuffle and repeat.

No GUI code lives here. Tracks are identified by their row number in the
library.
"""

import random
from dataclasses import dataclass


@dataclass(slots=True)
class Entry:
    row: int  # library row of the track
    queued: bool  # True if the user queued it (Play next / Add to queue)
    uid: int  # identifies this entry; the same track can appear more than once


class PlayOrder:
    """The sequence of tracks that plays, with a pointer to the playing one.

    `_entries` is the whole sequence, history included. `_position` is where
    the playing track is in it (-1 when nothing is playing).
    """

    REPEAT_MODES = ("off", "all", "one")

    def __init__(self) -> None:
        self.shuffle = False
        self.repeat = "off"
        self._entries: list[Entry] = []
        self._position = -1
        self._count = 0  # number of tracks in the library
        self._context: list[int] = []  # library rows in the order the table showed them
        self._next_uid = 0
        self._stop_after: int | None = None  # uid of the entry playback ends after

    # ----- reading the state -----

    @property
    def current_row(self) -> int:
        """Library row of the playing track, or -1 if nothing is playing."""
        if 0 <= self._position < len(self._entries):
            return self._entries[self._position].row
        return -1

    def upcoming_rows(self) -> list[int]:
        """The playing track, followed by the tracks that come after it."""
        if self._position < 0:
            return []
        return [entry.row for entry in self._entries[self._position :]]

    def stop_after_offset(self) -> int:
        """Where in upcoming_rows() playback is set to end, or -1."""
        if self._stop_after is None or self._position < 0:
            return -1
        for offset, entry in enumerate(self._entries[self._position :]):
            if entry.uid == self._stop_after:
                return offset
        return -1

    # ----- changing the library or the modes -----

    def reset(self, track_count: int) -> None:
        """Start over for a library of `track_count` tracks; nothing playing."""
        self._count = track_count
        self._context = list(range(track_count))
        self._entries = [self._new_entry(row) for row in self._context]
        self._position = -1
        self._stop_after = None

    def set_shuffle(self, enabled: bool) -> None:
        """Turn shuffle on or off, keeping the playing track and the queue."""
        self.shuffle = enabled
        current = self.current_row
        if current >= 0:
            self._rebuild(first=current, context=self._context)

    def set_repeat(self, mode: str) -> None:
        if mode in self.REPEAT_MODES:
            self.repeat = mode

    def library_row_removed(self, row: int) -> None:
        """A library track was deleted: drop its entries and renumber the rest."""
        entries: list[Entry] = []
        position = self._position
        for index, entry in enumerate(self._entries):
            if entry.row == row:
                if index < self._position:
                    position -= 1
                elif index == self._position:
                    position = -1
                continue
            if entry.row > row:
                entry.row -= 1
            entries.append(entry)
        self._entries = entries
        self._context = [r - 1 if r > row else r for r in self._context if r != row]
        self._count = max(0, self._count - 1)
        self._position = position

    # ----- choosing a track -----

    def start_at(self, row: int, context: list[int] | None = None) -> None:
        """The user picked `row` from the library: play it, then what follows.

        `context` is the library rows in the order the table shows them; the
        tracks after `row` in that order come next. Without it, the order of
        the last start is used. With shuffle on, the other tracks get a fresh
        random order. Tracks the user queued after the playing one are kept.
        """
        if 0 <= row < self._count:
            self._rebuild(
                first=row, context=self._context if context is None else context
            )

    def set_context(self, rows: list[int]) -> None:
        """Set the order used when the play order is rebuilt or starts over."""
        self._context = list(rows)

    def context_changed(self, rows: list[int]) -> None:
        """The list being played was edited: what follows the playing track now
        follows `rows`. A shuffled order is left alone, and so is everything
        when the playing track is no longer in the list."""
        current = self.current_row
        if self.shuffle or current not in rows:
            self._context = list(rows)
            return
        stop_row = next((e.row for e in self._entries if e.uid == self._stop_after), None)
        self._rebuild(first=current, context=rows)
        if stop_row is not None:
            upcoming = self._entries[self._position :]
            self._stop_after = next((e.uid for e in upcoming if e.row == stop_row), None)

    def jump_to_upcoming(self, offset: int) -> int:
        """The user picked the item `offset` places down the Upcoming list.

        Returns that track's library row (the order itself is kept), or -1.
        """
        position = self._position + offset
        if self._position < 0 or not 0 <= position < len(self._entries):
            return -1
        self._position = position
        return self._entries[position].row

    # ----- the queue -----

    def play_next(self, row: int) -> None:
        """Queue `row` to play right after the playing track, ahead of anything
        queued earlier."""
        if self._position >= 0 and 0 <= row < self._count:
            self._entries.insert(self._position + 1, self._new_entry(row, queued=True))

    def add_to_queue(self, row: int) -> None:
        """Queue `row` after the other queued tracks, ahead of the rest."""
        if self._position < 0 or not 0 <= row < self._count:
            return
        index = self._position + 1
        while index < len(self._entries) and self._entries[index].queued:
            index += 1
        self._entries.insert(index, self._new_entry(row, queued=True))

    def remove_upcoming(self, offset: int) -> bool:
        """Remove the entry `offset` places down the Upcoming list.

        The playing track (offset 0) cannot be removed.
        """
        index = self._position + offset
        if self._position < 0 or offset < 1 or index >= len(self._entries):
            return False
        entry = self._entries.pop(index)
        if entry.uid == self._stop_after:
            self._stop_after = None
        return True

    def toggle_stop_after(self, offset: int) -> bool:
        """Mark (or unmark) the entry `offset` places down the Upcoming list as
        the one playback ends after. Returns True if it is now marked."""
        index = self._position + offset
        if self._position < 0 or offset < 0 or index >= len(self._entries):
            return False
        uid = self._entries[index].uid
        self._stop_after = None if self._stop_after == uid else uid
        return self._stop_after is not None

    def take_stop_after(self) -> bool:
        """True, once, if playback should end after the playing track."""
        if self._position < 0 or self._stop_after is None:
            return False
        if self._entries[self._position].uid != self._stop_after:
            return False
        self._stop_after = None
        return True

    # ----- moving through the order -----

    def next(self, ended_naturally: bool = False) -> int | None:
        """Move to the next track and return its row, or None at the end.

        Repeat-one only applies when a track ends by itself: pressing Next
        still moves on.
        """
        if self._position < 0 or not self._entries:
            return None
        if ended_naturally and self.repeat == "one":
            return self.current_row
        if self._position + 1 < len(self._entries):
            self._position += 1
            return self.current_row
        if self.repeat == "all":
            self._wrap_around()
            return self.current_row
        return None

    def previous(self) -> int | None:
        """Move back one track and return its row, or None at the start."""
        if self._position > 0:
            self._position -= 1
            return self.current_row
        return None

    # ----- saving and restoring -----

    def snapshot(self) -> tuple[list[int], list[bool], int, int]:
        """Rows, queued flags, playing position and stop-after position."""
        stop_index = -1
        if self._stop_after is not None:
            for index, entry in enumerate(self._entries):
                if entry.uid == self._stop_after:
                    stop_index = index
                    break
        return (
            [entry.row for entry in self._entries],
            [entry.queued for entry in self._entries],
            self._position,
            stop_index,
        )

    def restore(
        self,
        rows: list[int],
        queued: list[bool],
        position: int,
        stop_after_index: int,
    ) -> bool:
        """Put back a saved order. Returns False, changing nothing, if it
        does not fit the current library."""
        if (
            len(rows) != len(queued)
            or any(not 0 <= row < self._count for row in rows)
            or not 0 <= position < len(rows)
            or stop_after_index >= len(rows)
        ):
            return False
        self._entries = [self._new_entry(row, flag) for row, flag in zip(rows, queued)]
        self._position = position
        self._stop_after = (
            self._entries[stop_after_index].uid if stop_after_index >= 0 else None
        )
        return True

    # ----- internal -----

    def _new_entry(self, row: int, queued: bool = False) -> Entry:
        entry = Entry(row, queued, self._next_uid)
        self._next_uid += 1
        return entry

    def _rebuild(self, first: int, context: list[int]) -> None:
        """Make `first` the playing track and order the library around it.

        `context` is the library rows in the order the table shows them.
        Tracks the user queued after the playing one are kept, right after it.
        """
        queued: list[Entry] = []
        if self._position >= 0:
            queued = [e for e in self._entries[self._position + 1 :] if e.queued]

        context = list(context)
        if first not in context:
            context.insert(0, first)
        self._context = context

        if self.shuffle:
            rest = [row for row in context if row != first]
            random.shuffle(rest)
            rows = [first] + rest
            position = 0
        else:
            rows = context
            position = context.index(first)

        entries = [self._new_entry(row) for row in rows]
        entries[position + 1 : position + 1] = queued
        self._entries = entries
        self._position = position

    def _wrap_around(self) -> None:
        """Start again after the last track (repeat all)."""
        rows = list(self._context) or list(range(self._count))
        if not rows:
            return
        last = self._entries[-1].row
        if self.shuffle and len(rows) > 1:
            random.shuffle(rows)
            if rows[0] == last:  # don't play the same track twice in a row
                swap = random.randrange(1, len(rows))
                rows[0], rows[swap] = rows[swap], rows[0]
        self._entries = [self._new_entry(row) for row in rows]
        self._position = 0
        self._stop_after = None