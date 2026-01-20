from flask import Blueprint, redirect, render_template, request, url_for
from flask_login import login_user, logout_user

from website.models import authenticate

auth_bp = Blueprint("auth", __name__)


@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "").strip()
        user = authenticate(username, password)
        if user:
            login_user(user)
            return redirect(url_for("main.dashboard"))
        error = "Usuario o contrasena incorrecta"
    return render_template("login.html", error=error)


@auth_bp.route("/logout")
def logout():
    logout_user()
    return redirect(url_for("auth.login"))
