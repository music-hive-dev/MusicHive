"""The Podcasts tab (player.ui) and the Podcasts tab of the settings window."""

from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import (
    QDateTime,
    QLocale,
    QObject,
    QPoint,
    QRect,
    QRectF,
    QSettings,
    QSize,
    Qt,
    QUrl,
    Signal,
)
from PySide6.QtGui import (
    QColor,
    QDesktopServices,
    QFont,
    QFontMetrics,
    QIcon,
    QImage,
    QPainter,
    QPainterPath,
    QPixmap,
    QTextDocumentFragment,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QDialog,
    QFileDialog,
    QHeaderView,
    QListWidgetItem,
    QMenu,
    QMessageBox,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QTableWidgetItem,
    QWidget,
)

from src import theme
from src.elided_label import ElidedLabel
from src.number_field import NumberField
from src.podcasts import Episode, Podcast, PodcastManager, PodcastSettings, SearchResult

ID_ROLE = Qt.ItemDataRole.UserRole
LIST_VISIBLE_KEY = "podcastListVisible"
LINES_ROLE = Qt.ItemDataRole.UserRole + 2  # a list row's lines: [title] or [title, second]
HEADING_ROLE = Qt.ItemDataRole.UserRole + 3  # True for a heading row between list sections
HEADING_HEIGHT = 26
ROW_PADDING = 3  # around the cover, the same on every side (as in the song lists)
TEXT_GAP = 10  # between the cover and the text
PROGRESS_ROLE = Qt.ItemDataRole.UserRole + 1  # 0..1 for the progress bar, None for none
TITLE, RELEASED, LENGTH, PROGRESS, DOWNLOAD = range(5)
COLUMN_WIDTHS = {RELEASED: 100, LENGTH: 70, PROGRESS: 130, DOWNLOAD: 130}
LIST_ICON = 40
SEARCH_ICON = 56
COVER_SIZE = 96
TOOLTIP_CHARACTERS = 600
PLAYING_MARK = "▶ "
DIMMED_ALPHA = 140  # played episodes
CORNER_RATIO = 0.155  # rounded corners, in proportion like the song lists' covers (7 of 45 px)


def plain_text(text: str) -> str:
    """Feeds often describe things in HTML; this keeps only the words."""
    if "<" not in text and "&" not in text:
        return text.strip()
    return QTextDocumentFragment.fromHtml(text).toPlainText().strip()


def format_length(ms: int) -> str:
    minutes = round(ms / 60_000)
    if minutes < 60:
        return f"{minutes} min"
    return f"{minutes // 60} h {minutes % 60:02d} min"


def format_date(seconds: float) -> str:
    if not seconds:
        return ""
    date = QDateTime.fromSecsSinceEpoch(int(seconds)).date()
    return QLocale.system().toString(date, QLocale.FormatType.ShortFormat)


def square_pixmap(image: QImage, side: int, rounded: bool = True) -> QPixmap:
    """The image cropped to a square of `side` pixels, with rounded corners (or square)."""
    scaled = image.scaled(
        side, side,
        Qt.AspectRatioMode.KeepAspectRatioByExpanding,
        Qt.TransformationMode.SmoothTransformation,
    )
    x, y = (scaled.width() - side) // 2, (scaled.height() - side) // 2
    pixmap = QPixmap(side, side)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    corners = QPainterPath()
    radius = side * CORNER_RATIO if rounded else 0
    corners.addRoundedRect(QRectF(0, 0, side, side), radius, radius)
    painter.setClipPath(corners)
    painter.drawImage(0, 0, scaled, x, y, side, side)
    painter.end()
    return pixmap


class CoverRowDelegate(QStyledItemDelegate):
    """Paints a list row as [cover] bold title / dimmed second line, like the song
    lists: the same padding all round the cover, text shortened with "…" to fit."""

    def sizeHint(self, option, index) -> QSize:
        if index.data(HEADING_ROLE):
            return QSize(100, HEADING_HEIGHT)
        side = option.decorationSize.height()
        return QSize(100, side + 2 * ROW_PADDING)

    def paint(self, painter: QPainter, option, index) -> None:
        if index.data(HEADING_ROLE):  # a section's name, small and dimmed
            colour = QColor(theme.colour(theme.TEXT))
            colour.setAlpha(170)
            painter.save()
            painter.setPen(colour)
            painter.setFont(option.font)
            painter.drawText(
                option.rect.adjusted(ROW_PADDING + 4, 0, -ROW_PADDING, 0),
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignBottom,
                index.data(Qt.ItemDataRole.DisplayRole) or "",
            )
            painter.restore()
            return
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        # A copy: opt.icon hands out the option's own icon, which is cleared just below.
        icon = QIcon(opt.icon)
        if icon.isNull():
            decoration = index.data(Qt.ItemDataRole.DecorationRole)
            icon = decoration if isinstance(decoration, QIcon) else QIcon()
        opt.text = ""
        opt.icon = QIcon()
        widget = option.widget
        style = widget.style() if widget else QApplication.style()
        style.drawControl(QStyle.ControlElement.CE_ItemViewItem, opt, painter, widget)

        rect = option.rect
        side = option.decorationSize.height()
        cover_rect = QRect(rect.left() + ROW_PADDING, rect.top() + ROW_PADDING, side, side)
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        icon.paint(painter, cover_rect)

        selected = bool(opt.state & QStyle.StateFlag.State_Selected)
        title_colour = QColor(theme.on_highlight() if selected else theme.colour(theme.TEXT))
        second_colour = QColor(title_colour)
        second_colour.setAlpha(170)
        lines = index.data(LINES_ROLE) or [index.data(Qt.ItemDataRole.DisplayRole) or ""]
        left = cover_rect.right() + 1 + TEXT_GAP
        width = max(0, rect.right() - ROW_PADDING - left + 1)
        title_font = QFont(option.font)
        title_font.setBold(True)
        title_metrics = QFontMetrics(title_font)
        title = title_metrics.elidedText(lines[0], Qt.TextElideMode.ElideRight, width)
        painter.setFont(title_font)
        painter.setPen(title_colour)
        if len(lines) > 1 and lines[1]:
            painter.drawText(QRect(left, cover_rect.top(), width, side // 2),
                             Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignBottom, title)
            second = QFontMetrics(option.font).elidedText(lines[1], Qt.TextElideMode.ElideRight, width)
            painter.setFont(option.font)
            painter.setPen(second_colour)
            painter.drawText(QRect(left, cover_rect.top() + side // 2, width, side - side // 2),
                             Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop, second)
        else:
            painter.drawText(QRect(left, cover_rect.top(), width, side),
                             Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, title)
        painter.restore()


def make_progress_delegate(base: type) -> type:
    """A cell delegate (built on the tables' own) that draws a thin progress bar."""

    class ProgressDelegate(base):
        def paint(self, painter: QPainter, option, index) -> None:
            super().paint(painter, option, index)
            fraction = index.data(PROGRESS_ROLE)
            if fraction is None:
                return
            rect = QRectF(option.rect).adjusted(10, option.rect.height() - 7, -10, -4)
            painter.save()
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.setPen(Qt.PenStyle.NoPen)
            track = QColor(theme.colour(theme.TEXT))
            track.setAlpha(45)
            painter.setBrush(track)
            painter.drawRoundedRect(rect, 1.5, 1.5)
            if fraction > 0:
                done = QRectF(rect)
                done.setWidth(max(3.0, rect.width() * min(1.0, fraction)))
                painter.setBrush(QColor(theme.active_highlight()))
                painter.drawRoundedRect(done, 1.5, 1.5)
            painter.restore()

    return ProgressDelegate


class PodcastsTab(QObject):
    """Connects the Podcasts tab of player.ui to the PodcastManager."""

    play_requested = Signal(int)  # the episode to play

    def __init__(
        self,
        window: QWidget,
        manager: PodcastManager,
        row_delegate: type,
        placeholder: QIcon,
        show_status,
        icon: Callable[[str], QIcon],
        settings: QSettings,
    ) -> None:
        super().__init__(window)
        self.w = w = window
        self.manager = manager
        self._placeholder = placeholder
        self._show_status = show_status
        self._icon = icon  # an icon from the assets folder, by name
        self._settings = settings
        self.shown_podcast: int | None = None
        self.playing_episode: int | None = None
        self._rows: dict[int, int] = {}  # episode id -> table row
        self._search_results: list[SearchResult] = []
        # Their text never sets their width, so long titles cannot widen the window.
        self._title = ElidedLabel(w.podcastTitle, max_lines=1)
        self._author = ElidedLabel(w.podcastAuthor, max_lines=1)
        self._description = ElidedLabel(w.podcastDescription, max_lines=3)
        # "x of y played" is always shown whole; the rest gives way when it is narrow.
        self._progress_details = ElidedLabel(w.podcastProgressDetails, max_lines=1)
        self._search_title = ElidedLabel(w.podcastSearchTitle, max_lines=1)
        self._keep_count = NumberField(w.podcastKeepCount)
        self._showing = False

        table = w.podcastEpisodes
        table.setItemDelegate(row_delegate(table))
        self._progress_delegate = make_progress_delegate(row_delegate)(table)
        table.setItemDelegateForColumn(PROGRESS, self._progress_delegate)
        table.verticalHeader().setDefaultSectionSize(30)
        header = table.horizontalHeader()
        header.setHighlightSections(False)
        header.setSectionResizeMode(TITLE, QHeaderView.ResizeMode.Stretch)
        for column, width in COLUMN_WIDTHS.items():
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.Interactive)
            table.setColumnWidth(column, width)

        w.podcastSearch.returnPressed.connect(self.search)
        w.podcastSearchButton.clicked.connect(lambda _checked=False: self.search())
        w.podcastCheckButton.clicked.connect(lambda _checked=False: manager.check_all())
        self._row_delegate = CoverRowDelegate(w.podcastList)
        w.podcastList.setItemDelegate(self._row_delegate)
        w.podcastSearchResults.setItemDelegate(CoverRowDelegate(w.podcastSearchResults))
        w.podcastList.currentItemChanged.connect(self._on_podcast_chosen)
        w.podcastList.customContextMenuRequested.connect(self._show_podcast_menu)
        w.podcastSearchResults.itemClicked.connect(self._on_result_clicked)
        w.podcastSearchResults.itemActivated.connect(self._on_result_clicked)
        w.podcastSubscribeButton.clicked.connect(lambda _checked=False: self._toggle_subscription())
        w.podcastPlayButton.clicked.connect(lambda _checked=False: self._continue_shown())
        # The funnel button shows or hides the list of podcasts, as on the other tabs.
        w.podcastListButton.toggled.connect(self._set_list_visible)
        visible = settings.value(LIST_VISIBLE_KEY, True, type=bool)
        w.podcastListButton.setChecked(visible)
        self._set_list_visible(visible)
        self._keep_count.valueChanged.connect(self._on_keep_count_changed)
        table.doubleClicked.connect(self._on_episode_activated)
        table.customContextMenuRequested.connect(self._show_episode_menu)

        manager.podcasts_changed.connect(self.refresh_list)
        manager.podcast_changed.connect(self._on_podcast_changed)
        manager.episode_changed.connect(self.update_episode)
        manager.download_progress.connect(self._on_download_progress)
        manager.search_finished.connect(self._show_results)
        manager.feed_opened.connect(self.show_podcast)
        manager.cover_ready.connect(self._on_cover_ready)

        self.refresh_list()
        w.podcastPages.setCurrentWidget(w.podcastWelcomePage)

    # ----- the list of subscriptions -----

    def cover_icon(self, podcast_id: int, side: int, rounded: bool = True) -> QPixmap:
        image = QImage(str(self.manager.cover_file(podcast_id)))
        if image.isNull():
            return self._placeholder.pixmap(side, side)
        return square_pixmap(image, side, rounded)

    def cover_image(self, podcast_id: int) -> QImage | None:
        """The podcast's cover at full size, for the player's info panel."""
        image = QImage(str(self.manager.cover_file(podcast_id)))
        return None if image.isNull() else image

    def refresh_list(self) -> None:
        view = self.w.podcastList
        view.blockSignals(True)
        view.clear()
        for podcast in self.manager.store.podcasts():
            new = self.manager.store.new_count(podcast.id)
            second = f"{new} new" if new else podcast.author
            item = QListWidgetItem(QIcon(self.cover_icon(podcast.id, LIST_ICON)), podcast.title)
            item.setData(LINES_ROLE, [podcast.title, second] if second else [podcast.title])
            item.setData(ID_ROLE, podcast.id)
            item.setToolTip(podcast.title)
            view.addItem(item)
            if podcast.id == self.shown_podcast:
                view.setCurrentItem(item)
        view.blockSignals(False)

    def _on_podcast_chosen(self, item: QListWidgetItem | None, _previous) -> None:
        if item is not None:
            self.show_podcast(item.data(ID_ROLE))

    def _on_podcast_changed(self, podcast_id: int) -> None:
        if podcast_id == self.shown_podcast:
            self.show_podcast(podcast_id)
        self.refresh_list()

    def _on_cover_ready(self, podcast_id: int) -> None:
        self.refresh_list()
        if podcast_id == self.shown_podcast:
            self.w.podcastCover.setPixmap(self.cover_icon(podcast_id, COVER_SIZE * 2, rounded=False))

    def _show_podcast_menu(self, position: QPoint) -> None:
        item = self.w.podcastList.itemAt(position)
        if item is None:
            return
        podcast = self.manager.store.podcast(item.data(ID_ROLE))
        if podcast is None:
            return
        menu = QMenu(self.w.podcastList)
        menu.addAction(self._icon("play"), "Play").triggered.connect(
            lambda _checked=False: self._continue(podcast)
        )
        menu.addSeparator()
        menu.addAction(self._icon("refresh-cw"), "Check for New Episodes").triggered.connect(
            lambda _checked=False: self.manager.check(podcast.id)
        )
        menu.addAction(self._icon("download"), "Download the Next Episodes").triggered.connect(
            lambda _checked=False: self.manager.auto_download(podcast.id)
        )
        menu.addAction(self._icon("list-check"), "Mark All as Played").triggered.connect(
            lambda _checked=False: self.manager.set_played(
                [e.id for e in self.manager.store.episodes(podcast.id)], True
            )
        )
        if podcast.link:
            menu.addAction(self._icon("globe"), "Open Website").triggered.connect(
                lambda _checked=False: QDesktopServices.openUrl(QUrl(podcast.link))
            )
        menu.addSeparator()
        menu.addAction(self._icon("calendar-off"), "Unsubscribe...").triggered.connect(
            lambda _checked=False: self._unsubscribe(podcast)
        )
        menu.exec(self.w.podcastList.viewport().mapToGlobal(position))

    def _set_list_visible(self, visible: bool) -> None:
        self.w.podcastList.setVisible(visible)
        self._settings.setValue(LIST_VISIBLE_KEY, visible)

    def _continue_shown(self) -> None:
        podcast = self.manager.store.podcast(self.shown_podcast) if self.shown_podcast else None
        if podcast is not None:
            self._continue(podcast)

    def _continue(self, podcast: Podcast) -> None:
        """Carry on where you left off, or with the next episode if that one ended."""
        episode_id = self.manager.episode_to_continue(podcast.id)
        if episode_id is None:
            self._show_status(f"You have heard every episode of {podcast.title}")
        else:
            self.play_requested.emit(episode_id)

    # ----- searching -----

    def search(self) -> None:
        text = self.w.podcastSearch.text().strip()
        if not text:
            return
        if "://" in text or text.lower().startswith("www."):
            self.manager.open_feed(text if "://" in text else f"https://{text}")
            return
        self._search_title.set_text(f"Searching for “{text}”…")
        self.w.podcastSearchResults.clear()
        self.w.podcastPages.setCurrentWidget(self.w.podcastSearchPage)
        self.manager.search(text)

    def _show_results(self, results: list[SearchResult]) -> None:
        self._search_results = results
        view = self.w.podcastSearchResults
        view.clear()
        text = self.w.podcastSearch.text().strip()
        self._search_title.set_text(
            f"Results for “{text}”" if results else f"Nothing found for “{text}”"
        )
        self._show_status("")
        placeholder = QIcon(self._placeholder.pixmap(SEARCH_ICON, SEARCH_ICON))
        for index, result in enumerate(results):
            details = " · ".join(
                part for part in (
                    result.author,
                    result.genre,
                    f"{result.episode_count} episodes" if result.episode_count else "",
                ) if part
            )
            item = QListWidgetItem(placeholder, result.title)  # the text type-to-find looks at
            item.setData(LINES_ROLE, [result.title, details])
            item.setToolTip(result.title)
            item.setData(ID_ROLE, index)
            view.addItem(item)
            if result.image_url:
                self.manager.fetch_image(
                    result.image_url,
                    lambda data, i=item, r=results: self._set_result_icon(i, r, data),
                )
        self.w.podcastPages.setCurrentWidget(self.w.podcastSearchPage)

    def _set_result_icon(self, item: QListWidgetItem, results: list, data: bytes) -> None:
        if results is not self._search_results:
            return  # a newer search replaced these results
        image = QImage.fromData(data)
        if not image.isNull():
            item.setIcon(QIcon(square_pixmap(image, SEARCH_ICON)))

    def _on_result_clicked(self, item: QListWidgetItem) -> None:
        index = item.data(ID_ROLE)
        if 0 <= index < len(self._search_results):
            self.manager.open_feed(self._search_results[index].feed_url)

    # ----- one podcast -----

    def show_podcast(self, podcast_id: int) -> None:
        podcast = self.manager.store.podcast(podcast_id)
        if podcast is None:
            return
        w = self.w
        self.shown_podcast = podcast_id
        w.podcastPages.setCurrentWidget(w.podcastShowPage)
        w.podcastCover.setPixmap(self.cover_icon(podcast_id, COVER_SIZE * 2, rounded=False))
        self._title.set_text(podcast.title)
        self._author.set_text(podcast.author)
        self._description.set_text(plain_text(podcast.description))
        # Like the play button, it shows what clicking it does.
        w.podcastSubscribeButton.setIcon(
            self._icon("calendar-off" if podcast.subscribed else "calendar")
        )
        w.podcastSubscribeButton.setToolTip("Unsubscribe..." if podcast.subscribed else "Subscribe")
        self._showing = True
        self._keep_count.setValue(self.manager.keep_count(podcast))
        self._showing = False
        for widget in (w.podcastKeepLabel, w.podcastKeepCount):
            widget.setEnabled(podcast.subscribed)
        self._show_progress()
        self._fill_episodes(self.manager.store.episodes(podcast_id))
        # Select it in the list, if subscribed, without showing it again.
        view = w.podcastList
        view.blockSignals(True)
        for row in range(view.count()):
            if view.item(row).data(ID_ROLE) == podcast_id:
                view.setCurrentRow(row)
                break
        else:
            view.clearSelection()
            view.setCurrentItem(None)
        view.blockSignals(False)

    def _show_progress(self) -> None:
        progress = self.manager.store.progress(self.shown_podcast)
        w = self.w
        w.podcastProgress.setRange(0, max(1, progress.total))
        w.podcastProgress.setValue(progress.played)
        w.podcastProgressLabel.setText(f"{progress.played} of {progress.total} played")
        details = []
        if progress.started:
            details.append(f"{progress.started} started")
        if progress.time_left_ms:
            details.append(f"{format_length(progress.time_left_ms)} left")
        self._progress_details.set_text(" · ".join(["", *details]).strip() if details else "")

    def _toggle_subscription(self) -> None:
        podcast = self.manager.store.podcast(self.shown_podcast) if self.shown_podcast else None
        if podcast is None:
            return
        if podcast.subscribed:
            self._unsubscribe(podcast)
        else:
            self.manager.subscribe(podcast.id)
            self._show_status(f"Subscribed to {podcast.title}")

    def _unsubscribe(self, podcast: Podcast) -> None:
        box = QMessageBox(self.w)
        box.setIcon(QMessageBox.Icon.Question)
        box.setWindowTitle("Unsubscribe")
        box.setText(f"Unsubscribe from “{podcast.title}”?")
        box.setInformativeText(
            "Its downloaded episodes can be deleted or kept. "
            "Deleting includes the episodes marked Keep."
        )
        delete = box.addButton("Unsubscribe and Delete", QMessageBox.ButtonRole.DestructiveRole)
        keep = box.addButton("Unsubscribe and Keep", QMessageBox.ButtonRole.AcceptRole)
        delete.setIcon(self._icon("trash"))
        keep.setIcon(self._icon("calendar-off"))
        box.addButton(QMessageBox.StandardButton.Cancel)
        box.setDefaultButton(keep)
        box.exec()
        if box.clickedButton() not in (delete, keep):
            return  # Cancel, or the window was closed
        self.manager.unsubscribe(podcast.id, delete_downloads=box.clickedButton() is delete)
        self._show_status(f"Unsubscribed from {podcast.title}")

    def _on_keep_count_changed(self, value: int) -> None:
        if not self._showing and self.shown_podcast is not None:
            self.manager.set_keep_count(self.shown_podcast, value)

    # ----- the episodes -----

    def _fill_episodes(self, episodes: list[Episode]) -> None:
        table = self.w.podcastEpisodes
        scroll = table.verticalScrollBar().value()
        selected = set(self.selected_episodes())
        table.setUpdatesEnabled(False)
        table.clearContents()
        table.setRowCount(len(episodes))
        self._rows = {}
        for row, episode in enumerate(episodes):
            self._rows[episode.id] = row
            for column in range(table.columnCount()):
                table.setItem(row, column, QTableWidgetItem())
            self._show_episode(row, episode)
            if episode.id in selected:
                table.selectRow(row)
        table.setUpdatesEnabled(True)
        table.verticalScrollBar().setValue(scroll)

    def _show_episode(self, row: int, episode: Episode) -> None:
        table = self.w.podcastEpisodes
        items = [table.item(row, column) for column in range(table.columnCount())]
        if None in items:
            return
        title, released, length, progress, download = items
        playing = episode.id == self.playing_episode
        title.setText((PLAYING_MARK if playing else "") + episode.title)
        title.setData(ID_ROLE, episode.id)
        description = plain_text(episode.description)
        if len(description) > TOOLTIP_CHARACTERS:
            description = description[:TOOLTIP_CHARACTERS].rstrip() + "…"
        title.setToolTip(f"<b>{episode.title}</b><br>{description}" if description else episode.title)
        font = QFont(table.font())
        font.setBold(playing or (not episode.played and episode.position_ms == 0))
        title.setFont(font)
        released.setText(format_date(episode.published))
        length.setText(format_length(episode.duration_ms) if episode.duration_ms else "")

        if episode.played:
            progress.setText("Played")
            progress.setData(PROGRESS_ROLE, None)
        elif episode.position_ms > 0 and episode.duration_ms:
            progress.setText(f"{format_length(episode.time_left_ms)} left")
            progress.setData(PROGRESS_ROLE, episode.position_ms / episode.duration_ms)
        elif episode.position_ms > 0:
            progress.setText("Started")
            progress.setData(PROGRESS_ROLE, None)
        else:
            progress.setText("")
            progress.setData(PROGRESS_ROLE, None)

        download.setText(self._download_text(episode))
        colour = QColor(theme.colour(theme.TEXT))
        if episode.played and not playing:
            colour.setAlpha(DIMMED_ALPHA)
        for item in items:
            item.setForeground(colour)

    def _download_text(self, episode: Episode) -> str:
        fraction = self.manager.progress_of.get(episode.id)
        if fraction is not None:
            text = f"Downloading {round(fraction * 100)} %" if fraction >= 0 else "Downloading…"
            if fraction == 0 and not self.manager.is_active_download(episode.id):
                text = "Waiting to download"
        elif self.manager.file_path(episode) is not None:
            text = "Downloaded"
        else:
            text = ""
        if episode.keep:
            text = f"{text} · Keep" if text else "Keep"
        return text

    def update_episode(self, episode_id: int) -> None:
        row = self._rows.get(episode_id)
        episode = self.manager.store.episode(episode_id)
        if row is not None and episode is not None:
            self._show_episode(row, episode)
        if episode is not None and episode.podcast_id == self.shown_podcast:
            self._show_progress()

    def _on_download_progress(self, episode_id: int, _fraction: float) -> None:
        row = self._rows.get(episode_id)
        episode = self.manager.store.episode(episode_id) if row is not None else None
        if episode is not None:
            self.w.podcastEpisodes.item(row, DOWNLOAD).setText(self._download_text(episode))

    def set_playing(self, episode_id: int | None) -> None:
        """Mark the episode that is playing (or none)."""
        before, self.playing_episode = self.playing_episode, episode_id
        for changed in {before, episode_id} - {None}:
            self.update_episode(changed)

    def selected_episodes(self) -> list[int]:
        table = self.w.podcastEpisodes
        rows = sorted({index.row() for index in table.selectionModel().selectedRows()})
        return [table.item(row, TITLE).data(ID_ROLE) for row in rows if table.item(row, TITLE)]

    def _on_episode_activated(self, index) -> None:
        item = self.w.podcastEpisodes.item(index.row(), TITLE)
        if item is not None:
            self.play_requested.emit(item.data(ID_ROLE))

    def _show_episode_menu(self, position: QPoint) -> None:
        table = self.w.podcastEpisodes
        clicked = table.itemAt(position)
        if clicked is None:
            return
        if not table.item(clicked.row(), TITLE).isSelected():
            table.selectRow(clicked.row())
        ids = self.selected_episodes()
        episodes = [e for e in (self.manager.store.episode(i) for i in ids) if e is not None]
        if not episodes:
            return
        manager = self.manager
        menu = QMenu(table)
        first = episodes[0]
        if len(episodes) == 1:
            menu.addAction(self._icon("play"), "Play").triggered.connect(
                lambda _checked=False: self.play_requested.emit(first.id)
            )
            menu.addSeparator()
        downloading = [e for e in episodes if manager.is_downloading(e.id)]
        downloaded = [e for e in episodes if manager.file_path(e) is not None]
        missing = [e for e in episodes if e not in downloaded and e not in downloading]
        if missing:
            menu.addAction(self._icon("download"), "Download").triggered.connect(
                lambda _checked=False: [manager.download(e.id) for e in missing]
            )
        if downloading:
            menu.addAction(self._icon("circle-x"), "Cancel Download").triggered.connect(
                lambda _checked=False: [manager.cancel_download(e.id) for e in downloading]
            )
        if downloaded:
            menu.addAction(self._icon("trash"), "Delete Download").triggered.connect(
                lambda _checked=False: self._delete_downloads(downloaded)
            )
        menu.addSeparator()
        if any(not e.played for e in episodes):
            menu.addAction(self._icon("check"), "Mark as Played").triggered.connect(
                lambda _checked=False: manager.set_played(ids, True)
            )
        if any(e.played or e.position_ms for e in episodes):
            menu.addAction(self._icon("rotate-ccw"), "Mark as Unplayed").triggered.connect(
                lambda _checked=False: manager.set_played(ids, False)
            )
        # Like the other items, it says what clicking it does.
        all_kept = all(e.keep for e in episodes)
        if all_kept:
            keep = menu.addAction(self._icon("pin-off"), "Stop Keeping (Delete Automatically Again)")
        else:
            keep = menu.addAction(self._icon("pin"), "Keep (Never Delete Automatically)")
        keep.triggered.connect(lambda _checked=False: manager.set_keep(ids, not all_kept))
        if len(episodes) == 1 and first.link:
            menu.addSeparator()
            menu.addAction(self._icon("globe"), "Open Episode Web Page").triggered.connect(
                lambda _checked=False: QDesktopServices.openUrl(QUrl(first.link))
            )
        menu.exec(table.viewport().mapToGlobal(position))

    def _delete_downloads(self, episodes: list[Episode]) -> None:
        for episode in episodes:
            if episode.id == self.playing_episode:
                self._show_status("The episode that is playing cannot be deleted")
                continue
            self.manager.delete_download(episode.id)


class PodcastSettingsTab:
    """Connects the Podcasts tab of settings.ui. Every change applies at once."""

    def __init__(self, dialog: QDialog, manager: PodcastManager) -> None:
        self._dialog = d = dialog
        self._manager = manager
        self._keep = NumberField(d.podcastKeepCountDefault)
        self._minutes = NumberField(d.podcastCheckMinutes)
        self._days = NumberField(d.podcastDeleteOldDays)
        self._gigabytes = NumberField(d.podcastSizeLimitGb)
        self._showing = True
        s = manager.settings
        self._show_folder(s)
        self._keep.setValue(s.keep_count)
        self._minutes.setValue(s.check_minutes)
        d.podcastContinue.setChecked(s.continue_with_next)
        d.podcastDeletePlayed.setChecked(s.delete_played)
        d.podcastDeleteOld.setChecked(s.delete_old)
        self._days.setValue(s.delete_old_days)
        d.podcastSizeLimit.setChecked(s.size_limit)
        self._gigabytes.setValue(s.size_limit_gb)
        self._showing = False
        self._show_enabled()

        for field in (self._keep, self._minutes, self._days, self._gigabytes):
            field.valueChanged.connect(lambda _value: self._changed())
        for checkbox in (d.podcastContinue, d.podcastDeletePlayed, d.podcastDeleteOld, d.podcastSizeLimit):
            checkbox.toggled.connect(lambda _on: self._changed())
        d.podcastFolderButton.clicked.connect(lambda _checked=False: self._choose_folder())
        d.podcastFolderReset.clicked.connect(lambda _checked=False: self._set_folder(""))

    def _show_folder(self, settings: PodcastSettings) -> None:
        self._dialog.podcastFolder.setText(str(settings.download_folder()))
        self._dialog.podcastFolderReset.setEnabled(bool(settings.folder))

    def _show_enabled(self) -> None:
        d = self._dialog
        d.podcastDeleteOldDays.setEnabled(d.podcastDeleteOld.isChecked())
        d.podcastSizeLimitGb.setEnabled(d.podcastSizeLimit.isChecked())

    def _choose_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(
            self._dialog, "Folder for podcast downloads",
            str(self._manager.settings.download_folder()),
        )
        if folder:
            self._set_folder(folder)

    def _set_folder(self, folder: str) -> None:
        manager = self._manager
        if Path(folder or ".") == Path(manager.settings.folder or ".") and bool(folder) == bool(
            manager.settings.folder
        ):
            return
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            failed = manager.change_folder(folder)
        finally:
            QApplication.restoreOverrideCursor()
        if failed:
            QMessageBox.warning(
                self._dialog, "Podcast folder",
                f"{failed} download{'' if failed == 1 else 's'} could not be moved to the new folder.",
            )
        self._show_folder(manager.settings)

    def _changed(self) -> None:
        if self._showing:
            return
        d = self._dialog
        self._show_enabled()
        s = self._manager.settings
        self._manager.apply_settings(PodcastSettings(
            folder=s.folder,
            check_minutes=self._minutes.value(),
            keep_count=self._keep.value(),
            continue_with_next=d.podcastContinue.isChecked(),
            delete_played=d.podcastDeletePlayed.isChecked(),
            delete_old=d.podcastDeleteOld.isChecked(),
            delete_old_days=self._days.value(),
            size_limit=d.podcastSizeLimit.isChecked(),
            size_limit_gb=self._gigabytes.value(),
        ))
