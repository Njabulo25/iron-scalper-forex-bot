"""Simple session-based authentication for the trading dashboard."""

from functools import wraps

from flask import jsonify, redirect, request, session


USERS = {
    "njabulo": {"pin": "1234", "role": "admin"},
    "friend1": {"pin": "5678", "role": "viewer"},
    "friend2": {"pin": "9012", "role": "viewer"},
}


def check_auth(username, pin):
    """Return whether the supplied username and PIN match a configured user."""
    user = USERS.get(username)
    return user is not None and user["pin"] == pin


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
