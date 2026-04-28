from pydantic import BaseModel
from typing import List


class HealthResponse(BaseModel):
    status: str   # "healthy" | "unhealthy"
    service: str


class Movie(BaseModel):
    id: int
    title: str
    genre: str


class CatalogResponse(BaseModel):
    movies: List[Movie]
    service: str
    degraded: bool = False


class FailResponse(BaseModel):
    message: str
    crashed: bool


class RecoverResponse(BaseModel):
    message: str
    crashed: bool
