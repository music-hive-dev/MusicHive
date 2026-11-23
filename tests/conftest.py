"""Shared setup for the tests. Run them from the project folder with: pytest"""

import os

# Some modules touch Qt classes; never open a window or need a display.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
