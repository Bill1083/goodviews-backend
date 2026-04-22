from flask import Flask
from flask_cors import CORS
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

from app.config import Config

limiter = Limiter(key_func=get_remote_address)


def create_app() -> Flask:
    app = Flask(__name__)
    app.config.from_object(Config)

    # CORS — only allow configured origins
    CORS(app, resources={r"/api/*": {"origins": app.config["CORS_ORIGINS"]}})

    # Rate limiter — backed by Redis when available
    limiter.init_app(app)

    # Register blueprints
    from app.controllers.movies import movies_bp
    from app.controllers.reviews import reviews_bp

    app.register_blueprint(movies_bp, url_prefix="/api/movies")
    app.register_blueprint(reviews_bp, url_prefix="/api/reviews")

    @app.get("/api/health")
    def health():
        return {"status": "ok"}

    return app
