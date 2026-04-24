"""
Entry point for fallback-service.
"""

import logging

import uvicorn
from fastapi import FastAPI

from app.config.settings import settings
from app.routes.fallback_routes import router

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)

app = FastAPI(
    title="Fallback Service",
    description="Safe degraded response when core-service is unavailable.",
    version="1.0.0",
)

app.include_router(router)


if __name__ == "__main__":
    uvicorn.run(
        "app.main:app",
        host=settings.host,
        port=settings.port,
        reload=False,
    )
