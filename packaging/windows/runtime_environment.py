# Runs inside the Windows build before main.py and before any library is loaded.
import os

# MSYS2's FFmpeg comes with Whisper, and so with ggml, which GStreamer's libav
# plugin then loads. The build checks GStreamer's plugins inside MusicHive itself,
# so ggml can be loaded twice, and its crash handler then aborts the program on
# purpose (GGML_ASSERT(prev != ggml_uncaught_exception)). This turns that off.
os.environ["GGML_NO_BACKTRACE"] = "1"
