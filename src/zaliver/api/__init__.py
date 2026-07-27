"""Safe FastAPI wrapper around ZaliverCore (headless control plane)."""

from __future__ import annotations

from zaliver.api.app import create_app

__all__ = ["create_app"]
