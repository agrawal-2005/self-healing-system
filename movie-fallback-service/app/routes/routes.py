import logging

from fastapi import APIRouter
from pydantic import BaseModel
from typing import List

router = APIRouter()
logger = logging.getLogger(__name__)


class HealthResponse(BaseModel):
    status: str
    service: str


class Movie(BaseModel):
    id: int
    title: str
    genre: str


class CatalogResponse(BaseModel):
    movies: List[Movie]
    service: str
    degraded: bool

# Cached snapshot — always available even when movie-service is down
_CACHED_CATALOG = [
    Movie(id=1, title="Inception",      genre="Sci-Fi"),
    Movie(id=2, title="The Dark Knight", genre="Action"),
    Movie(id=3, title="Interstellar",    genre="Sci-Fi"),
]


@router.get("/health", response_model=HealthResponse, summary="Health check")
async def health():
    return HealthResponse(status="healthy", service="movie-fallback-service")


@router.get("/catalog", response_model=CatalogResponse, summary="Cached movie catalog (degraded mode)")
async def catalog():
    logger.info("FALLBACK_TRIGGERED: serving cached catalog (movie-service unavailable)")
    return CatalogResponse(
        movies=_CACHED_CATALOG,
        service="movie-fallback-service",
        degraded=True,
    )
