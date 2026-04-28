import logging

import uvicorn
from fastapi import FastAPI

from app.config.settings import settings
from app.routes.routes import router

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [payment-service] %(levelname)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

app = FastAPI(
    title="Payment Service",
    description="Critical payment processor. Supports crash simulation for self-healing tests.",
    version="1.0.0",
)

app.include_router(router)

if __name__ == "__main__":
    uvicorn.run(app, host=settings.host, port=settings.port, reload=False)
