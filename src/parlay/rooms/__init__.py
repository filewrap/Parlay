"""Persistent listening rooms and their authenticated ASGI gateway."""

from __future__ import annotations

from typing import Any

from .service import RoomError, RoomService


def create_app(*args: Any, **kwargs: Any) -> Any:
    """Import the optional ASGI dependency only when the gateway is constructed."""
    from .gateway import create_app as factory

    return factory(*args, **kwargs)


def validate_init_data(*args: Any, **kwargs: Any) -> dict:
    """Import gateway authentication support on demand."""
    from .gateway import validate_init_data as validate

    return validate(*args, **kwargs)


__all__ = ["RoomError", "RoomService", "create_app", "validate_init_data"]
