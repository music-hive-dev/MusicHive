"""The Radio tab (player.ui) and the Radio tab of the settings window.

The tab does not play anything itself: it asks for a station to be played
(play_requested) and is told what is playing (set_playing, set_now_playing…).
"""

import urllib.parse
from collections.abc import Callable
from dataclasses import replace

from PySide6.QtCore import QDateTime, QLocale, QObject, QPoint, QSettings, Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QIcon, QImage, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QFileDialog,
    QHeaderView,
    QInputDialog,
    QListWidgetItem,
    QMenu,
    QTableWidgetItem,
    QWidget,
)

from src.elided_label import ElidedLabel
from src.formatting import format_time
from src.number_field import NumberField
from src.podcast_view import (
    HEADING_ROLE,
    ID_ROLE,
    LINES_ROLE,
    CoverRowDelegate,
    square_pixmap,
)
from src.radio import RadioManager, Station

LIST_VISIBLE_KEY = "radioListVisible"
LIST_ICON = 40
RESULT_ICON = 48
LOGO_SIZE = 96
RECENT_IN_LIST = 10
LIVE_SLACK_SECONDS = 2  # closer to live than this counts as live
WEB_SEARCH = "https://duckduckgo.com/?q="
STATION_ROLE = Qt.ItemDataRole.UserRole + 5  # a search result's Station


def heard_time(seconds: float) -> str:
    moment = QDateTime.fromSecsSinceEpoch(int(seconds))
    if moment.date() == QDateTime.currentDateTime().date():
        return QLocale.system().toString(moment.time(), QLocale.FormatType.ShortFormat)
    return QLocale.system().toString(moment, QLocale.FormatType.ShortFormat)


class RadioTab(QObject):
    """Connects the Radio tab of player.ui to the RadioManager."""

    play_requested = Signal(int)  # station id
    go_live_requested = Signal()
    tape_toggled = Signal(bool)
    remember_requested = Signal()
    pin_toggled = Signal(int)  # a station to pin to the sidebar, or unpin

    def __init__(
        self,
        window: QWidget,
        manager: RadioManager,
        row_delegate: type,
        placeholder: QIcon,
        icon: Callable[[str], QIcon],
        show_status,
        settings: QSettings,
        find_in_library: Callable[[str], None],
    ) -> None:
        super().__init__(window)
        self.w = w = window
        self.manager = manager
        self._placeholder = placeholder
        self._icon = icon
        self._show_status = show_status
        self._settings = settings
        self._find_in_library = find_in_library
        self.is_pinned: Callable[[int], bool] = lambda _station_id: False  # set by the window
        self.shown_station: int | None = None
        self.playing_station: int | None = None
        self._now_playing = ""
        self._can_tape = False
        self._results: list[Station] = []
        self._lists_loaded = False
        self._name = ElidedLabel(w.radioName, max_lines=1)
        self._details = ElidedLabel(w.radioDetails, max_lines=2)
        self._now = ElidedLabel(w.radioNowPlaying, max_lines=1)
        self._browse_title = ElidedLabel(w.radioBrowseTitle, max_lines=1)

        for view in (w.radioList, w.radioResults):
            view.setItemDelegate(CoverRowDelegate(view))
        for table, widths in ((w.radioHistory, {0: 90}), (w.radioRemembered, {1: 180, 2: 130})):
            table.setItemDelegate(row_delegate(table))
            table.verticalHeader().setDefaultSectionSize(30)
            header = table.horizontalHeader()
            header.setHighlightSections(False)
            title_column = 1 if table is w.radioHistory else 0
            header.setSectionResizeMode(title_column, QHeaderView.ResizeMode.Stretch)
            for column, width in widths.items():
                header.setSectionResizeMode(column, QHeaderView.ResizeMode.Interactive)
                table.setColumnWidth(column, width)

        w.radioSearch.returnPressed.connect(self.search)
        w.radioSearchButton.clicked.connect(lambda _checked=False: self.search())
        for combo in (w.radioCountry, w.radioLanguage, w.radioGenre):
            combo.activated.connect(lambda _index: self.search())
        w.radioRememberedButton.clicked.connect(lambda _checked=False: self.show_remembered())
        w.radioListButton.toggled.connect(self._set_list_visible)
        visible = settings.value(LIST_VISIBLE_KEY, True, type=bool)
        w.radioListButton.setChecked(visible)
        self._set_list_visible(visible)

        w.radioList.itemClicked.connect(self._on_list_clicked)
        w.radioList.itemActivated.connect(self._on_list_activated)
        w.radioList.customContextMenuRequested.connect(self._show_list_menu)
        w.radioResults.itemClicked.connect(self._on_result_clicked)
        w.radioResults.itemActivated.connect(self._on_result_activated)
        w.radioResults.customContextMenuRequested.connect(self._show_result_menu)
        w.radioPlayButton.clicked.connect(lambda _checked=False: self._play_shown())
        w.radioFavouriteButton.clicked.connect(lambda _checked=False: self._toggle_favourite())
        w.radioTapeButton.toggled.connect(self._on_tape_button)
        w.radioRememberButton.clicked.connect(lambda _checked=False: self.remember_requested.emit())
        w.radioGoLiveButton.clicked.connect(lambda _checked=False: self.go_live_requested.emit())
        w.radioHistory.customContextMenuRequested.connect(self._show_history_menu)
        w.radioRemembered.customContextMenuRequested.connect(self._show_remembered_menu)

        manager.stations_found.connect(self._show_results)
        manager.lists_ready.connect(self._fill_lists)
        manager.favourites_changed.connect(self.refresh_list)
        manager.station_changed.connect(self._on_station_changed)
        manager.history_changed.connect(self._on_history_changed)
        manager.remembered_changed.connect(self._on_remembered_changed)
        manager.logo_ready.connect(self._on_logo_ready)

        self.refresh_list()
        self._browse_title.set_text("Popular stations")
        w.radioPages.setCurrentWidget(w.radioBrowsePage)
        w.podcastsTab.currentChanged.connect(lambda _index: self._load_when_shown())
        self._load_when_shown()

    def _load_when_shown(self) -> None:
        """The directory is asked only once the tab is first opened."""
        if not self._lists_loaded and self.w.podcastsTab.currentWidget() is self.w.radioTab:
            self._lists_loaded = True
            self.manager.load_lists()
            self.manager.popular()

    # ----- the list of favourites and recent stations -----

    def logo(self, station_id: int, side: int, rounded: bool = True) -> QPixmap:
        image = QImage(str(self.manager.logo_file(station_id)))
        if image.isNull():
            return self._placeholder.pixmap(side, side)
        return square_pixmap(image, side, rounded)

    def logo_image(self, station_id: int) -> QImage | None:
        image = QImage(str(self.manager.logo_file(station_id)))
        return None if image.isNull() else image

    def refresh_list(self) -> None:
        view = self.w.radioList
        store = self.manager.store
        favourites = store.favourites()
        favourite_ids = {s.id for s in favourites}
        recent = [s for s in store.recent() if s.id not in favourite_ids][:RECENT_IN_LIST]
        view.clear()
        for station in favourites:
            self._add_list_row(station)
        if recent:
            heading = QListWidgetItem("Recently played")
            heading.setData(HEADING_ROLE, True)
            heading.setFlags(Qt.ItemFlag.NoItemFlags)
            view.addItem(heading)
            for station in recent:
                self._add_list_row(station)
        if not favourites and not recent:
            heading = QListWidgetItem("Your favourite stations appear here")
            heading.setData(HEADING_ROLE, True)
            heading.setFlags(Qt.ItemFlag.NoItemFlags)
            view.addItem(heading)

    def _add_list_row(self, station: Station) -> None:
        view = self.w.radioList
        second = "Playing" if station.id == self.playing_station else station.details()
        item = QListWidgetItem(QIcon(self.logo(station.id, LIST_ICON)), station.name)
        item.setData(LINES_ROLE, [station.name, second])
        item.setData(ID_ROLE, station.id)
        item.setToolTip(station.name)
        view.addItem(item)
        if station.id == self.shown_station:
            view.setCurrentItem(item)
        self.manager.fetch_logo(station.id)

    def _set_list_visible(self, visible: bool) -> None:
        self.w.radioList.setVisible(visible)
        self._settings.setValue(LIST_VISIBLE_KEY, visible)

    def _on_list_clicked(self, item: QListWidgetItem) -> None:
        if item.data(ID_ROLE):
            self.show_station(item.data(ID_ROLE))

    def _on_list_activated(self, item: QListWidgetItem) -> None:
        if item.data(ID_ROLE):
            self.play_requested.emit(item.data(ID_ROLE))

    def _show_list_menu(self, position: QPoint) -> None:
        item = self.w.radioList.itemAt(position)
        station = self.manager.store.station(item.data(ID_ROLE)) if item and item.data(ID_ROLE) else None
        if station is None:
            return
        menu = QMenu(self.w.radioList)
        self._add_station_actions(menu, station)
        if not station.favourite and station.last_played:
            menu.addAction(self._icon("x"), "Remove from Recently Played").triggered.connect(
                lambda _checked=False: self._forget_recent(station.id)
            )
        menu.exec(self.w.radioList.viewport().mapToGlobal(position))

    def _add_station_actions(self, menu: QMenu, station: Station) -> None:
        menu.addAction(self._icon("play"), "Play").triggered.connect(
            lambda _checked=False: self.play_requested.emit(station.id)
        )
        menu.addSeparator()
        if station.favourite:
            menu.addAction(self._icon("star-off"), "Remove from Favourites").triggered.connect(
                lambda _checked=False: self.manager.set_favourite(station.id, False)
            )
        else:
            menu.addAction(self._icon("star"), "Add to Favourites").triggered.connect(
                lambda _checked=False: self.manager.set_favourite(station.id, True)
            )
        menu.addAction(self._icon("pencil"), "Rename...").triggered.connect(
            lambda _checked=False: self._rename(station)
        )
        pinned = self.is_pinned(station.id)
        menu.addAction(
            self._icon("pin-off" if pinned else "pin"),
            "Unpin from Sidebar" if pinned else "Pin to Sidebar",
        ).triggered.connect(lambda _checked=False: self.pin_toggled.emit(station.id))
        if station.homepage:
            menu.addAction(self._icon("globe"), "Open Website").triggered.connect(
                lambda _checked=False: QDesktopServices.openUrl(QUrl(station.homepage))
            )

    def _rename(self, station: Station) -> None:
        name, accepted = QInputDialog.getText(self.w, "Rename station", "Name:", text=station.name)
        if accepted and name.strip():
            self.manager.rename(station.id, name.strip())

    def _forget_recent(self, station_id: int) -> None:
        self.manager.store.forget_recent(station_id)
        self.refresh_list()

    def _on_station_changed(self, station_id: int) -> None:
        self.refresh_list()
        if station_id == self.shown_station:
            self.show_station(station_id)

    def _on_logo_ready(self, station_id: int) -> None:
        view = self.w.radioList
        for row in range(view.count()):
            item = view.item(row)
            if item.data(ID_ROLE) == station_id:
                item.setIcon(QIcon(self.logo(station_id, LIST_ICON)))
        if station_id == self.shown_station:
            self.w.radioLogo.setPixmap(self.logo(station_id, LOGO_SIZE * 2, rounded=False))

    # ----- finding stations -----

    def _fill_lists(self, lists: dict) -> None:
        w = self.w
        for combo, everything, entries in (
            (w.radioCountry, "All countries", [(name, code) for name, code in lists["countries"]]),
            (w.radioLanguage, "All languages", [(name.title(), name) for name in lists["languages"]]),
            (w.radioGenre, "All genres", [(name.title(), name) for name in lists["tags"]]),
        ):
            combo.clear()
            combo.addItem(everything, "")
            for text, value in entries:
                combo.addItem(text, value)

    def search(self) -> None:
        w = self.w
        text = w.radioSearch.text().strip()
        if "://" in text:
            station_id = self.manager.add_by_address(text)
            self.show_station(station_id)
            self._show_status("Added the station. Play it, and add it to your favourites if you like it.")
            return
        country = w.radioCountry.currentData() or ""
        language = w.radioLanguage.currentData() or ""
        genre = w.radioGenre.currentData() or ""
        if not (text or country or language or genre):
            self._browse_title.set_text("Popular stations")
            w.radioPages.setCurrentWidget(w.radioBrowsePage)
            self.manager.popular()
            return
        parts = [f"“{text}”"] if text else []
        parts += [w.radioGenre.currentText() if genre else "",
                  w.radioLanguage.currentText() if language else "",
                  w.radioCountry.currentText() if country else ""]
        heading = "Stations: " + ", ".join(p for p in parts if p)
        self._browse_title.set_text(heading)
        w.radioResults.clear()
        w.radioPages.setCurrentWidget(w.radioBrowsePage)
        self.manager.search(text, country, language, genre, heading)

    def _show_results(self, heading: str, stations: list[Station]) -> None:
        self._results = stations
        view = self.w.radioResults
        view.clear()
        self._browse_title.set_text(heading if stations else f"{heading} – nothing found")
        placeholder = QIcon(self._placeholder.pixmap(RESULT_ICON, RESULT_ICON))
        for index, station in enumerate(stations):
            item = QListWidgetItem(placeholder, station.name)
            item.setData(LINES_ROLE, [station.name, station.details()])
            item.setData(ID_ROLE, index)
            item.setToolTip(station.name)
            view.addItem(item)
            if station.favicon:
                self.manager.fetch_image(
                    station.favicon, lambda data, i=item, r=stations: self._set_result_icon(i, r, data)
                )
        self.w.radioPages.setCurrentWidget(self.w.radioBrowsePage)

    def _set_result_icon(self, item: QListWidgetItem, results: list, data: bytes) -> None:
        if results is not self._results:
            return  # newer results replaced these
        image = QImage.fromData(data)
        if not image.isNull():
            item.setIcon(QIcon(square_pixmap(image, RESULT_ICON)))

    def _result(self, item: QListWidgetItem | None) -> Station | None:
        index = item.data(ID_ROLE) if item is not None else None
        return self._results[index] if index is not None and 0 <= index < len(self._results) else None

    def _on_result_clicked(self, item: QListWidgetItem) -> None:
        station = self._result(item)
        if station is not None:
            self.show_station(self.manager.keep(station))

    def _on_result_activated(self, item: QListWidgetItem) -> None:
        station = self._result(item)
        if station is not None:
            self.play_requested.emit(self.manager.keep(station))

    def _show_result_menu(self, position: QPoint) -> None:
        station = self._result(self.w.radioResults.itemAt(position))
        if station is None:
            return
        stored = self.manager.store.station(self.manager.keep(station))
        menu = QMenu(self.w.radioResults)
        self._add_station_actions(menu, stored)
        menu.exec(self.w.radioResults.viewport().mapToGlobal(position))

    # ----- one station -----

    def show_station(self, station_id: int) -> None:
        station = self.manager.store.station(station_id)
        if station is None:
            return
        w = self.w
        self.shown_station = station_id
        w.radioPages.setCurrentWidget(w.radioStationPage)
        w.radioLogo.setPixmap(self.logo(station_id, LOGO_SIZE * 2, rounded=False))
        self._name.set_text(station.name)
        details = station.details()
        if station.language:
            details = f"{details} · {station.language.title()}" if details else station.language.title()
        self._details.set_text(details)
        w.radioFavouriteButton.setIcon(self._icon("star_full" if station.favourite else "star"))
        w.radioFavouriteButton.setToolTip(
            "Remove from favourites" if station.favourite else "Add to favourites"
        )
        self._show_playing_controls()
        self._fill_history()
        self.manager.fetch_logo(station_id)

    def _show_playing_controls(self) -> None:
        w = self.w
        playing = self.shown_station is not None and self.shown_station == self.playing_station
        self._now.set_text(f"Now: {self._now_playing}" if playing and self._now_playing else "")
        w.radioLiveLabel.setVisible(playing)
        w.radioGoLiveButton.setVisible(playing)
        w.radioTapeButton.setEnabled(playing and self._can_tape)
        w.radioRememberButton.setEnabled(playing and bool(self._now_playing))
        w.radioPlayButton.setToolTip("Playing" if playing else "Play this station")

    def _play_shown(self) -> None:
        if self.shown_station is not None:
            self.play_requested.emit(self.shown_station)

    def _toggle_favourite(self) -> None:
        station = self.manager.store.station(self.shown_station) if self.shown_station else None
        if station is not None:
            self.manager.set_favourite(station.id, not station.favourite)

    def _on_tape_button(self, on: bool) -> None:
        self.w.radioTapeButton.setToolTip("Stop taping" if on else self._tape_tooltip())
        self.tape_toggled.emit(on)

    def _tape_tooltip(self) -> str:
        return ("Tape what you hear: each song becomes a file in the tapes folder"
                if self.manager.settings.tape_per_song
                else "Tape what you hear into one file in the tapes folder")

    # ----- told by the player -----

    def set_playing(self, station_id: int | None, can_tape: bool = False) -> None:
        self.playing_station = station_id
        self._can_tape = can_tape
        self._now_playing = ""
        if station_id is None:
            self.set_taping(False)
        self.refresh_list()
        self._show_playing_controls()

    def set_now_playing(self, title: str) -> None:
        self._now_playing = title
        self._show_playing_controls()

    def set_behind(self, seconds: float) -> None:
        w = self.w
        live = seconds < LIVE_SLACK_SECONDS
        w.radioLiveLabel.setText("Live" if live else f"{format_time(int(seconds * 1000))} behind live")
        w.radioGoLiveButton.setEnabled(not live)

    def set_taping(self, on: bool) -> None:
        button = self.w.radioTapeButton
        button.blockSignals(True)
        button.setChecked(on)
        button.blockSignals(False)
        button.setToolTip("Stop taping" if on else self._tape_tooltip())

    # ----- what was heard -----

    def _fill_history(self) -> None:
        table = self.w.radioHistory
        songs = self.manager.store.history(self.shown_station) if self.shown_station else []
        table.setRowCount(len(songs))
        for row, song in enumerate(songs):
            when = QTableWidgetItem(heard_time(song.heard_at))
            title = QTableWidgetItem(song.title)
            title.setData(ID_ROLE, song.id)
            title.setToolTip(song.title)
            table.setItem(row, 0, when)
            table.setItem(row, 1, title)

    def _on_history_changed(self, station_id: int) -> None:
        if station_id == self.shown_station:
            self._fill_history()

    def _selected_titles(self, table, column: int) -> list[str]:
        rows = sorted({index.row() for index in table.selectionModel().selectedRows()})
        return [table.item(row, column).text() for row in rows if table.item(row, column)]

    def _show_history_menu(self, position: QPoint) -> None:
        table = self.w.radioHistory
        item = table.itemAt(position)
        if item is None:
            return
        if not table.item(item.row(), 1).isSelected():
            table.selectRow(item.row())
        titles = self._selected_titles(table, 1)
        menu = QMenu(table)
        menu.addAction(self._icon("heart-plus"), "Remember").triggered.connect(
            lambda _checked=False: [self.manager.remember(self.shown_station, t) for t in titles]
        )
        self._add_song_actions(menu, titles)
        menu.addSeparator()
        menu.addAction(self._icon("trash"), "Clear This Station's History").triggered.connect(
            lambda _checked=False: self.manager.clear_history(self.shown_station)
        )
        menu.exec(table.viewport().mapToGlobal(position))

    def _add_song_actions(self, menu: QMenu, titles: list[str]) -> None:
        menu.addAction(self._icon("clipboard-copy"), "Copy").triggered.connect(
            lambda _checked=False: QApplication.clipboard().setText("\n".join(titles))
        )
        if len(titles) == 1:
            title = titles[0]
            menu.addAction(self._icon("library"), "Find in Library").triggered.connect(
                lambda _checked=False: self._find_in_library(title)
            )
            menu.addAction(self._icon("search"), "Search the Web").triggered.connect(
                lambda _checked=False: QDesktopServices.openUrl(
                    QUrl(WEB_SEARCH + urllib.parse.quote(title))
                )
            )

    # ----- remembered songs -----

    def show_remembered(self) -> None:
        self._fill_remembered()
        self.w.radioPages.setCurrentWidget(self.w.radioRememberedPage)

    def _fill_remembered(self) -> None:
        table = self.w.radioRemembered
        songs = self.manager.store.remembered()
        table.setRowCount(len(songs))
        for row, song in enumerate(songs):
            title = QTableWidgetItem(song.title)
            title.setData(ID_ROLE, song.id)
            title.setToolTip(song.title)
            table.setItem(row, 0, title)
            table.setItem(row, 1, QTableWidgetItem(song.station))
            table.setItem(row, 2, QTableWidgetItem(heard_time(song.heard_at)))
        self.w.radioRememberedTitle.setText(
            f"Remembered songs ({len(songs)})" if songs else "Remembered songs – none yet"
        )

    def _on_remembered_changed(self) -> None:
        if self.w.radioPages.currentWidget() is self.w.radioRememberedPage:
            self._fill_remembered()

    def _show_remembered_menu(self, position: QPoint) -> None:
        table = self.w.radioRemembered
        item = table.itemAt(position)
        if item is None:
            return
        if not table.item(item.row(), 0).isSelected():
            table.selectRow(item.row())
        rows = sorted({index.row() for index in table.selectionModel().selectedRows()})
        titles = [table.item(row, 0).text() for row in rows]
        ids = [table.item(row, 0).data(ID_ROLE) for row in rows]
        menu = QMenu(table)
        self._add_song_actions(menu, titles)
        menu.addSeparator()
        menu.addAction(self._icon("trash"), "Forget").triggered.connect(
            lambda _checked=False: self.manager.forget(ids)
        )
        menu.exec(table.viewport().mapToGlobal(position))


class RadioSettingsTab:
    """Connects the Radio tab of settings.ui. Every change applies at once."""

    def __init__(self, dialog: QDialog, manager: RadioManager) -> None:
        self._dialog = d = dialog
        self._manager = manager
        self._minutes = NumberField(d.radioKeepMinutes)
        self._after = NumberField(d.radioScrobbleAfter)
        self._showing = True
        s = manager.settings
        self._minutes.setValue(s.keep_minutes)
        d.radioScrobble.setChecked(s.scrobble)
        self._after.setValue(s.scrobble_after_seconds)
        d.radioTapePerSong.setChecked(s.tape_per_song)
        d.radioTapeFromStart.setChecked(s.tape_from_song_start)
        d.radioTapeTags.setChecked(s.tape_tags)
        self._show_folder()
        self._showing = False
        self._show_enabled()

        for field in (self._minutes, self._after):
            field.valueChanged.connect(lambda _value: self._changed())
        for checkbox in (d.radioScrobble, d.radioTapePerSong, d.radioTapeFromStart, d.radioTapeTags):
            checkbox.toggled.connect(lambda _on: self._changed())
        d.radioTapeFolderButton.clicked.connect(lambda _checked=False: self._choose_folder())
        d.radioTapeFolderReset.clicked.connect(lambda _checked=False: self._set_folder(""))

    def _show_folder(self) -> None:
        settings = self._manager.settings
        self._dialog.radioTapeFolder.setText(str(settings.tapes_folder()))
        self._dialog.radioTapeFolderReset.setEnabled(bool(settings.tape_folder))

    def _show_enabled(self) -> None:
        d = self._dialog
        on = d.radioScrobble.isChecked()
        d.radioScrobbleAfterLabel.setEnabled(on)
        d.radioScrobbleAfter.setEnabled(on)

    def _choose_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(
            self._dialog, "Folder for tapes", str(self._manager.settings.tapes_folder())
        )
        if folder:
            self._set_folder(folder)

    def _set_folder(self, folder: str) -> None:
        self._apply(tape_folder=folder)
        self._show_folder()

    def _changed(self) -> None:
        if self._showing:
            return
        self._show_enabled()
        d = self._dialog
        self._apply(
            keep_minutes=self._minutes.value(),
            scrobble=d.radioScrobble.isChecked(),
            scrobble_after_seconds=self._after.value(),
            tape_per_song=d.radioTapePerSong.isChecked(),
            tape_from_song_start=d.radioTapeFromStart.isChecked(),
            tape_tags=d.radioTapeTags.isChecked(),
        )

    def _apply(self, **changes) -> None:
        self._manager.apply_settings(replace(self._manager.settings, **changes))

