"""Entry point for ``python -m mediareaderx`` and the frozen executable."""

import sys

from mediareaderx.app import main

if __name__ == "__main__":
    sys.exit(main())
