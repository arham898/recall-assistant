"""FastAPI dependency functions — pull shared objects from app.state."""

from __future__ import annotations

from fastapi import Header, HTTPException, Request


def require_api_key(
    request: Request,
    x_api_key: str | None = Header(None),
) -> None:
    """Raise 401 if the API key header is missing or wrong."""
    settings = request.app.state.settings
    if x_api_key != settings.api_key:
        raise HTTPException(status_code=401, detail="Invalid or missing API key")
