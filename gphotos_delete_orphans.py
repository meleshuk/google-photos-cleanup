#!/usr/bin/env python3
"""Compatibility entry point for the Google Photos orphan deleter."""

import sys

from gphotos_cleanup.delete_cli import *
from gphotos_cleanup.delete_selftest import self_test


if __name__ == "__main__":
    sys.exit(main())
