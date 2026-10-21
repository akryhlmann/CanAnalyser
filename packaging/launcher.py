"""Entry point for the PyInstaller build (it needs a script, not a module)."""

import sys

from cananalyser.__main__ import main

sys.exit(main())
