"""Domain exceptions. API exception handlers map these to `{"error": {"code", "message"}}`."""

from __future__ import annotations

from typing import Any


class AppError(Exception):
    """Base class for errors that should be returned to API clients."""

    status_code: int = 500
    code: str = "internal_error"

    def __init__(self, message: str, *, details: Any | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details


class NotFoundError(AppError):
    status_code = 404
    code = "not_found"


class ConflictError(AppError):
    """The request is valid but conflicts with the current state of the resource."""

    status_code = 409
    code = "conflict"


class ValidationFailedError(AppError):
    """Semantic validation failure (e.g. a bad task graph). Returned as HTTP 422."""

    status_code = 422
    code = "validation_error"


class InvalidTransitionError(AppError):
    """Raised by the state machine when a status change is not allowed."""

    status_code = 409
    code = "invalid_transition"

    def __init__(self, entity: str, from_status: str, to_status: str) -> None:
        super().__init__(
            f"Invalid {entity} status transition: {from_status} -> {to_status}",
            details={"entity": entity, "from": from_status, "to": to_status},
        )
        self.entity = entity
        self.from_status = from_status
        self.to_status = to_status


class UnauthorizedError(AppError):
    status_code = 401
    code = "unauthorized"
