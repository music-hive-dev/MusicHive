"""The history of tag changes, so that they can be undone.

Just before the tag writer changes a file, it notes what the file had: the old
text of each tag that is about to change, and the old picture, rating or .lrc
file if those change too (snapshot). Once a save is finished, its notes are
kept as one entry in the data folder, under tag-history/<id>/: entry.json, and
the old pictures as separate files beside it. Only the newest MAX_ENTRIES are
kept.

Undoing an entry writes the old values back, through the tag writer like any
other change, so the undo is an entry of its own and can be undone in turn.
"""

import json
import os
import shutil
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from src.library_db import data_dir
from src.track_metadata import TagChanges, read_editable_file

MAX_ENTRIES = 50
FOLDER_NAME = "tag-history"
ENTRY_FILE = "entry.json"

_lock = threading.Lock()  # entries are written on the tag writer's thread


def history_dir() -> Path:
    return data_dir() / FOLDER_NAME


@dataclass
class Before:
    """What one file had before a change, for the parts the change touches."""

    path: Path
    tags: dict[str, str] = field(default_factory=dict)  # old text; "" if the tag was missing
    cover_changed: bool = False
    cover: bytes | None = None  # the old embedded picture, if cover_changed
    rating: int | None = None  # the old rating, if the change set one
    lrc_changed: bool = False
    lrc_text: str | None = None  # the old .lrc file, if lrc_changed; None if there was none


@dataclass
class HistoryEntry:
    id: str
    when: float  # seconds since 1970
    description: str
    root: Path  # of the library the songs belong to
    db_path: Path
    songs: int
    undone: bool = False
    undoes: str = ""  # the id of the entry this one undid, if it is an undo


def snapshot(job) -> Before | None:
    """What the file has now, of what `job` will change. None if it changes nothing."""
    changes = job.changes
    if not changes.tags and not changes.cover_changed and changes.rating is None and job.lrc_text is None:
        return None
    current = read_editable_file(job.path)
    before = Before(job.path)
    for name in changes.tags:
        before.tags[name] = current.tags.get(name, "")
    if changes.cover_changed:
        before.cover_changed = True
        before.cover = current.cover
    if changes.rating is not None:
        before.rating = current.rating
    if job.lrc_text is not None:
        before.lrc_changed = True
        before.lrc_text = current.lrc_text
    return before


def _entry_dir(entry_id: str) -> Path:
    return history_dir() / entry_id


def _write_json(path: Path, data: dict) -> None:
    temp = path.with_name(f".{path.name}.tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    os.replace(temp, path)


def save_entry(
    description: str, root: Path, db_path: Path, befores: list[Before], undoes: str = ""
) -> HistoryEntry | None:
    """Keep the notes of one finished save. None if there is nothing to keep or
    they cannot be written (the change itself has happened either way)."""
    if not befores:
        return None
    entry = HistoryEntry(
        id=f"{int(time.time() * 1000)}-{uuid.uuid4().hex[:8]}",
        when=time.time(),
        description=description,
        root=root,
        db_path=db_path,
        songs=len(befores),
        undoes=undoes,
    )
    folder = _entry_dir(entry.id)
    files = []
    try:
        with _lock:
            folder.mkdir(parents=True, exist_ok=True)
            for number, before in enumerate(befores):
                cover_file = ""
                if before.cover_changed and before.cover:
                    cover_file = f"{number}.img"
                    (folder / cover_file).write_bytes(before.cover)
                files.append({
                    "path": str(before.path),
                    "tags": before.tags,
                    "cover_changed": before.cover_changed,
                    "cover_file": cover_file,
                    "rating": before.rating,
                    "lrc_changed": before.lrc_changed,
                    "lrc_text": before.lrc_text,
                })
            _write_json(folder / ENTRY_FILE, {
                "description": entry.description,
                "when": entry.when,
                "root": str(root),
                "db_path": str(db_path),
                "undone": False,
                "undoes": undoes,
                "files": files,
            })
            _prune()
    except OSError as error:
        print(f"MusicHive: cannot keep the tag history: {error}")
        shutil.rmtree(folder, ignore_errors=True)
        return None
    return entry


def _prune() -> None:
    """Remove the oldest entries beyond MAX_ENTRIES. Called with _lock held."""
    folders = sorted((p for p in history_dir().iterdir() if p.is_dir()), key=lambda p: p.name)
    for old in folders[:-MAX_ENTRIES]:
        shutil.rmtree(old, ignore_errors=True)


def _read(folder: Path) -> dict | None:
    try:
        return json.loads((folder / ENTRY_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def list_entries() -> list[HistoryEntry]:
    """Every kept entry, the newest first."""
    base = history_dir()
    if not base.is_dir():
        return []
    entries = []
    with _lock:
        for folder in sorted(base.iterdir(), key=lambda p: p.name, reverse=True):
            data = _read(folder) if folder.is_dir() else None
            if data is None:
                continue
            entries.append(
                HistoryEntry(
                    id=folder.name,
                    when=float(data.get("when") or 0),
                    description=data.get("description", ""),
                    root=Path(data.get("root", "")),
                    db_path=Path(data.get("db_path", "")),
                    songs=len(data.get("files", [])),
                    undone=bool(data.get("undone")),
                    undoes=data.get("undoes", ""),
                )
            )
    return entries


def restore_jobs(entry: HistoryEntry) -> list:
    """The changes (TagJobs) that put back what the files had before the entry's save."""
    from src.tag_writer import TagJob  # tag_writer imports this module

    folder = _entry_dir(entry.id)
    data = _read(folder)
    if data is None:
        return []
    jobs = []
    for item in data.get("files", []):
        changes = TagChanges(tags=dict(item.get("tags") or {}))
        if item.get("cover_changed"):
            changes.cover_changed = True
            cover_file = item.get("cover_file") or ""
            try:
                changes.cover = (folder / cover_file).read_bytes() if cover_file else None
            except OSError:
                continue  # without the old picture this file cannot be put back
        if item.get("rating") is not None:
            changes.rating = int(item["rating"])
        lrc_text = None
        if item.get("lrc_changed"):
            lrc_text = item.get("lrc_text") or ""  # "" removes a .lrc that was not there
        jobs.append(TagJob(Path(item["path"]), changes, lrc_text=lrc_text))
    return jobs


def set_undone(entry_id: str, undone: bool) -> None:
    folder = _entry_dir(entry_id)
    with _lock:
        data = _read(folder)
        if data is None:
            return
        data["undone"] = undone
        try:
            _write_json(folder / ENTRY_FILE, data)
        except OSError:
            pass
