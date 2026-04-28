import logging
import uuid

from fastapi import HTTPException

from app.models.schemas import FailResponse, HealthResponse, RecoverResponse, WorkResponse
from app.services.state_manager import StateManager

logger = logging.getLogger(__name__)


class PaymentService:
    def __init__(self, state_manager: StateManager, service_name: str) -> None:
        self.state_manager = state_manager
        self.service_name = service_name

    def health(self) -> HealthResponse:
        if self.state_manager.is_crashed():
            return HealthResponse(status="unhealthy", service=self.service_name)
        return HealthResponse(status="healthy", service=self.service_name)

    def process_payment(self) -> WorkResponse:
        if self.state_manager.is_crashed():
            logger.error("process_payment(): CRASHED — raising 500")
            raise HTTPException(
                status_code=500,
                detail=f"{self.service_name} is in a simulated crashed state. POST /recover to reset.",
            )
        txn_id = f"TXN-{uuid.uuid4().hex[:8].upper()}"
        logger.info("process_payment(): processed %s", txn_id)
        return WorkResponse(
            message="Payment processed successfully.",
            service=self.service_name,
            transaction_id=txn_id,
        )

    def trigger_fail(self) -> FailResponse:
        self.state_manager.set_crashed()
        logger.warning("trigger_fail(): payment-service is now CRASHED")
        return FailResponse(
            message=f"{self.service_name} is now simulating a crash. POST /recover to reset.",
            crashed=True,
        )

    def recover(self) -> RecoverResponse:
        self.state_manager.recover()
        logger.info("recover(): payment-service restored")
        return RecoverResponse(
            message=f"{self.service_name} has recovered. All failure flags cleared.",
            crashed=False,
        )
