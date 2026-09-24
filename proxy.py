"""
OpenCode Space Gateway Entrypoint
================================
Imports the modular FastAPI application from the gateway package.
"""

import sys
import os

# Ensure the directory containing proxy.py is in sys.path
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from gateway import app

__all__ = ["app"]
