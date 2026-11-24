# Replaces PyInstaller's own hook for Qt Multimedia. MusicHive only uses its
# QAudioBuffer and QAudioFormat, not Qt's media backends, and the FFmpeg backend
# alone would bring FFmpeg, its video codecs, Whisper and ggml along.
from pathlib import Path

from PyInstaller.utils.hooks.qt import add_qt6_dependencies

hiddenimports, binaries, datas = add_qt6_dependencies(__file__)
binaries = [entry for entry in binaries if Path(entry[0]).parent.name != "multimedia"]
