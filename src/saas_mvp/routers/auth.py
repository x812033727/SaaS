"""Auth router — placeholder for Task #2."""

from fastapi import APIRouter

router = APIRouter(prefix="/auth", tags=["auth"])


@router.get("/health")
def auth_health():
    return {"status": "auth router ready"}
