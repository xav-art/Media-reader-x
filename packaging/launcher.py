"""PyInstaller entry script (absolute import so the frozen app finds the package)."""

import sys

from mediareaderx.app import main

sys.exit(main())
