#!/usr/bin/env python3
"""Compatibility entry point for the Google Photos orphan finder."""

import sys

from gphotos_cleanup.finder_reporting import *


if __name__ == "__main__":
    sys.exit(main())
