"""Session authentication and local operator account management."""

from collections import OrderedDict
from functools import wraps
import argparse
import getpass
import threading
import time

from flask import jsonify, redirect, request, session

from src.engine import database


MIN_PASSPHRASE_LENGTH = 12
_LOGIN_WINDOW_SECONDS = 15 * 60
_LOGIN_MAX_ATTEMPTS = 5
_LOGIN_ATTEMPTS = OrderedDict()
_LOGIN_LOCK = threading.Lock()


def authenticate(username, passphrase):
    """Return a public user record when the passphrase is valid."""
    username = str(username or "").strip()
    if not username or not isinstance(passphrase, str):
        return None
    if len(passphrase) < MIN_PASSPHRASE_LENGTH:
        return None
    try:
        return database.authenticate_user(username, passphrase)
    except Exception:
        return None


def check_auth(username, pin):
    """Compatibility boolean wrapper for older callers."""
    return authenticate(username, pin) is not None


def login_allowed(remote_addr):
    """Limit repeated login attempts from one client address."""
    key = remote_addr or "unknown"
    now = time.monotonic()
    with _LOGIN_LOCK:
        attempts = [
            stamp for stamp in _LOGIN_ATTEMPTS.get(key, ())
            if now - stamp < _LOGIN_WINDOW_SECONDS
        ]
        if attempts:
            _LOGIN_ATTEMPTS[key] = attempts
            _LOGIN_ATTEMPTS.move_to_end(key)
        else:
            _LOGIN_ATTEMPTS.pop(key, None)
        return len(attempts) < _LOGIN_MAX_ATTEMPTS


def record_login_attempt(remote_addr, succeeded):
    key = remote_addr or "unknown"
    now = time.monotonic()
    with _LOGIN_LOCK:
        attempts = [
            stamp for stamp in _LOGIN_ATTEMPTS.get(key, ())
            if now - stamp < _LOGIN_WINDOW_SECONDS
        ]
        if succeeded:
            _LOGIN_ATTEMPTS.pop(key, None)
            return
        attempts.append(now)
        _LOGIN_ATTEMPTS[key] = attempts
        _LOGIN_ATTEMPTS.move_to_end(key)
        while len(_LOGIN_ATTEMPTS) > 1024:
            _LOGIN_ATTEMPTS.popitem(last=False)


def login_required(route_function):
    """Require a logged-in Flask session before executing a dashboard route."""
    @wraps(route_function)
    def wrapped_route(*args, **kwargs):
        if session.get("logged_in") is not True:
            if request.headers.get("X-Requested-With") == "XMLHttpRequest" or request.is_json:
                return jsonify({"error": "unauthorized"}), 401
            return redirect("/login")
        return route_function(*args, **kwargs)

    return wrapped_route


def admin_required(route_function):
    """Require an authenticated operator account for state-changing controls."""
    @wraps(route_function)
    @login_required
    def wrapped_route(*args, **kwargs):
        if session.get("role") != "admin":
            return jsonify({"error": "administrator access required"}), 403
        return route_function(*args, **kwargs)

    return wrapped_route


def _manage_users(argv=None):
    parser = argparse.ArgumentParser(description="Manage Iron Scalper dashboard users")
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("add-user", help="add an admin or read-only viewer")
    create.add_argument("--username", required=True)
    create.add_argument("--role", required=True, choices=("admin", "viewer"))
    reset = commands.add_parser("reset-passphrase", help="replace an existing user's passphrase")
    reset.add_argument("--username", required=True)
    args = parser.parse_args(argv)

    passphrase = getpass.getpass("Passphrase (12 characters minimum): ")
    confirmation = getpass.getpass("Confirm passphrase: ")
    if passphrase != confirmation:
        parser.error("passphrases do not match")

    database.init_db()
    if args.command == "add-user":
        if not database.add_user(args.username, passphrase, args.role):
            parser.error("that username already exists")
        print(f"Created {args.role} account {args.username}.")
        return 0

    if not database.set_user_passphrase(args.username, passphrase):
        parser.error("that username does not exist")
    print(f"Updated the passphrase for {args.username}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(_manage_users())
