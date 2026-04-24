"""
Entry point for api-service.

This file:
  1. Creates the FastAPI application.
  2. Registers route handlers.
  3. Starts the Uvicorn server when run directly.

It does NOT contain business logic, HTTP clients, or settings.
Those live in their own layers and are wired together in dependencies.py.
"""

import logging

import uvicorn
from fastapi import FastAPI

from app.config.settings import settings
from app.routes.api_routes import router

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)

app = FastAPI(
    title="API Service",
    description="Gateway: routes requests to core-service, auto-falls back to fallback-service.",
    version="1.0.0",
)

# Register all routes defined in api_routes.py
app.include_router(router)


if __name__ == "__main__":
    uvicorn.run(
        "app.main:app",
        host=settings.host,
        port=settings.port,
        reload=False,
    )
