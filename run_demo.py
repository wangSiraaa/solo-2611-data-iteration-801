#!/usr/bin/env python3
"""Small entry point: ``python run_demo.py``."""

import sys

from delivery_system.cli import main

if __name__ == "__main__":
    raise SystemExit(main(["demo"] + sys.argv[1:]))
