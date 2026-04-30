class StateManager:
    """Owns the mutable failure state for payment-service."""

    def __init__(self) -> None:
        self._crashed: bool = False

    def set_crashed(self) -> None:
        self._crashed = True

    def is_crashed(self) -> bool:
        return self._crashed

    def recover(self) -> None:
        self._crashed = False
