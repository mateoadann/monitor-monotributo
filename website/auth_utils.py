from __future__ import annotations

from functools import wraps

from flask import abort, redirect, url_for
from flask_login import current_user


def role_required(*roles):
    def decorator(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            if not current_user.is_authenticated:
                return redirect(url_for("auth.login"))
            if current_user.role not in roles:
                abort(403)
            return f(*args, **kwargs)
        return wrapper
    return decorator


def editor_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not current_user.is_authenticated:
            return redirect(url_for("auth.login"))
        if not current_user.can_edit_data():
            abort(403)
        return f(*args, **kwargs)
    return wrapper


def admin_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not current_user.is_authenticated:
            return redirect(url_for("auth.login"))
        if not current_user.can_manage_users():
            abort(403)
        return f(*args, **kwargs)
    return wrapper
