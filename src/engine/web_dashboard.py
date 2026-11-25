"""Compatibility entry point for the current Iron Scalper terminal.

Older instructions and shortcuts start ``src.engine.web_dashboard``. Keep that
command working, but serve the same template, static assets and API as the
default launcher instead of its retired inline dashboard.
"""

from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.engine.web_dashboard_next import app, run, socketio, state, state_lock


if __name__ == "__main__":
    run()
