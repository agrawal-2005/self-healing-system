from fastapi import APIRouter, Depends, Response

from app.dependencies import get_payment_service
from app.models.schemas import FailResponse, HealthResponse, RecoverResponse, WorkResponse
from app.services.payment_service import PaymentService

router = APIRouter()


@router.get("/health", response_model=HealthResponse, summary="Health check")
async def health(response: Response, service: PaymentService = Depends(get_payment_service)):
    result = service.health()
    if result.status != "healthy":
        response.status_code = 503
    return result


@router.get("/process-payment", response_model=WorkResponse, summary="Process a payment")
async def process_payment(service: PaymentService = Depends(get_payment_service)):
    return service.process_payment()


@router.post("/fail", response_model=FailResponse, summary="Simulate a crash")
async def fail(service: PaymentService = Depends(get_payment_service)):
    return service.trigger_fail()


@router.post("/recover", response_model=RecoverResponse, summary="Reset failure flags")
async def recover(service: PaymentService = Depends(get_payment_service)):
    return service.recover()
