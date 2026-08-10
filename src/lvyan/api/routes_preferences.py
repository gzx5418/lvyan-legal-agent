"""用户长期偏好 API 路由。

只接受白名单展示偏好；案件事实、聊天记录和附件不属于该路由的数据模型。
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends

from lvyan.api.auth import get_current_user_id
from lvyan.api.models import UserPreferenceResponse, UserPreferenceUpdate
from lvyan.memory.user_preferences import UserPreferences


def create_preferences_router(store: UserPreferences) -> APIRouter:
    router = APIRouter(prefix="/api/preferences", tags=["preferences"])

    @router.get("", response_model=UserPreferenceResponse)
    async def get_preferences(
        user_id: str = Depends(get_current_user_id),
    ) -> UserPreferenceResponse:
        pref = await asyncio.to_thread(store.get, user_id)
        return UserPreferenceResponse.model_validate(pref.model_dump())

    @router.patch("", response_model=UserPreferenceResponse)
    async def update_preferences(
        req: UserPreferenceUpdate,
        user_id: str = Depends(get_current_user_id),
    ) -> UserPreferenceResponse:
        updates = req.model_dump(exclude_none=True)
        if updates:
            await asyncio.to_thread(store.update, user_id, **updates)
        pref = await asyncio.to_thread(store.get, user_id)
        return UserPreferenceResponse.model_validate(pref.model_dump())

    @router.delete("")
    async def delete_preferences(
        user_id: str = Depends(get_current_user_id),
    ) -> dict[str, bool]:
        deleted = await asyncio.to_thread(store.delete, user_id)
        return {"deleted": deleted}

    return router


__all__ = ["create_preferences_router"]
