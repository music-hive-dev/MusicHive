"""The Last.fm window (lastfm.ui): log in or out, and download play counts."""



from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import QDir, QFile, QIODevice
from PySide6.QtUiTools import QUiLoader
from PySide6.QtWidgets import QFileDialog, QMessageBox, QPushButton, QWidget

from src import theme
from src.number_field import NumberField
from src.lastfm import LastFmClient


def load_dialog(path: Path, parent: QWidget):
    ui_file = QFile(str(path))
    if not ui_file.open(QIODevice.ReadOnly):
        raise RuntimeError(f"Cannot open {path}: {ui_file.errorString()}")
    try:
        loader = QUiLoader()
        loader.setWorkingDirectory(QDir(str(theme.ui_dir(path.parent))))
        dialog = loader.load(ui_file, parent)
    finally:
        ui_file.close()
    if dialog is None:
        raise RuntimeError(f"Failed to load {path}: {loader.errorString()}")
    theme.apply(dialog)
    return dialog


class LastFmDialog:
    """Connects the widgets of lastfm.ui to the Last.fm client."""

    def __init__(
        self,
        ui_path: Path,
        client: LastFmClient,
        parent: QWidget,
        apply_counts: Callable[[dict], tuple[int, int]],
    ) -> None:
        self.client = client
        self._apply_counts = apply_counts
        self.dialog = load_dialog(ui_path, parent)
        d = self.dialog
        self._login_text = d.lastFmLoginButton.text()

        d.lastFmLoginButton.clicked.connect(self._on_login_clicked)
        d.lastFmLogoutButton.clicked.connect(client.logout)
        d.lastFmRememberCheckBox.toggled.connect(client.set_remember)
        d.lastFmDownloadButton.clicked.connect(self._on_download_clicked)
        d.lastFmImportButton.clicked.connect(self._on_import_clicked)
        self._backlog_per_day = NumberField(d.lastFmBacklogPerDay)
        self._backlog_per_day.setValue(client.backlog_per_day)
        self._backlog_per_day.valueChanged.connect(client.set_backlog_per_day)
        # Enter confirms a typed number; it must not press a button as well.
        for button in d.findChildren(QPushButton):
            button.setAutoDefault(False)
        d.lastFmButtonBox_2.rejected.connect(d.reject)

        client.state_changed.connect(self.refresh)
        client.counts_progress.connect(self._on_progress)
        client.counts_ready.connect(self._on_counts_ready)
        client.counts_failed.connect(self._on_counts_failed)
        self.refresh()

    def open_dialog(self) -> None:
        self.refresh()
        self.dialog.exec()

    def refresh(self) -> None:
        """Show the current login state."""
        d = self.dialog
        client = self.client
        state = client.login_state

        if not client.is_configured:
            status = (
                "Last.fm is not set up yet. Add your API key and secret to the secrets "
                "file (Settings, Online, Go to Secrets File), then restart MusicHive."
            )
        elif state == "in":
            status = f"Logged in as {client.username}."
        elif state == "waiting":
            status = "Waiting for you to allow MusicHive in your browser…"
        else:
            status = "Not logged in."
        d.lastFmStatusLabel.setText(status)

        d.lastFmRememberCheckBox.blockSignals(True)
        d.lastFmRememberCheckBox.setChecked(client.remember)
        d.lastFmRememberCheckBox.blockSignals(False)

        d.lastFmLoginButton.setText("Cancel" if state == "waiting" else self._login_text)
        d.lastFmLoginButton.setEnabled(client.is_configured and state != "in")
        d.lastFmLogoutButton.setEnabled(state == "in")
        d.lastFmDownloadButton.setEnabled(state == "in" and not client.is_downloading)
        d.lastFmImportButton.setEnabled(state == "in")
        waiting = client.backlog_count
        if waiting:
            days = -(-waiting // client.backlog_per_day)  # rounded up
            d.lastFmBacklogLabel.setText(
                f"{waiting} imported plays waiting: about {days} day{'' if days == 1 else 's'} "
                "with MusicHive open."
            )
        else:
            d.lastFmBacklogLabel.setText("")

    # ----- buttons -----

    def _on_login_clicked(self) -> None:
        if self.client.login_state == "waiting":
            self.client.cancel_login()
        else:
            self.client.start_login()

    def _on_import_clicked(self) -> None:
        d = self.dialog
        chosen, _used_filter = QFileDialog.getOpenFileName(
            d, "Import scrobble log", "", "Scrobble logs (*.log);;All files (*)"
        )
        if not chosen:
            return
        try:
            result = self.client.import_scrobble_log(Path(chosen))
        except OSError as error:
            QMessageBox.warning(d, "Import scrobble log", f"Could not read the file:\n{error}")
            return
        notes = [f"Added {result.added} plays."]
        if result.already_imported:
            notes.append(f"{result.already_imported} were imported before and left out.")
        if result.skipped_on_player:
            notes.append(f"{result.skipped_on_player} were skipped on the player and left out.")
        if result.unreadable:
            notes.append(f"{result.unreadable} lines could not be read.")
        QMessageBox.information(d, "Import scrobble log", "\n".join(notes))

    def _on_download_clicked(self) -> None:
        d = self.dialog
        pending = self.client.pending_count
        if pending:
            self.client.flush()
            QMessageBox.information(
                d,
                "Scrobbles still waiting",
                f"{pending} scrobble(s) have not been sent to Last.fm yet. "
                "Please try again in a moment, so that no recent plays are lost.",
            )
            return

        box = QMessageBox(
            QMessageBox.Icon.Warning,
            "Replace play counts?",
            "This replaces the play counts stored in MusicHive with the counts "
            "from your Last.fm account, for every song Last.fm knows about. "
            "This can lower a count if Last.fm's number is lower.\n\n"
            "Songs with no plays on Last.fm keep their local play count.\n\n"
            "This cannot be undone.",
            parent=d,
        )
        download = box.addButton("Download and replace", QMessageBox.ButtonRole.AcceptRole)
        cancel = box.addButton(QMessageBox.StandardButton.Cancel)
        box.setDefaultButton(cancel)
        box.exec()
        if box.clickedButton() is not download:
            return

        d.lastFmSyncLabel.setText("Downloading play counts…")
        self.client.download_play_counts()

    # ----- play count download -----

    def _on_progress(self, page: int, total_pages: int) -> None:
        self.dialog.lastFmSyncLabel.setText(
            f"Downloading play counts… page {page} of {total_pages}"
        )

    def _on_counts_ready(self, counts: dict) -> None:
        label = self.dialog.lastFmSyncLabel
        label.setText("Applying play counts…")
        updated, libraries = self._apply_counts(counts)
        if updated:
            label.setText(
                f"Done. Updated {updated} songs in {libraries} "
                f"librar{'y' if libraries == 1 else 'ies'} "
                f"({len(counts)} songs on Last.fm)."
            )
        else:
            label.setText(
                f"Done, but no songs matched the {len(counts)} songs on Last.fm."
            )

    def _on_counts_failed(self, message: str) -> None:
        self.dialog.lastFmSyncLabel.setText(f"Download failed: {message}")