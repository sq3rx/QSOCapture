"""Shared pytest setup: use an isolated app-data directory for every test run."""

import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# Must be set before config.py is imported.
os.environ["LOCALAPPDATA"] = tempfile.mkdtemp(prefix="qsocapture-tests-")
