import logging

from fastapi import HTTPException

from app.models.schemas import CatalogResponse, FailResponse, HealthResponse, Movie, RecoverResponse
from app.services.state_manager import StateManager

logger = logging.getLogger(__name__)

_CATALOG = [
    Movie(id=1, title="Inception",       genre="Sci-Fi"),
    Movie(id=2, title="The Dark Knight",  genre="Action"),
    Movie(id=3, title="Interstellar",     genre="Sci-Fi"),
    Movie(id=4, title="Parasite",         genre="Thriller"),
    Movie(id=5, title="The Godfather",    genre="Drama"),
]


class MovieService:
    def __init__(self, state_manager: StateManager, service_name: str) -> None:
        self.state_manager = state_manager
        self.service_name = service_name

    def health(self) -> HealthResponse:
        if self.state_manager.is_crashed():
            return HealthResponse(status="unhealthy", service=self.service_name)
        return HealthResponse(status="healthy", service=self.service_name)

    def get_catalog(self) -> CatalogResponse:
        if self.state_manager.is_crashed():
            logger.error("get_catalog(): CRASHED — raising 500")
            raise HTTPException(
                status_code=500,
                detail=f"{self.service_name} is in a simulated crashed state. POST /recover to reset.",
            )
        logger.info("get_catalog(): returning %d movies", len(_CATALOG))
        return CatalogResponse(movies=_CATALOG, service=self.service_name, degraded=False)

    def trigger_fail(self) -> FailResponse:
        self.state_manager.set_crashed()
        logger.warning("trigger_fail(): movie-service is now CRASHED")
        return FailResponse(
            message=f"{self.service_name} is now simulating a crash. POST /recover to reset.",
            crashed=True,
        )

    def recover(self) -> RecoverResponse:
        self.state_manager.recover()
        logger.info("recover(): movie-service restored")
        return RecoverResponse(
            message=f"{self.service_name} has recovered. All failure flags cleared.",
            crashed=False,
        )
