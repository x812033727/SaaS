"""Notes router — placeholder for Task #5."""

from fastapi import APIRouter

router = APIRouter(prefix="/notes", tags=["notes"])


@router.get("/health")
def notes_health():
    return {"status": "notes router ready"}
