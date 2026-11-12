"""The tag editor window (tag_edit.ui), for one song or several at once.

With several songs, a field whose songs disagree starts empty and is only
written if the user types in it, so nothing changes by accident.
"""

from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

from PySide6.QtCore import QSettings, QStandardPaths, Qt
from PySide6.QtGui import QIcon, QImage, QPixmap
from PySide6.QtWidgets import QDialog, QFileDialog, QMessageBox, QTableWidgetItem, QWidget

from src.lastfm_dialog import load_dialog
from src.online_search import (
    LYRICS_PREFERENCE_KEY,
    ONLY_TIMED,
    PREFER_TIMED,
    OnlineSearch,
    compare_covers,
    compare_genres,
    HAS_LYRICS_TAG,
    is_instrumental,
    pick_lyrics,
)
from src.playlist_dialog import chosen_preset, fill_preset_combo
from src.playlist_models import ClickFilter, make_square_icon
from src.song_settings import SoundPresets
from src.star_rating import StarRating
from src.tag_writer import TagJob
from src.track_metadata import (
    PROTECTED_TAGS,
    EditableFile,
    TagChanges,
    clean_tag_name,
    read_editable_file,
    read_track_info,
)

# Each has a widget called tagField_<name> in tag_edit.ui.
FIELD_NAMES = (
    "title", "artist", "albumartist", "album", "date",
    "tracknumber", "tracktotal", "discnumber", "disctotal",
    "publisher", "composer", "conductor", "genre", "comment",
    "originalartist", "originalalbum", "originaldate", "keywords", "occasion", "mood",
    "language", "grouping", "subtitle", "bpm", "initialkey",
    "lyricist", "remixer", "copyright", "encodedby", "isrc",
)
MULTIPLE_VALUES = "(multiple values)"
LRC_SOURCE, EMBEDDED_SOURCE = 0, 1  # the entries of tagEditLyricsSource, in order
IMAGE_PREVIEW_SIDE = 320


def _shared(values: list) -> tuple[bool, object]:
    """Whether all values are equal, and that value."""
    return all(value == values[0] for value in values), values[0]


class TagEditorDialog:
    """Connects the widgets of tag_edit.ui. Nothing is written here: run() returns
    what should change, and the caller has it written."""

    def __init__(
        self,
        ui_path: Path,
        parent: QWidget,
        paths: list[Path],
        files: list[EditableFile],
        placeholder: QIcon,
        empty_star: QIcon,
        full_star: QIcon,
        online: OnlineSearch,
        siblings: list[Path] | None = None,
        load_presets: Callable[[list[Path]], dict[Path, SoundPresets]] | None = None,
        equaliser_names: list[str] | None = None,
        visualiser_names: list[str] | None = None,
    ) -> None:
        """`siblings` are the songs the arrow buttons go through, one song at a time.
        `load_presets` reads songs' presets (the Settings tab); `equaliser_names`
        and `visualiser_names` are the presets to choose from."""
        self._paths = paths
        self._files = files
        self._placeholder = placeholder
        self._ui_folder = ui_path.parent
        self._online = online
        self._cover_search: int | None = None  # the request being waited for
        self._genre_search: int | None = None
        self._lyrics_search: int | None = None
        self._settings = QSettings("MusicHive", "MusicHive")
        self._pending: dict[Path, TagJob] = {}  # what to write, by song
        self._jobs: list[TagJob] = []
        self._seen = dict(zip(paths, files))  # songs as read, with the edits so far
        self._load_presets = load_presets or (lambda _paths: {})
        self._equaliser_names = equaliser_names or []
        self._visualiser_names = visualiser_names or []
        self._presets: dict[Path, SoundPresets] = {}  # as stored, with the edits so far
        self.preset_changes: dict[Path, SoundPresets] = {}  # what to store, after Save
        self._siblings = siblings if len(paths) == 1 and paths[0] in (siblings or []) else []
        self._position = self._siblings.index(paths[0]) if self._siblings else 0
        self.dialog = load_dialog(ui_path, parent)
        d = self.dialog

        self._image_click = ClickFilter(d.tagEditImage)
        self._image_click.clicked.connect(self._choose_image)
        d.tagEditChooseImageButton.clicked.connect(self._choose_image)
        d.tagEditRemoveImageButton.clicked.connect(self._remove_image)
        self._stars = StarRating(d.tagEditRating, empty_star, full_star)
        self._stars.rated.connect(self._on_rated)
        d.tagEditLyricsSource.currentIndexChanged.connect(self._on_lyrics_source_changed)
        d.tagEditAddTagButton.clicked.connect(lambda _checked=False: self._add_tag_row())
        d.tagEditRemoveTagButton.clicked.connect(lambda _checked=False: self._remove_tag_rows())
        d.tagEditPreviousButton.clicked.connect(lambda _checked=False: self._go_to_song(-1))
        d.tagEditNextButton.clicked.connect(lambda _checked=False: self._go_to_song(1))
        d.tagEditPreviousButton.setVisible(bool(self._siblings))
        d.tagEditNextButton.setVisible(bool(self._siblings))
        d.tagEditButtonBox.accepted.connect(self._on_save)
        d.tagEditButtonBox.rejected.connect(d.reject)
        self._find_button_text = d.tagEditFindImageButton.text()
        d.tagEditFindImageButton.clicked.connect(lambda _checked=False: self._find_covers())
        d.tagEditFindGenreButton.clicked.connect(lambda _checked=False: self._find_genres())
        d.tagEditFindLyricsButton.clicked.connect(lambda _checked=False: self._find_lyrics())
        d.tagEditLyricsPreference.setCurrentIndex(
            self._settings.value(LYRICS_PREFERENCE_KEY, PREFER_TIMED, type=int)
        )
        d.tagEditLyricsPreference.currentIndexChanged.connect(
            lambda index: self._settings.setValue(LYRICS_PREFERENCE_KEY, index)
        )
        online.covers_found.connect(self._on_covers_found)
        online.genres_found.connect(self._on_genres_found)
        online.lyrics_found.connect(self._on_lyrics_found)
        self._show_songs()

    def _show_songs(self) -> None:
        """Fill the window from the current song or songs."""
        d = self.dialog
        if self._siblings:
            d.setWindowTitle(f"Edit tags ({self._position + 1} of {len(self._siblings)})")
            d.tagEditPreviousButton.setEnabled(self._position > 0)
            d.tagEditNextButton.setEnabled(self._position < len(self._siblings) - 1)
        else:
            count = len(self._paths)
            d.setWindowTitle("Edit tags" if count == 1 else f"Edit tags of {count} songs")

        self._initial: dict[str, str] = {}
        for name in FIELD_NAMES:
            self._fill_field(name)
        d.tagEditOtherTable.setRowCount(0)
        self._fill_other_tags()

        same_cover, cover = _shared([file.cover for file in self._files])
        self._cover = cover if same_cover else None
        self._cover_changed = False
        self._any_cover = any(file.cover for file in self._files)
        self._show_cover()

        same_rating, rating = _shared([file.rating for file in self._files])
        self._rating = rating if same_rating else 0
        self._rating_changed = False
        self._stars.set_rating(self._rating)
        self._show_lyrics()
        self._show_presets()
        self._cover_search = self._genre_search = None  # those results were for another song
        self._set_searching(d.tagEditFindImageButton, False)
        self._set_searching(d.tagEditFindGenreButton, False)

    def _go_to_song(self, step: int) -> None:
        """Keep the edits made so far, and show the next or previous song."""
        if not self._remember_changes():
            return
        self._position += step
        path = self._siblings[self._position]
        if path not in self._seen:
            self._seen[path] = read_editable_file(path)
        self._paths = [path]
        self._files = [self._seen[path]]
        self._show_songs()

    def run(self) -> list[TagJob]:
        """Show the window. Returns what to write; empty if nothing is to be done.
        The presets to store are in preset_changes afterwards."""
        d = self.dialog
        if d.exec() != QDialog.DialogCode.Accepted:
            self.preset_changes = {}  # noted while going through songs, then cancelled
        self._online.covers_found.disconnect(self._on_covers_found)
        self._online.genres_found.disconnect(self._on_genres_found)
        d.deleteLater()
        return self._jobs

    # ----- fields -----

    def _field(self, name: str) -> QWidget:
        return getattr(self.dialog, f"tagField_{name}")

    def _shown_value(self, name: str) -> tuple[str, bool]:
        """The text all songs share for a tag, and whether they do share one."""
        same, value = _shared([file.tags.get(name, "") for file in self._files])
        return (value if same else ""), same

    def _fill_field(self, name: str) -> None:
        text, same = self._shown_value(name)
        self._initial[name] = text
        widget = self._field(name)
        if hasattr(widget, "setPlainText"):
            widget.setPlainText(text)
        else:
            widget.setText(text)
        if not same:
            widget.setPlaceholderText(MULTIPLE_VALUES)

    def _field_text(self, name: str) -> str:
        widget = self._field(name)
        return widget.toPlainText() if hasattr(widget, "toPlainText") else widget.text()

    # ----- other tags -----

    def _fill_other_tags(self) -> None:
        names = sorted({name for file in self._files for name in file.tags})
        self._removed_tags: set[str] = set()
        for name in names:
            if name not in FIELD_NAMES and name != "lyrics":
                text, same = self._shown_value(name)
                self._initial[name] = text
                self._add_tag_row(name, text, same)

    def _add_tag_row(self, name: str = "", text: str = "", same: bool = True) -> None:
        table = self.dialog.tagEditOtherTable
        row = table.rowCount()
        table.insertRow(row)
        name_item = QTableWidgetItem(name)
        name_item.setData(Qt.ItemDataRole.UserRole, name)  # "" marks a new tag
        if name:
            name_item.setFlags(name_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
        value_item = QTableWidgetItem(text)
        if not same:
            value_item.setToolTip(MULTIPLE_VALUES)
        table.setItem(row, 0, name_item)
        table.setItem(row, 1, value_item)
        if not name:
            table.setCurrentCell(row, 0)
            table.editItem(name_item)

    def _set_other_tag(self, name: str, text: str) -> None:
        """Put a value in the other tags table, in the tag's row or a new one."""
        table = self.dialog.tagEditOtherTable
        for row in range(table.rowCount()):
            name_item = table.item(row, 0)
            if name_item is None:
                continue
            old_name = name_item.data(Qt.ItemDataRole.UserRole)
            if (old_name or clean_tag_name(name_item.text())) == name:
                value_item = table.item(row, 1)
                if value_item is None:
                    table.setItem(row, 1, QTableWidgetItem(text))
                else:
                    value_item.setText(text)
                    value_item.setToolTip("")
                return
        self._removed_tags.discard(name)
        row = table.rowCount()
        table.insertRow(row)
        name_item = QTableWidgetItem(name)
        name_item.setData(Qt.ItemDataRole.UserRole, "")  # new: saved as a new tag
        table.setItem(row, 0, name_item)
        table.setItem(row, 1, QTableWidgetItem(text))

    def _remove_tag_rows(self) -> None:
        table = self.dialog.tagEditOtherTable
        for row in sorted({index.row() for index in table.selectedIndexes()}, reverse=True):
            name = table.item(row, 0).data(Qt.ItemDataRole.UserRole)
            if name:
                self._removed_tags.add(name)
            table.removeRow(row)

    # ----- settings: the song's presets -----

    def _show_presets(self) -> None:
        d = self.dialog
        unread = [path for path in self._paths if path not in self._presets]
        if unread:
            loaded = self._load_presets(unread)
            for path in unread:
                self._presets[path] = loaded.get(path, SoundPresets())
        shown = [self._presets[path] for path in self._paths]
        for combo, names, values in (
            (d.tagEditEqualiser, self._equaliser_names, [p.equaliser for p in shown]),
            (d.tagEditVisualiser, self._visualiser_names, [p.visualiser for p in shown]),
        ):
            same, value = _shared(values)
            fill_preset_combo(combo, names, value if same else "")
            if not same:
                for other in values:  # keep every song's preset on the list
                    if other and combo.findText(other) < 0:
                        combo.addItem(other)
                combo.setCurrentIndex(-1)  # shows "(multiple values)"
        same, over = _shared([p.over_playlist for p in shown])
        box = d.tagEditOverPlaylist
        box.setTristate(not same)
        if same:
            box.setCheckState(Qt.CheckState.Checked if over else Qt.CheckState.Unchecked)
        else:
            box.setCheckState(Qt.CheckState.PartiallyChecked)
        self._presets_initial = (
            d.tagEditEqualiser.currentIndex(),
            d.tagEditVisualiser.currentIndex(),
            box.checkState(),
        )

    def _remember_presets(self) -> None:
        """Note the presets the user changed, for every song on screen."""
        d = self.dialog
        equaliser, visualiser, over = self._presets_initial
        changes = {}
        if d.tagEditEqualiser.currentIndex() != equaliser:
            changes["equaliser"] = chosen_preset(d.tagEditEqualiser)
        if d.tagEditVisualiser.currentIndex() != visualiser:
            changes["visualiser"] = chosen_preset(d.tagEditVisualiser)
        state = d.tagEditOverPlaylist.checkState()
        if state != over and state != Qt.CheckState.PartiallyChecked:
            changes["over_playlist"] = state == Qt.CheckState.Checked
        if not changes:
            return
        for path in self._paths:
            self._presets[path] = replace(self._presets[path], **changes)
            self.preset_changes[path] = self._presets[path]

    # ----- image and rating -----

    def _show_cover(self) -> None:
        d = self.dialog
        image = QImage.fromData(self._cover) if self._cover else QImage()
        if image.isNull():
            pixmap = self._placeholder.pixmap(IMAGE_PREVIEW_SIDE, IMAGE_PREVIEW_SIDE)
        else:
            pixmap = QPixmap.fromImage(make_square_icon(image, IMAGE_PREVIEW_SIDE))
        d.tagEditImage.setPixmap(pixmap)
        d.tagEditRemoveImageButton.setEnabled(bool(self._cover) or self._any_cover)

    def _choose_image(self) -> None:
        start_dir = QStandardPaths.writableLocation(QStandardPaths.PicturesLocation)
        path, _ = QFileDialog.getOpenFileName(
            self.dialog, "Choose an image", start_dir, "Images (*.png *.jpg *.jpeg)"
        )
        if not path:
            return
        data = Path(path).read_bytes()
        if QImage.fromData(data).isNull():
            QMessageBox.warning(self.dialog, "Image", "That image could not be read.")
            return
        self._cover = data
        self._cover_changed = True
        self._show_cover()

    def _remove_image(self) -> None:
        self._cover = None
        self._cover_changed = True
        self._any_cover = False
        self._show_cover()

    def _on_rated(self, rating: int) -> None:
        self._rating = rating
        self._rating_changed = True
        self._stars.set_rating(rating)

    # ----- searching the internet -----

    def _set_searching(self, button: QWidget, searching: bool) -> None:
        button.setEnabled(not searching)
        button.setText("Searching…" if searching else self._find_button_text)

    def _search_artist(self) -> str:
        return self._field_text("albumartist").strip() or self._field_text("artist").strip()

    def _find_covers(self) -> None:
        album = self._field_text("album").strip()
        if not album:
            QMessageBox.information(self.dialog, "Find image", "Enter the album's name first.")
            return
        self._set_searching(self.dialog.tagEditFindImageButton, True)
        self._cover_search = self._online.find_covers(self._search_artist(), album)

    def _on_covers_found(self, request: int, covers: list, error: str) -> None:
        if request != self._cover_search:
            return
        self._cover_search = None
        self._set_searching(self.dialog.tagEditFindImageButton, False)
        if error or not covers:
            text = f"The search failed: {error}" if error else "No image was found for this album."
            QMessageBox.information(self.dialog, "Find image", text)
            return
        chosen = compare_covers(
            self._ui_folder / "cover_compare.ui", self.dialog, self._cover, covers
        )
        if chosen is not None:
            self._cover = chosen.data
            self._cover_changed = True
            self._show_cover()

    def _find_genres(self) -> None:
        artist = self._field_text("artist").strip() or self._field_text("albumartist").strip()
        if not artist:
            QMessageBox.information(self.dialog, "Find genres", "Enter the artist's name first.")
            return
        self._set_searching(self.dialog.tagEditFindGenreButton, True)
        self._genre_search = self._online.find_genres(
            artist, self._field_text("title").strip(), self._field_text("album").strip()
        )

    def _on_genres_found(self, request: int, genres: list, error: str) -> None:
        if request != self._genre_search:
            return
        self._genre_search = None
        self._set_searching(self.dialog.tagEditFindGenreButton, False)
        if error or not genres:
            text = f"The search failed: {error}" if error else "No genres were found."
            QMessageBox.information(self.dialog, "Find genres", text)
            return
        chosen = compare_genres(
            self._ui_folder / "genre_compare.ui",
            self.dialog,
            self._field_text("genre").strip(),
            genres,
        )
        if chosen is not None:
            self._field("genre").setText("; ".join(chosen))

    # ----- lyrics -----

    def _find_lyrics(self) -> None:
        title = self._field_text("title").strip()
        artist = self._field_text("artist").strip() or self._field_text("albumartist").strip()
        if not title or not artist:
            QMessageBox.information(self.dialog, "Find lyrics", "Enter the title and artist first.")
            return
        try:
            duration_ms = read_track_info(self._paths[0])[0].duration_ms or 0
        except Exception:  # an unreadable file: search without its length
            duration_ms = 0
        self._set_searching(self.dialog.tagEditFindLyricsButton, True)
        self._lyrics_search = self._online.find_lyrics(
            artist, title, self._field_text("album").strip(), duration_ms / 1000
        )

    def _on_lyrics_found(self, request: int, found: list, error: str) -> None:
        if request != self._lyrics_search:
            return
        self._lyrics_search = None
        d = self.dialog
        self._set_searching(d.tagEditFindLyricsButton, False)
        if error:
            QMessageBox.information(d, "Find lyrics", f"The search failed: {error}")
            return
        preference = d.tagEditLyricsPreference.currentIndex()
        text = pick_lyrics(found, preference)
        if text is None:
            if is_instrumental(found):
                self._set_other_tag(HAS_LYRICS_TAG, "false")
                message = (
                    "LRCLIB lists this song as an instrumental: it has no lyrics. "
                    f"Its “{HAS_LYRICS_TAG}” tag was set to false (saved with Save)."
                )
            elif found and preference == ONLY_TIMED:
                message = "Only plain lyrics were found for this song, no timed ones."
            else:
                message = "No lyrics were found for this song."
            QMessageBox.information(d, "Find lyrics", message)
            return
        current = d.tagEditLyrics.toPlainText().strip()
        if current and current != text.strip():
            answer = QMessageBox.question(
                d, "Find lyrics", "Replace the lyrics shown with the ones found?"
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        d.tagEditLyrics.setPlainText(text)
        self._set_other_tag(HAS_LYRICS_TAG, "true")

    def _show_lyrics(self) -> None:
        """Lyrics belong to one song, so with several the tab is switched off."""
        d = self.dialog
        if len(self._files) > 1:
            d.tagEditTabs.setTabEnabled(d.tagEditTabs.indexOf(d.tagEditLyricsTab), False)
            self._lyrics_initial = self._lyrics = None
            return
        file = self._files[0]
        self._lyrics_initial = [file.lrc_text or "", file.tags.get("lyrics", "")]
        self._lyrics = list(self._lyrics_initial)
        self._lyrics_source = LRC_SOURCE if file.lrc_text is not None else EMBEDDED_SOURCE
        d.tagEditLyricsSource.blockSignals(True)  # the text below is not an edit
        d.tagEditLyricsSource.setCurrentIndex(self._lyrics_source)
        d.tagEditLyricsSource.blockSignals(False)
        d.tagEditLyrics.setPlainText(self._lyrics[self._lyrics_source])

    def _on_lyrics_source_changed(self, source: int) -> None:
        d = self.dialog
        self._lyrics[self._lyrics_source] = d.tagEditLyrics.toPlainText()
        self._lyrics_source = source
        d.tagEditLyrics.setPlainText(self._lyrics[source])

    # ----- saving -----

    def _changed_tags(self) -> dict[str, str] | None:
        """The tags the user changed, or None if a new tag has a problem."""
        changes = {
            name: self._field_text(name).strip()
            for name in FIELD_NAMES
            if self._field_text(name).strip() != self._initial[name]
        }
        changes.update({name: "" for name in self._removed_tags})
        table = self.dialog.tagEditOtherTable
        for row in range(table.rowCount()):
            old_name = table.item(row, 0).data(Qt.ItemDataRole.UserRole)
            value = (table.item(row, 1).text() if table.item(row, 1) else "").strip()
            if old_name:
                if value != self._initial[old_name]:
                    changes[old_name] = value
                continue
            name = clean_tag_name(table.item(row, 0).text())
            if not name and not value:
                continue  # an empty row that was added and left alone
            if not name or name in PROTECTED_TAGS:
                QMessageBox.warning(
                    self.dialog, "Tag name", f"'{table.item(row, 0).text()}' can't be used as a tag name."
                )
                return None
            changes[name] = value
        return changes

    def _remember_changes(self) -> bool:
        """Note the edits to the songs on screen. False if a new tag has a problem."""
        d = self.dialog
        tags = self._changed_tags()
        if tags is None:
            return False
        self._remember_presets()
        lrc_text = None
        if self._lyrics is not None:
            self._lyrics[self._lyrics_source] = d.tagEditLyrics.toPlainText()
            if self._lyrics[LRC_SOURCE] != self._lyrics_initial[LRC_SOURCE]:
                lrc_text = self._lyrics[LRC_SOURCE]
            if self._lyrics[EMBEDDED_SOURCE].strip() != self._lyrics_initial[EMBEDDED_SOURCE]:
                tags["lyrics"] = self._lyrics[EMBEDDED_SOURCE].strip()
        rating = self._rating if self._rating_changed else None
        for path, file in zip(self._paths, self._files):
            self._add_to_job(path, tags, rating, lrc_text)
            for name, text in tags.items():
                if text:
                    file.tags[name] = text
                else:
                    file.tags.pop(name, None)
            if self._cover_changed:
                file.cover = self._cover
            if rating is not None:
                file.rating = rating
            if lrc_text is not None:
                file.lrc_text = lrc_text if lrc_text.strip() else None
        return True

    def _add_to_job(
        self, path: Path, tags: dict[str, str], rating: int | None, lrc_text: str | None
    ) -> None:
        job = self._pending.setdefault(path, TagJob(path, TagChanges()))
        job.changes.tags.update(tags)
        if self._cover_changed:
            job.changes.cover = self._cover
            job.changes.cover_changed = True
        if rating is not None:
            job.changes.rating = rating
        if lrc_text is not None:
            job.lrc_text = lrc_text

    def _on_save(self) -> None:
        if self._remember_changes():
            self._jobs = [
                job
                for job in self._pending.values()
                if not job.changes.is_empty or job.lrc_text is not None
            ]
            self.dialog.accept()
