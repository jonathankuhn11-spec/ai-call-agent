"""Startet den Stimmdienst: python -m stimme  (STIMME_PORT, Standard 8300, nur auf 127.0.0.1 bzw. STIMME_HOST)."""
from __future__ import annotations

import logging
import os

import uvicorn

from stimme.server import app_erzeugen

if __name__ == "__main__":
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
    uvicorn.run(app_erzeugen(), host=os.environ.get("STIMME_HOST", "127.0.0.1"), port=int(os.environ.get("STIMME_PORT", "8300")), log_level="info")
