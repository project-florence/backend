"""Meta endpoint'leri (public): avatar listesi vb."""

from fastapi import APIRouter

router = APIRouter()

# Backend'in tanidigi avatar id'leri. SVG dosyalari frontend assets'inde tutulur
# (backend bu dosyalari serve etmez); url '/avatars/{id}.svg' seklindedir.
AVATAR_IDS = [f"avatar-{i}" for i in range(1, 13)]


@router.get("/meta/avatars")
async def list_avatars():
    return [
        {"id": avatar_id, "url": f"/avatars/{avatar_id}.svg"} for avatar_id in AVATAR_IDS
    ]
