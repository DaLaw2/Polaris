"""The one shape every refusal takes over HTTP."""

from fastapi import HTTPException


def refuse(status: int, code: str, message: str, **extra) -> HTTPException:
    """An HTTPException whose detail carries a stable code beside the text."""
    return HTTPException(status_code=status,
                         detail={"code": code, "message": message, **extra})
