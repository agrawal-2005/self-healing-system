from app.config.settings import settings
from app.services.movie_service import MovieService
from app.services.state_manager import StateManager

_state_manager = StateManager()
_movie_service = MovieService(state_manager=_state_manager, service_name=settings.service_name)


def get_movie_service() -> MovieService:
    return _movie_service
