from fastapi import APIRouter, Depends, Response

from app.dependencies import get_movie_service
from app.models.schemas import CatalogResponse, FailResponse, HealthResponse, RecoverResponse
from app.services.movie_service import MovieService

router = APIRouter()


@router.get("/health", response_model=HealthResponse, summary="Health check")
async def health(response: Response, service: MovieService = Depends(get_movie_service)):
    result = service.health()
    if result.status != "healthy":
        response.status_code = 503
    return result


@router.get("/catalog", response_model=CatalogResponse, summary="Get movie catalog")
async def catalog(service: MovieService = Depends(get_movie_service)):
    return service.get_catalog()


@router.post("/fail", response_model=FailResponse, summary="Simulate a crash")
async def fail(service: MovieService = Depends(get_movie_service)):
    return service.trigger_fail()


@router.post("/recover", response_model=RecoverResponse, summary="Reset failure flags")
async def recover(service: MovieService = Depends(get_movie_service)):
    return service.recover()
