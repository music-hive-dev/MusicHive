"""MusicHive: application startup and UI wiring."""

import sqlite3
import sys
from pathlib import Path

from PySide6.QtCore import (
    QFile,
    QIODevice,
    QModelIndex,
    QSettings,
    QStandardPaths,
    Qt,
    QThreadPool,
    QTimer,
)
from PySide6.QtGui import QCursor, QIcon, QImage
from PySide6.QtUiTools import QUiLoader
from PySide6.QtWidgets import (
    QAbstractItemView,
    QAbstractSlider,
    QApplication,
    QFileDialog,
    QHeaderView,
    QLabel,
    QMenu,
    QMessageBox,
    QProxyStyle,
    QStyle,
    QToolTip,
)

from src import library_db
from src.audio_player import AudioPlayer
from src.cover_view import SquareCover
from src.formatting import format_long_duration, format_size, format_time
from src.library_db import LibraryInfo, SavedSession
from src.library_scanner import ScanSignals, ScanTask
from src.play_order import PlayOrder
from src.track_list import (
    COLUMNS,
    QueueModel,
    SortedTrackModel,
    TrackDelegate,
    TrackListModel,
)
from src.track_metadata import read_track_details

UI_FILE = Path(__file__).parent / "player.ui"
ASSETS_DIR = Path(__file__).parent / "assets"

# If the track is further in than this, Previous restarts it instead of
# going back to the previous track.
PREVIOUS_RESTART_THRESHOLD_MS = 5000

REPEAT_STATUS = {
    "off": "Repeat off",
    "all": "Repeat all tracks",
    "one": "Repeat current track",
}

# Icon (a file name in assets/, without ".svg") for each context menu entry.
MENU_ICONS = {
    "play_now": "play",
    "play_next": "list-start",
    "add_to_queue": "list-plus",
    "add_to_playlist": "list-music",
    "edit": "pencil",
    "delete_from_playlist": "x",
    "delete_file": "trash",
    "go_to": "library",
    "remove_from_upcoming": "minus",
    "end_after": "eject",
}

# "Go to" entries: (menu label, name of the TrackInfo field to match)
GO_TO_FIELDS = (
    ("Artist", "artist"),
    ("Title", "title"),
    ("Album", "album"),
    ("Year", "year"),
    ("Album artist", "album_artist"),
    ("Genre", "genre"),
)


def load_ui(path: Path):
    """Load a Qt Designer .ui file and return the window it describes."""
    ui_file = QFile(str(path))
    if not ui_file.open(QIODevice.ReadOnly):
        raise RuntimeError(f"Cannot open UI file {path}: {ui_file.errorString()}")
    try:
        loader = QUiLoader()
        window = loader.load(ui_file)
    finally:
        ui_file.close()
    if window is None:
        raise RuntimeError(f"Failed to load {path}: {loader.errorString()}")
    return window


def asset_icon(name: str) -> QIcon:
    """An icon from the assets folder: asset_icon("play") loads assets/play.svg."""
    path = ASSETS_DIR / f"{name}.svg"
    icon = QIcon(str(path))
    if icon.isNull():
        print(f"MusicHive: missing or unreadable icon: {path}", file=sys.stderr)
    return icon


def setup_track_view(view, model) -> None:
    """Give the Upcoming list a model and the cover-and-two-lines row painter."""
    view.setModel(model)
    view.setItemDelegate(TrackDelegate(view))
    view.setUniformItemSizes(True)
    view.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
    view.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    view.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)


def setup_library_table(view, model) -> None:
    """Give the library table its model, look and column widths."""
    view.setModel(model)
    view.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    view.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
    view.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    view.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
    view.setAlternatingRowColors(True)
    view.setShowGrid(False)
    view.setWordWrap(False)
    view.setTextElideMode(Qt.TextElideMode.ElideRight)
    view.verticalHeader().hide()
    view.verticalHeader().setDefaultSectionSize(26)

    header = view.horizontalHeader()
    header.setHighlightSections(False)
    header.setStretchLastSection(False)
    for index, column in enumerate(COLUMNS):
        view.setColumnWidth(index, column.width)
        if column.stretch:
            header.setSectionResizeMode(index, QHeaderView.ResizeMode.Stretch)

    # Click a column title to sort by it; click it again to reverse the order.
    view.setSortingEnabled(True)
    view.sortByColumn(-1, Qt.SortOrder.AscendingOrder)  # start in library order


class JumpToClickStyle(QProxyStyle):
    """Makes a left click on a slider's groove jump straight to that position."""

    def styleHint(self, hint, option=None, widget=None, returnData=None):
        if hint == QStyle.StyleHint.SH_Slider_AbsoluteSetButtons:
            return Qt.MouseButton.LeftButton.value
        return super().styleHint(hint, option, widget, returnData)


class MusicHive:
    """Connects the widgets from player.ui to the application logic."""

    def __init__(self) -> None:
        self.window = load_ui(UI_FILE)
        self.player = AudioPlayer()
        self.settings = QSettings("MusicHive", "MusicHive")

        # Libraries
        self.libraries: list[LibraryInfo] = []
        self.current_library: LibraryInfo | None = None  # chosen in the drop-down
        self.loaded_library: LibraryInfo | None = None  # whose tracks are shown
        self.tracks: list[Path] = []  # indexed by library row
        self.current_index = -1  # library row of the playing track, or -1 if none

        # Which track plays next (play order, queue, shuffle, repeat)
        self.play_order = PlayOrder()

        # Playback
        self.playback_state = "stopped"  # "playing", "paused" or "stopped"
        self.position_ms = 0  # last reported playback position

        # Background scanning (one scan at a time)
        self.scan_pool = QThreadPool(self.window)
        self.scan_pool.setMaxThreadCount(1)
        self.scan_signals = ScanSignals(self.window)
        self.scan_task: ScanTask | None = None
        self.scan_library: LibraryInfo | None = None

        w = self.window

        # Icons used from code. (The rest are set in Qt Designer.)
        self.icon_play = asset_icon("play")
        self.icon_pause = asset_icon("pause")
        self.repeat_icons = {
            "off": asset_icon("repeat-off"),
            "all": asset_icon("repeat"),
            "one": asset_icon("repeat-1"),
        }
        self.menu_icons = {key: asset_icon(name) for key, name in MENU_ICONS.items()}

        self.volume_icons = {
            "muted": asset_icon("volume-off"),
            "silent": asset_icon("volume"),
            "low": asset_icon("volume-1"),
            "high": asset_icon("volume-2"),
        }

        # Left-click on a slider bar jumps to that position (instead of nudging).
        self._slider_style = JumpToClickStyle()
        w.progressSlider.setStyle(self._slider_style)
        w.volumeSlider.setStyle(self._slider_style)

        # Library table: the model holds the tracks (row n is library row n),
        # and the sorted model on top decides the order they are shown in.
        self.library_model = TrackListModel(w)
        self.library_proxy = SortedTrackModel(w)
        self.library_proxy.setSourceModel(self.library_model)
        setup_library_table(w.libraryList, self.library_proxy)
        self.restore_table_layout()
        w.libraryList.doubleClicked.connect(self.play_item)

        # Upcoming tracks: the playing track first, then the tracks that will
        # follow it in play order. It reads its data from the library model.
        self.queue_model = QueueModel(self.library_model, w)
        setup_track_view(w.upcomingList, self.queue_model)
        w.upcomingList.doubleClicked.connect(self.play_upcoming_item)
        w.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, w.upcomingDock)
        w.menuView.addAction(w.upcomingDock.toggleViewAction())

        # Right-click menus on both lists
        for view, in_upcoming in ((w.libraryList, False), (w.upcomingList, True)):
            view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
            view.customContextMenuRequested.connect(
                lambda pos, v=view, up=in_upcoming: self.show_track_menu(v, pos, up)
            )

        # Track info panel (filled whenever a track starts playing)
        self.cover_view = SquareCover(w.trackInfoCoverImg, asset_icon("eighth-notes"))
        for label in (
            w.trackInfoTitle,
            w.trackInfoArtistAlbumArtist,
            w.trackInfoAlbumYear,
            w.trackInfoFile,
        ):
            label.setWordWrap(True)

        # Shuffle and repeat buttons (settings are remembered between sessions)
        w.shuffleButton.setCheckable(True)
        w.repeatButton.setCheckable(True)

        shuffle_on = self.settings.value("shuffle", False, type=bool)
        w.shuffleButton.setChecked(shuffle_on)
        self.play_order.set_shuffle(shuffle_on)
        w.shuffleButton.toggled.connect(self.on_shuffle_toggled)

        self.play_order.set_repeat(self.settings.value("repeat", "off", type=str))
        self.update_repeat_button()
        w.repeatButton.clicked.connect(self.cycle_repeat)

        # Library selector and buttons
        w.libraryCombo.setPlaceholderText("No library yet")
        w.libraryCombo.activated.connect(self.on_library_chosen)
        w.addLibraryButton.clicked.connect(self.add_library)
        w.rescanButton.clicked.connect(self.rescan)

        # Menu actions (created in Qt Designer)
        w.actionAddLibrary.triggered.connect(self.add_library)
        w.actionRescanLibrary.triggered.connect(self.rescan)
        w.actionRemoveLibrary.triggered.connect(self.remove_library)
        w.actionQuit.triggered.connect(w.close)
        w.actionPlayPause.triggered.connect(self.on_play_pause)
        w.actionPrevious.triggered.connect(self.play_previous)
        w.actionNext.triggered.connect(self.play_next)
        w.actionAbout.triggered.connect(self.show_about)

        # Transport buttons
        w.playButton.clicked.connect(self.on_play_pause)
        w.previousButton.clicked.connect(self.play_previous)
        w.nextButton.clicked.connect(self.play_next)

        # Seek slider (user interaction)
        w.progressSlider.sliderMoved.connect(self.on_slider_moved)
        w.progressSlider.sliderReleased.connect(self.on_slider_released)
        w.progressSlider.actionTriggered.connect(self.on_slider_action)

        # Volume slider and mute button (the volume is remembered between sessions)
        w.muteButton.setCheckable(True)
        saved_volume = self.settings.value("volume", w.volumeSlider.value(), type=int)
        w.volumeSlider.setValue(saved_volume)
        self.player.set_volume(w.volumeSlider.value())
        self.update_volume_ui()
        w.volumeSlider.valueChanged.connect(self.on_volume_changed)
        w.muteButton.toggled.connect(self.on_mute_toggled)

        # Player -> UI
        self.player.track_started.connect(self.on_track_started)
        self.player.track_finished.connect(self.on_track_finished)
        self.player.state_changed.connect(self.on_state_changed)
        self.player.position_changed.connect(self.on_position_changed)
        self.player.duration_changed.connect(self.on_duration_changed)
        self.player.error_occurred.connect(self.on_player_error)

        # Scanner -> UI
        self.scan_signals.progress.connect(self.on_scan_progress)
        self.scan_signals.finished.connect(self.on_scan_finished)
        self.scan_signals.failed.connect(self.on_scan_failed)
        self.scan_signals.cancelled.connect(self.on_scan_cancelled)

        # Library totals, at the right end of the status bar
        self.stats_label = QLabel()
        self.stats_label.setContentsMargins(0, 0, 8, 0)
        w.statusBar().addPermanentWidget(self.stats_label)

        self.on_state_changed("stopped")
        self.clear_track_info()
        self.restore_libraries()

    def show(self) -> None:
        self.window.show()

    def shutdown(self) -> None:
        """Save the session, stop background work and playback. Called on quit."""
        if self.scan_task is not None:
            self.scan_task.cancel()
        self.scan_pool.waitForDone()  # a scan must be finished before we save
        header = self.window.libraryList.horizontalHeader()
        self.settings.setValue("libraryHeaderState", header.saveState())
        self.save_session()
        self.player.shutdown()

    def show_status(self, text: str) -> None:
        self.window.statusBar().showMessage(text)

    def show_about(self) -> None:
        QMessageBox.about(
            self.window,
            "About MusicHive",
            "<b>MusicHive</b><br>A simple local music player built with "
            "PySide6 and Qt Multimedia.",
        )

    # ----- volume and window title -----

    def on_volume_changed(self, value: int) -> None:
        """The volume slider moved (dragged, clicked, scrolled or by keyboard)."""
        w = self.window
        self.player.set_volume(value)
        self.settings.setValue("volume", value)
        if w.muteButton.isChecked():
            w.muteButton.setChecked(False)  # changing the volume unmutes
        self.update_volume_ui()
        if w.volumeSlider.isSliderDown():
            # Show the value next to the cursor while dragging.
            QToolTip.showText(QCursor.pos(), f"{value}%", w.volumeSlider)

    def on_mute_toggled(self, muted: bool) -> None:
        self.player.set_muted(muted)
        self.update_volume_ui()

    def update_volume_ui(self) -> None:
        """Match the mute button's icon and both tooltips to the volume."""
        w = self.window
        volume = w.volumeSlider.value()
        muted = w.muteButton.isChecked()

        w.volumeSlider.setToolTip(f"Volume: {volume}%")
        if muted:
            key = "muted"
            w.muteButton.setToolTip(f"Muted ({volume}%) – click to unmute")
        else:
            key = "silent" if volume == 0 else "low" if volume <= 50 else "high"
            w.muteButton.setToolTip(f"Volume: {volume}% – click to mute")
        w.muteButton.setIcon(self.volume_icons[key])

    def update_window_title(self, title: str = "", artist: str = "") -> None:
        """The window title: "MusicHive", or "MusicHive – Title – Artist" while
        a track is loaded."""
        parts = ["MusicHive"] + [part for part in (title, artist) if part]
        self.window.setWindowTitle(" – ".join(parts))

    # ----- the library table -----

    def restore_table_layout(self) -> None:
        """Bring back the column widths and sort order from the last session."""
        header = self.window.libraryList.horizontalHeader()
        state = self.settings.value("libraryHeaderState")
        if state:
            header.restoreState(state)
        for index, column in enumerate(COLUMNS):
            if column.stretch:
                header.setSectionResizeMode(index, QHeaderView.ResizeMode.Stretch)

    def display_rows(self) -> list[int]:
        """Library rows in the order the table currently shows them."""
        return self.library_proxy.source_rows()

    def table_index(self, row: int) -> QModelIndex:
        """The table's index for library row `row` (it accounts for sorting)."""
        return self.library_proxy.mapFromSource(self.library_model.index(row, 0))

    def select_in_table(self, row: int, at_top: bool = False) -> None:
        """Highlight library row `row` in the table and scroll it into view."""
        index = self.table_index(row)
        if not index.isValid():
            return
        view = self.window.libraryList
        view.setCurrentIndex(index)
        hint = (
            QAbstractItemView.ScrollHint.PositionAtTop
            if at_top
            else QAbstractItemView.ScrollHint.EnsureVisible
        )
        view.scrollTo(index, hint)

    # ----- track info panel -----

    def update_track_info(self, track: Path) -> None:
        """Fill the track info panel from the file that just started playing."""
        details = read_track_details(track)
        w = self.window
        w.trackInfoTitle.setText(details.title)
        w.trackInfoArtistAlbumArtist.setText(details.artist_line)
        w.trackInfoAlbumYear.setText(details.album_line)
        w.trackInfoFile.setText(details.audio_format)
        self.update_window_title(details.title, details.artist)
        cover = QImage.fromData(details.cover) if details.cover else None
        self.cover_view.set_image(cover)

    def clear_track_info(self) -> None:
        w = self.window
        for label in (
            w.trackInfoTitle,
            w.trackInfoArtistAlbumArtist,
            w.trackInfoAlbumYear,
            w.trackInfoFile,
        ):
            label.clear()
        self.cover_view.set_image(None)
        self.update_window_title()

    def show_nothing_playing(self) -> None:
        """Nothing is playing: reset the now-playing label and the info panel."""
        self.window.currentTrackLabel.setText("Nothing playing")
        self.clear_track_info()

    # ----- play order, shuffle and repeat -----

    def refresh_upcoming(self, keep_scroll: bool = False) -> None:
        """Show the playing track and the ones after it in the Upcoming list.

        With `keep_scroll`, the list stays where it was scrolled to (used when
        the user edits the queue); otherwise it goes back to the top.
        """
        view = self.window.upcomingList
        scroll_bar = view.verticalScrollBar()
        scroll = scroll_bar.value()

        self.queue_model.set_rows(
            self.play_order.upcoming_rows(), self.play_order.stop_after_offset()
        )
        if self.queue_model.rowCount() > 0:
            # The playing track is always the first row of the Upcoming list.
            view.setCurrentIndex(self.queue_model.index(0, 0))

        if keep_scroll:
            QTimer.singleShot(0, lambda: scroll_bar.setValue(scroll))
        else:
            view.scrollToTop()

    def reset_play_order(self) -> None:
        """Nothing is playing: start a fresh play order for the current tracks."""
        self.current_index = -1
        self.play_order.reset(len(self.tracks))
        self.refresh_upcoming()

    def on_shuffle_toggled(self, enabled: bool) -> None:
        self.play_order.set_shuffle(enabled)
        self.settings.setValue("shuffle", enabled)
        self.refresh_upcoming()
        self.show_status("Shuffle on" if enabled else "Shuffle off")

    def cycle_repeat(self) -> None:
        """Repeat button: off -> all -> one -> off."""
        modes = PlayOrder.REPEAT_MODES
        mode = modes[(modes.index(self.play_order.repeat) + 1) % len(modes)]
        self.play_order.set_repeat(mode)
        self.settings.setValue("repeat", mode)
        self.update_repeat_button()
        self.show_status(REPEAT_STATUS[mode])

    def update_repeat_button(self) -> None:
        """Make the repeat button show the current mode."""
        button = self.window.repeatButton
        mode = self.play_order.repeat
        button.setChecked(mode != "off")
        button.setIcon(self.repeat_icons[mode])
        button.setToolTip(REPEAT_STATUS[mode])

    # ----- right-click menu -----

    def show_track_menu(self, view, pos, in_upcoming: bool) -> None:
        """Right-click menu for a song in the library table or the Upcoming list."""
        index = view.indexAt(pos)
        if not index.isValid():
            return
        if in_upcoming:
            offset = index.row()  # places down the Upcoming list (0 = playing)
            row = self.queue_model.library_row(index)
        else:
            offset = -1
            row = self.library_proxy.mapToSource(index).row()
        info = self.library_model.info(row)
        if info is None or not 0 <= row < len(self.tracks):
            return

        scanning = self.scan_task is not None
        menu = QMenu(view)
        menu.setToolTipsVisible(True)

        def add(target, label, icon_key, callback, enabled=True, tip=""):
            action = target.addAction(self.menu_icons.get(icon_key, QIcon()), label)
            action.setEnabled(enabled)
            if tip:
                action.setToolTip(tip)
            action.triggered.connect(lambda _checked=False: callback())
            return action

        add(menu, "Play now", "play_now", lambda: self.play_now(row, offset))
        add(menu, "Play next", "play_next", lambda: self.queue_track(row, play_next=True))
        add(menu, "Add to queue", "add_to_queue", lambda: self.queue_track(row, play_next=False))
        add(
            menu, "Add to playlist…", "add_to_playlist", lambda: None,
            enabled=False, tip="Playlists are not available yet",
        )
        menu.addSeparator()
        add(menu, "Edit…", "edit", lambda: None, enabled=False, tip="Not available yet")
        add(
            menu, "Delete from playlist", "delete_from_playlist", lambda: None,
            enabled=False, tip="Playlists are not available yet",
        )
        add(
            menu, "Delete file", "delete_file", lambda: self.delete_file(row),
            enabled=not scanning,
            tip="Wait for the scan to finish" if scanning else "",
        )
        menu.addSeparator()

        go_menu = menu.addMenu(self.menu_icons["go_to"], "Go to")
        go_menu.setToolTipsVisible(True)
        for label, field in GO_TO_FIELDS:
            has_value = field == "title" or bool(getattr(info, field))
            add(
                go_menu, label, None, lambda f=field: self.go_to(row, f),
                enabled=has_value,
                tip="" if has_value else "This track has no such tag (rescan to read new ones)",
            )

        if in_upcoming:
            menu.addSeparator()
            add(
                menu, "Remove from upcoming", "remove_from_upcoming",
                lambda: self.remove_from_upcoming(offset),
                enabled=offset > 0,
                tip="" if offset > 0 else "The playing track cannot be removed",
            )
            marked = self.play_order.stop_after_offset() == offset
            add(
                menu,
                "Don't end playback after track" if marked else "End playback after track",
                "end_after",
                lambda: self.toggle_stop_after(offset),
            )

        menu.exec(view.viewport().mapToGlobal(pos))

        if in_upcoming and self.queue_model.rowCount() > 0:
            # The highlight in the Upcoming list marks the playing track.
            view.setCurrentIndex(self.queue_model.index(0, 0))

    def play_now(self, row: int, upcoming_offset: int = -1) -> None:
        """Play a song. From the Upcoming list (offset >= 0) the order is kept;
        from the library table (offset -1) a fresh order starts at `row`,
        following the table's current sort order."""
        if upcoming_offset >= 0:
            row = self.play_order.jump_to_upcoming(upcoming_offset)
            if row < 0:
                return
        else:
            self.play_order.start_at(row, self.display_rows())
        self.play_index(row)

    def queue_track(self, row: int, play_next: bool) -> None:
        """Play next / Add to queue."""
        title = self.library_model.info(row).title
        if self.current_index < 0:
            # Nothing is playing, so there is nothing to queue behind.
            self.play_order.start_at(row, self.display_rows())
            self.play_index(row)
            return
        if play_next:
            self.play_order.play_next(row)
            self.show_status(f"Playing next: {title}")
        else:
            self.play_order.add_to_queue(row)
            self.show_status(f"Added to queue: {title}")
        self.refresh_upcoming(keep_scroll=True)

    def remove_from_upcoming(self, offset: int) -> None:
        if self.play_order.remove_upcoming(offset):
            self.refresh_upcoming(keep_scroll=True)

    def toggle_stop_after(self, offset: int) -> None:
        enabled = self.play_order.toggle_stop_after(offset)
        self.refresh_upcoming(keep_scroll=True)
        if enabled:
            self.show_status("Playback will end after that track")
        else:
            self.show_status("Playback will no longer end after that track")

    def go_to(self, row: int, field: str) -> None:
        """Select, in the library table, the first track sharing `field` with
        `row`, going by the order the table currently shows."""
        library = self.library_model
        if field == "title":
            target = row
        else:
            value = getattr(library.info(row), field)
            target = next(
                (
                    candidate
                    for candidate in self.display_rows()
                    if getattr(library.info(candidate), field) == value
                ),
                -1,
            )
        if target < 0:
            return
        self.select_in_table(target, at_top=True)
        self.window.libraryList.setFocus()
        self.show_status(f"Went to '{library.info(target).title}'")

    def delete_file(self, row: int) -> None:
        """Move a track's file to the Trash (after asking) and drop it from the library."""
        library = self.loaded_library
        if library is None or not 0 <= row < len(self.tracks):
            return
        path = self.tracks[row]
        title = self.library_model.info(row).title

        answer = QMessageBox.question(
            self.window,
            "Delete file",
            f"Move this file to the Trash?\n\n{path}",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return

        if path.exists():
            result = QFile.moveToTrash(str(path))
            trashed = result[0] if isinstance(result, tuple) else bool(result)
            if not trashed:
                answer = QMessageBox.question(
                    self.window,
                    "Trash not available",
                    f"'{path.name}' could not be moved to the Trash.\n\n"
                    "Delete it permanently instead? This cannot be undone.",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.No,
                )
                if answer != QMessageBox.StandardButton.Yes:
                    return
                try:
                    path.unlink()
                except OSError as error:
                    QMessageBox.warning(self.window, "Cannot delete file", str(error))
                    return

        try:
            library_db.remove_track(library.db_path, self.relative_path(path))
        except sqlite3.Error:
            pass  # the next rescan will bring the library data up to date
        self.update_library_stats()

        was_playing = row == self.current_index
        table_bar = self.window.libraryList.verticalScrollBar()
        table_scroll = table_bar.value()

        del self.tracks[row]
        self.library_model.remove_row(row)  # resets the table
        if was_playing:
            self.player.unload()
            self.show_nothing_playing()
            self.reset_play_order()
        else:
            self.play_order.library_row_removed(row)
            if self.current_index > row:
                self.current_index -= 1
            if self.current_index >= 0:
                self.select_in_table(self.current_index)
            self.refresh_upcoming(keep_scroll=True)
        QTimer.singleShot(0, lambda: table_bar.setValue(table_scroll))
        self.show_status(f"Deleted '{title}'")

    # ----- saving and restoring the session -----

    def relative_path(self, path: Path) -> str:
        """A track's path relative to the library folder, as stored in the database."""
        return path.relative_to(self.loaded_library.root).as_posix()

    def save_session(self) -> None:
        """Remember the playing track, its position, the play order and the queue."""
        library = self.loaded_library
        if library is None:
            return
        try:
            if 0 <= self.current_index < len(self.tracks):
                rows, queued, order_position, stop_index = self.play_order.snapshot()
                library_db.save_session(
                    library.db_path,
                    SavedSession(
                        rel_path=self.relative_path(self.tracks[self.current_index]),
                        position_ms=self.position_ms,
                        order=rows,
                        order_position=order_position,
                        queued=queued,
                        stop_after_index=stop_index,
                    ),
                )
            else:
                library_db.clear_session(library.db_path)
        except (sqlite3.Error, OSError):
            pass  # failing to save must never get in the way of quitting

    def restore_session(self, info: LibraryInfo) -> bool:
        """Put back what was playing when the app last quit, paused.

        Returns False if there is nothing usable to restore.
        """
        try:
            session = library_db.load_session(info.db_path)
        except sqlite3.Error:
            return False
        if session is None or not self.tracks:
            return False

        queued = session.queued
        if len(queued) != len(session.order):
            queued = [False] * len(session.order)

        # Normal case: the saved order still fits this library.
        row = -1
        if self.play_order.restore(
            session.order, queued, session.order_position, session.stop_after_index
        ):
            row = self.play_order.current_row
            if self.relative_path(self.tracks[row]) != session.rel_path:
                row = -1

        # Otherwise the library changed since then: keep the song if it still
        # exists, and rebuild the order from it.
        if row < 0:
            self.play_order.reset(len(self.tracks))
            row = next(
                (
                    index
                    for index, path in enumerate(self.tracks)
                    if self.relative_path(path) == session.rel_path
                ),
                -1,
            )
            if row < 0:
                return False
            self.play_order.start_at(row, self.display_rows())

        self.play_order.set_context(self.display_rows())
        return self.load_track_paused(row, session.position_ms)

    def load_track_paused(self, row: int, position_ms: int = 0) -> bool:
        """Make `row` the current track, loaded but paused at `position_ms`."""
        self.current_index = row
        self.position_ms = position_ms
        self.select_in_table(row)
        self.refresh_upcoming()
        if not self.player.load_file(self.tracks[row], position_ms):
            self.reset_play_order()  # the song file is gone
            return False
        return True

    # ----- libraries -----

    def restore_libraries(self) -> None:
        """At startup: fill the drop-down and reopen the last used library."""
        library_db.migrate_old_libraries()
        libraries = library_db.list_libraries()
        ids = [library.id for library in libraries]
        last = self.settings.value("lastLibrary", "", type=str)
        select = last if last in ids else (ids[0] if ids else None)

        self.refresh_libraries(select)
        if self.current_library is not None:
            self.open_library(self.current_library, restore=True)
        else:
            self.show_status("Add a library to get started (File → Add Library…)")

    def refresh_libraries(self, select_id: str | None = None) -> None:
        """Re-read the saved libraries and rebuild the drop-down."""
        self.libraries = library_db.list_libraries()
        combo = self.window.libraryCombo
        combo.clear()
        for index, library in enumerate(self.libraries):
            combo.addItem(library.name, library.id)
            combo.setItemData(index, str(library.root), Qt.ItemDataRole.ToolTipRole)

        selected = next(
            (i for i, library in enumerate(self.libraries) if library.id == select_id),
            -1,
        )
        combo.setCurrentIndex(selected)
        self.current_library = self.libraries[selected] if selected >= 0 else None
        self.update_controls()

    def on_library_chosen(self, combo_index: int) -> None:
        """The user picked a library from the drop-down."""
        library_id = self.window.libraryCombo.itemData(combo_index)
        info = next((l for l in self.libraries if l.id == library_id), None)
        if info is not None:
            self.open_library(info)

    def open_library(self, info: LibraryInfo, restore: bool = False) -> None:
        """Show a library's saved tracks (scanning first if it never was).

        With `restore`, also put back the session saved when the app last quit.
        """
        self.player.unload()
        self.show_nothing_playing()
        self.current_library = info
        self.loaded_library = None

        try:
            loaded = library_db.load_library(info)
        except sqlite3.Error as error:
            self.tracks = []
            self.library_model.clear()
            self.reset_play_order()
            self.update_controls()
            self.update_library_stats()
            self.show_status(f"Cannot read library '{info.name}': {error}")
            return

        self.loaded_library = info
        self.tracks = loaded.paths
        self.library_model.set_library(loaded.paths, loaded.infos, loaded.covers)
        self.reset_play_order()
        self.settings.setValue("lastLibrary", info.id)
        self.update_controls()
        self.update_library_stats()

        if info.scanned_at is None:
            self.start_scan(info)
        elif not (restore and self.restore_session(info)):
            self.show_status(self.describe(info, len(self.tracks)))

    def reload_current_library(self) -> None:
        """Re-read the current library after a scan, without stopping playback."""
        info = self.current_library
        if info is None:
            return
        try:
            loaded = library_db.load_library(info)
        except sqlite3.Error as error:
            self.show_status(f"Cannot read library '{info.name}': {error}")
            return

        playing = (
            self.tracks[self.current_index]
            if 0 <= self.current_index < len(self.tracks)
            else None
        )
        self.loaded_library = info
        self.tracks = loaded.paths
        self.library_model.set_library(loaded.paths, loaded.infos, loaded.covers)
        self.play_order.reset(len(self.tracks))

        # If the playing track is still there, rebuild the order from it so
        # Next/Previous keep working; otherwise nothing is "playing" any more.
        if playing in self.tracks:
            row = self.tracks.index(playing)
            self.play_order.start_at(row, self.display_rows())
            self.current_index = row
            self.select_in_table(row)
        else:
            self.current_index = -1
        self.refresh_upcoming()
        self.update_library_stats()

    def add_library(self) -> None:
        start_dir = QStandardPaths.writableLocation(QStandardPaths.MusicLocation)
        folder = QFileDialog.getExistingDirectory(
            self.window, "Choose a music folder to add as a library", start_dir
        )
        if not folder:  # the user cancelled the dialog
            return

        try:
            info, created = library_db.add_library(Path(folder))
        except (OSError, sqlite3.Error) as error:
            QMessageBox.warning(
                self.window,
                "Cannot add library",
                f"The library could not be created:\n{error}",
            )
            return

        self.refresh_libraries(select_id=info.id)
        self.open_library(info)  # starts the first scan if it was never scanned
        if not created:
            self.show_status(f"'{info.name}' is already in your libraries")

    def remove_library(self) -> None:
        info = self.current_library
        if info is None:
            return
        answer = QMessageBox.question(
            self.window,
            "Remove library",
            f"Remove the library '{info.name}'?\n\n"
            "This only deletes MusicHive's saved data for it. "
            "Your music files are not touched.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return

        try:
            library_db.remove_library(info)
        except OSError as error:
            QMessageBox.warning(self.window, "Cannot remove library", str(error))
            return

        self.player.unload()
        self.show_nothing_playing()
        self.loaded_library = None  # its database is gone; nothing to save at quit
        self.tracks = []
        self.library_model.clear()
        self.reset_play_order()
        self.settings.remove("lastLibrary")
        self.refresh_libraries(select_id=None)
        self.show_status(f"Removed library '{info.name}'")

    def update_library_stats(self) -> None:
        """Show the library's file count, size and length at the right of the status bar."""
        label = self.stats_label
        library = self.loaded_library
        if library is None or library.scanned_at is None:
            label.clear()
            label.setToolTip("")
            return
        try:
            stats = library_db.library_stats(library.db_path)
        except sqlite3.Error:
            label.clear()
            label.setToolTip("")
            return

        noun = "file" if stats.file_count == 1 else "files"
        label.setText(
            f"{stats.file_count} {noun}, {format_size(stats.total_bytes)}, "
            f"{format_long_duration(stats.total_ms)}"
        )
        label.setToolTip(
            f"{stats.file_count:,} {noun}\n"
            f"{stats.total_bytes:,} bytes\n"
            f"{format_long_duration(stats.total_ms, seconds=True)}"
        )

    def describe(self, info: LibraryInfo, track_count: int) -> str:
        scanned = info.scanned_at.replace("T", " ") if info.scanned_at else "never"
        return f"{info.name}: {track_count} tracks · last scanned {scanned}"

    def update_controls(self) -> None:
        """Enable or disable the library controls to match the current state."""
        w = self.window
        scanning = self.scan_task is not None
        has_library = self.current_library is not None

        w.libraryCombo.setEnabled(not scanning and bool(self.libraries))
        w.addLibraryButton.setEnabled(not scanning)
        w.actionAddLibrary.setEnabled(not scanning)
        w.rescanButton.setEnabled(has_library and not scanning)
        w.actionRescanLibrary.setEnabled(has_library and not scanning)
        w.actionRemoveLibrary.setEnabled(has_library and not scanning)

    # ----- scanning -----

    def rescan(self) -> None:
        if self.current_library is None:
            self.show_status("Add a library first (File → Add Library…)")
            return
        self.start_scan(self.current_library)

    def start_scan(self, info: LibraryInfo) -> None:
        if self.scan_task is not None:
            return  # one scan at a time
        self.scan_library = info
        self.scan_task = ScanTask(info, self.scan_signals)
        self.update_controls()
        self.show_status(f"Scanning '{info.name}'…")
        self.scan_pool.start(self.scan_task)

    def on_scan_progress(self, library_id: str, done: int, total: int) -> None:
        name = self.scan_library.name if self.scan_library else "library"
        self.show_status(f"Scanning '{name}': {done} of {total} tracks")

    def on_scan_finished(self, library_id: str, count: int) -> None:
        name = self.scan_library.name if self.scan_library else "library"
        self.scan_task = None
        self.scan_library = None

        current_id = self.current_library.id if self.current_library else None
        self.refresh_libraries(select_id=current_id)  # also refreshes the saved info
        if self.current_library is not None and self.current_library.id == library_id:
            self.reload_current_library()

        if count:
            self.show_status(f"Scan complete: {count} tracks in '{name}'")
        else:
            self.show_status(f"Scan complete: no supported audio files in '{name}'")

    def on_scan_failed(self, library_id: str, message: str) -> None:
        self.scan_task = None
        self.scan_library = None
        self.update_controls()
        self.show_status("Scan failed")
        QMessageBox.warning(self.window, "Scan failed", message)

    def on_scan_cancelled(self, library_id: str) -> None:
        self.scan_task = None
        self.scan_library = None
        self.update_controls()

    # ----- choosing what to play -----

    def play_index(self, row: int) -> None:
        """Play library row `row`. The play order must already point at it."""
        if not 0 <= row < len(self.tracks):
            return
        self.current_index = row
        self.position_ms = 0
        self.select_in_table(row)
        self.refresh_upcoming()
        self.player.play_file(self.tracks[row])

    def play_item(self, model_index: QModelIndex) -> None:
        """Called when the user double-clicks a song in the library table."""
        row = self.library_proxy.mapToSource(model_index).row()
        if row >= 0:
            self.play_now(row)

    def play_upcoming_item(self, model_index: QModelIndex) -> None:
        """Called when the user double-clicks a song in the Upcoming list."""
        self.play_now(-1, upcoming_offset=model_index.row())

    def on_play_pause(self) -> None:
        """One button for both: pause while playing, resume while paused,
        otherwise start the highlighted track (or the first one shown)."""
        if self.playback_state == "playing":
            self.player.pause()
        elif self.playback_state == "paused":
            self.player.play()
        else:
            selected = self.window.libraryList.currentIndex()
            row = self.library_proxy.mapToSource(selected).row() if selected.isValid() else -1
            if row < 0:
                shown = self.display_rows()
                row = shown[0] if shown else -1
            if row < 0:
                self.show_status("Choose a library with music first")
                return
            self.play_order.start_at(row, self.display_rows())
            self.play_index(row)

    def play_next(self) -> None:
        if self.current_index < 0:
            shown = self.display_rows()
            if shown:  # nothing playing yet: start from the top of the table
                self.play_order.start_at(shown[0], shown)
                self.play_index(shown[0])
            return
        row = self.play_order.next()
        if row is None:
            self.show_status("This is the last track")
        else:
            self.play_index(row)

    def play_previous(self) -> None:
        """Restart the track if it is well underway, otherwise go back one."""
        if self.current_index < 0:
            return
        if self.position_ms > PREVIOUS_RESTART_THRESHOLD_MS:
            self.player.seek(0)  # keeps the current play/pause state
            return
        row = self.play_order.previous()
        if row is None:  # already at the start of the play order
            self.player.seek(0)
        else:
            self.play_index(row)

    # ----- seek slider -----

    def on_slider_moved(self, value: int) -> None:
        """While dragging, only update the time label; seek on release."""
        self.window.currentTimeLabel.setText(format_time(value))

    def on_slider_released(self) -> None:
        self.player.seek(self.window.progressSlider.value())

    def on_slider_action(self, action: int) -> None:
        """Handle keyboard and wheel changes (arrows, PageUp/Down, Home/End).

        Slider-move actions come from clicks and drags; those are handled
        once, on release, by on_slider_released.
        """
        if action == QAbstractSlider.SliderAction.SliderMove.value:
            return
        slider = self.window.progressSlider
        if not slider.isSliderDown():
            self.player.seek(slider.sliderPosition())

    # ----- player -> UI -----

    def on_track_started(self, track: Path) -> None:
        self.window.currentTrackLabel.setText(track.stem)
        self.update_track_info(track)

    def on_track_finished(self) -> None:
        stop_here = self.play_order.take_stop_after()
        row = self.play_order.next(ended_naturally=True)
        if row is None:
            self.show_status("Reached the end of the library")
        elif stop_here:
            # "End playback after track": the next track waits, paused.
            self.load_track_paused(row)
        else:
            self.play_index(row)

    def on_state_changed(self, state: str) -> None:
        self.playback_state = state
        w = self.window

        # The single transport button shows what clicking it will do.
        if state == "playing":
            w.playButton.setIcon(self.icon_pause)
            w.playButton.setToolTip("Pause")
            self.show_status(f"Playing {w.currentTrackLabel.text()}")
        else:
            w.playButton.setIcon(self.icon_play)
            w.playButton.setToolTip("Play")
            if state == "paused":
                self.show_status("Paused")
            else:  # stopped
                self.position_ms = 0
                w.progressSlider.setValue(0)
                w.currentTimeLabel.setText(format_time(0))

    def on_position_changed(self, position_ms: int) -> None:
        self.position_ms = position_ms
        slider = self.window.progressSlider
        if slider.isSliderDown():  # the user is dragging; don't fight them
            return
        slider.setValue(position_ms)
        self.window.currentTimeLabel.setText(format_time(position_ms))

    def on_duration_changed(self, duration_ms: int) -> None:
        self.window.progressSlider.setRange(0, duration_ms)
        self.window.durationLabel.setText(format_time(duration_ms))

    def on_player_error(self, message: str) -> None:
        self.show_nothing_playing()
        self.show_status(message)


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("MusicHive")
    app.setDesktopFileName("musichive")  # must match musichive.desktop
    music_hive = MusicHive()
    app.aboutToQuit.connect(music_hive.shutdown)
    music_hive.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())