"""Build absolute links from the trusted application origin."""

from flask import current_app


def public_url(path: str) -> str:
    """Return an absolute URL without consulting the incoming Host header."""
    return f"{current_app.config['PUBLIC_BASE_URL']}/{path.lstrip('/')}"
