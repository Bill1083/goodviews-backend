import logging

from flask import jsonify

logger = logging.getLogger(__name__)


def server_error(message: str, exc: Exception, status: int = 500):
    """Logs the real exception server-side and returns a generic message to the
    client — avoids leaking DB/PostgREST internals (constraint names, column
    names, connection details) to API callers, including unauthenticated ones."""
    logger.exception(message)
    return jsonify({"error": message}), status
