import logging

import uvicorn
from fastapi import FastAPI

from app.routes.routes import router

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [movie-fallback-service] %(levelname)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

app = FastAPI(
    title="Movie Fallback Service",
    description="Cached movie catalog — returned when movie-service is unavailable.",
    version="1.0.0",
)

app.include_router(router)

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8021, reload=False)
