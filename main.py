"""Start the Iron Scalper local trading terminal.

The web dashboard starts first. Live trading starts only after the operator
presses ``Start engine`` in the dashboard, where MT5 readiness is checked.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def _arguments(argv=None):
    parser = argparse.ArgumentParser(description="Run the Iron Scalper terminal")
    parser.add_argument("--host", default="127.0.0.1", help="Bind address (default: local machine only)")
    parser.add_argument("--port", default=5000, type=int, help="Dashboard port (default: 5000)")
    return parser.parse_args(argv)


def main(argv=None):
    args = _arguments(argv)
    os.chdir(PROJECT_ROOT)
    if not 1 <= args.port <= 65535:
        print("Port must be between 1 and 65535.", file=sys.stderr)
        return 2

    try:
        from src.engine.web_dashboard_next import run
    except ImportError as error:
        missing = getattr(error, "name", None) or str(error)
        print(f"Iron Scalper cannot start: missing Python dependency ({missing}).", file=sys.stderr)
        print("Run setup.bat, or install the project dependencies with:", file=sys.stderr)
        print("  .venv\\Scripts\\python.exe -m pip install -r requirements-runtime.txt", file=sys.stderr)
        return 1
    except Exception as error:
        print(f"Iron Scalper could not load the dashboard: {error}", file=sys.stderr)
        return 1

    try:
        run(host=args.host, port=args.port)
    except KeyboardInterrupt:
        print("\nIron Scalper stopped.")
    except OSError as error:
        print(f"Iron Scalper could not bind to {args.host}:{args.port}: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
