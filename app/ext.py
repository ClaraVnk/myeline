from authlib.integrations.flask_client import OAuth
from flask_babel import Babel
from flask_login import LoginManager
from flask_migrate import Migrate
from flask_sqlalchemy import SQLAlchemy

# Re-export the single, app-configured limiter so that route decorators
# (`@limiter.limit(...)`) and `init_app` in app/__init__.py operate on the
# SAME instance. A previously divergent `Limiter()` defined here was never
# init_app'd, silently disabling every per-route rate limit (login,
# 2FA verify, register, password reset, scrapers, RAG cost cap).
from app.utils.flask_limiter import limiter  # noqa: E402,F401


db = SQLAlchemy()
migrate = Migrate()
login_manager = LoginManager()
babel = Babel()
oauth = OAuth()
