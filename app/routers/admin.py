"""Admin REST API (protected by X-API-Key)."""

from fastapi import APIRouter, Depends, Query

from app.dependencies.deps import get_admin_service, require_admin
from app.schemas.admin import StatsRead
from app.schemas.conversation import ConversationDetail, ConversationRead
from app.schemas.user import UserRead
from app.services.admin_service import AdminService

router = APIRouter(
    prefix="/admin", tags=["admin"], dependencies=[Depends(require_admin)]
)


@router.get("/users", response_model=list[UserRead])
async def list_users(
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    service: AdminService = Depends(get_admin_service),
) -> list[UserRead]:
    return [UserRead.model_validate(u) for u in await service.list_users(offset, limit)]


@router.get("/conversations", response_model=list[ConversationRead])
async def list_conversations(
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    service: AdminService = Depends(get_admin_service),
) -> list[ConversationRead]:
    return [
        ConversationRead.model_validate(c)
        for c in await service.list_conversations(offset, limit)
    ]


@router.get("/conversations/{conversation_id}", response_model=ConversationDetail)
async def get_conversation(
    conversation_id: int,
    service: AdminService = Depends(get_admin_service),
) -> ConversationDetail:
    return ConversationDetail.model_validate(
        await service.get_conversation(conversation_id)
    )


@router.delete("/conversations/{conversation_id}", status_code=204)
async def delete_conversation(
    conversation_id: int,
    service: AdminService = Depends(get_admin_service),
) -> None:
    await service.delete_conversation(conversation_id)


@router.get("/stats", response_model=StatsRead)
async def stats(service: AdminService = Depends(get_admin_service)) -> StatsRead:
    return await service.stats()
