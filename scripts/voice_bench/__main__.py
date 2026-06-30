"""Entry point: ``python -m scripts.voice_bench``."""

import sys

from .runner import main

if __name__ == "__main__":
    sys.exit(main())
