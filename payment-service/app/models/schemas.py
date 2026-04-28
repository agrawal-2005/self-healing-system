from pydantic import BaseModel


class HealthResponse(BaseModel):
    status: str   # "healthy" | "unhealthy"
    service: str


class WorkResponse(BaseModel):
    message: str
    service: str
    transaction_id: str


class FailResponse(BaseModel):
    message: str
    crashed: bool


class RecoverResponse(BaseModel):
    message: str
    crashed: bool
