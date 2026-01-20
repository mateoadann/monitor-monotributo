import os

from flask import Flask
from flask_login import LoginManager

from website.models import create_admin_if_missing, db, seed_data


login_manager = LoginManager()
login_manager.login_view = "auth.login"


def create_app():
    app = Flask(__name__)
    app.config["SECRET_KEY"] = "dev-change-me"
    default_db = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "monitor.db"))
    app.config["SQLALCHEMY_DATABASE_URI"] = os.environ.get(
        "DATABASE_URL",
        "sqlite:///" + default_db,
    )
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

    from website.auth import auth_bp
    from website.views import main_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(main_bp)

    db.init_app(app)
    login_manager.init_app(app)

    from website.models import get_user_by_id

    @login_manager.user_loader
    def load_user(user_id):
        return get_user_by_id(user_id)

    with app.app_context():
        db.create_all()
        create_admin_if_missing()
        seed_data()

    return app
