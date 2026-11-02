"""Deciding which track plays next: normal order, shuffle and repeat.

No GUI code lives here. Tracks are identified by their row number in the
library.
"""

import random


class PlayOrder:
    """The order in which a library's tracks will play.

    `_order` lists every library row exactly once, and `_position` is where
    the playing track sits within it (-1 when nothing is playing).
    """

    REPEAT_MODES = ("off", "all", "one")

    def __init__(self) -> None:
        self.shuffle = False
        self.repeat = "off"
        self._order: list[int] = []
        self._position = -1

    # ----- reading the state -----

    @property
    def current_row(self) -> int:
        """Library row of the playing track, or -1 if nothing is playing."""
        if 0 <= self._position < len(self._order):
            return self._order[self._position]
        return -1

    def upcoming_rows(self) -> list[int]:
        """The playing track, followed by the tracks that come after it."""
        if self._position < 0:
            return []
        return self._order[self._position :]

    # ----- changing the library or the modes -----

    def reset(self, track_count: int) -> None:
        """Start over for a library of `track_count` tracks; nothing playing."""
        self._order = list(range(track_count))
        self._position = -1

    def set_shuffle(self, enabled: bool) -> None:
        """Turn shuffle on or off, keeping the playing track where it is."""
        self.shuffle = enabled
        current = self.current_row
        if current >= 0:
            self._build_order(first=current)

    def set_repeat(self, mode: str) -> None:
        if mode in self.REPEAT_MODES:
            self.repeat = mode

    # ----- choosing a track -----

    def start_at(self, row: int) -> None:
        """The user picked `row` from the library: play it, then what follows.

        With shuffle on, the other tracks get a fresh random order.
        """
        if 0 <= row < len(self._order):
            self._build_order(first=row)

    def jump_to_upcoming(self, offset: int) -> int:
        """The user picked the item `offset` places down the Upcoming list.

        Returns that track's library row (the order itself is kept), or -1.
        """
        position = self._position + offset
        if self._position < 0 or not 0 <= position < len(self._order):
            return -1
        self._position = position
        return self._order[position]

    # ----- moving through the order -----

    def next(self, ended_naturally: bool = False) -> int | None:
        """Move to the next track and return its row, or None at the end.

        Repeat-one only applies when a track ends by itself: pressing Next
        still moves on.
        """
        if self._position < 0 or not self._order:
            return None
        if ended_naturally and self.repeat == "one":
            return self.current_row
        if self._position + 1 < len(self._order):
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

    # ----- internal -----

    def _build_order(self, first: int) -> None:
        """Make `first` the playing track and order the rest."""
        count = len(self._order)
        if self.shuffle:
            rest = [row for row in range(count) if row != first]
            random.shuffle(rest)
            self._order = [first] + rest
            self._position = 0
        else:
            self._order = list(range(count))
            self._position = first

    def _wrap_around(self) -> None:
        """Start the order again after its last track (repeat all)."""
        count = len(self._order)
        last = self._order[-1]
        if self.shuffle and count > 1:
            order = list(range(count))
            random.shuffle(order)
            if order[0] == last:  # don't play the same track twice in a row
                swap = random.randrange(1, count)
                order[0], order[swap] = order[swap], order[0]
            self._order = order
        self._position = 0