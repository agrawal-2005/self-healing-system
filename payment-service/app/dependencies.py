from app.config.settings import settings
from app.services.payment_service import PaymentService
from app.services.state_manager import StateManager

_state_manager = StateManager()
_payment_service = PaymentService(state_manager=_state_manager, service_name=settings.service_name)


def get_payment_service() -> PaymentService:
    return _payment_service
