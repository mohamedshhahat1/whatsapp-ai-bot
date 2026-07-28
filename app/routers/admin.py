"""Admin REST API (protected by X-API-Key and rate limited per client IP)."""

from fastapi import APIRouter, Depends, Query, Request

from app.core.ratelimit import ADMIN_LIMIT, limiter
from app.dependencies.deps import get_admin_service, require_admin
from app.schemas.admin import StatsRead
from app.schemas.conversation import ConversationDetail, ConversationRead
from app.schemas.knowledge import KnowledgeDocumentRead, KnowledgeSearchHit
from app.schemas.user import UserRead
from app.services.admin_service import AdminService

router = APIRouter(
    prefix="/admin", tags=["admin"], dependencies=[Depends(require_admin)]
)


@router.get("/users", response_model=list[UserRead])
@limiter.limit(ADMIN_LIMIT)
async def list_users(
    request: Request,
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    service: AdminService = Depends(get_admin_service),
) -> list[UserRead]:
    return [UserRead.model_validate(u) for u in await service.list_users(offset, limit)]


@router.get("/conversations", response_model=list[ConversationRead])
@limiter.limit(ADMIN_LIMIT)
async def list_conversations(
    request: Request,
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    service: AdminService = Depends(get_admin_service),
) -> list[ConversationRead]:
    return [
        ConversationRead.model_validate(c)
        for c in await service.list_conversations(offset, limit)
    ]


@router.get("/conversations/{conversation_id}", response_model=ConversationDetail)
@limiter.limit(ADMIN_LIMIT)
async def get_conversation(
    request: Request,
    conversation_id: int,
    service: AdminService = Depends(get_admin_service),
) -> ConversationDetail:
    return ConversationDetail.model_validate(
        await service.get_conversation(conversation_id)
    )


@router.delete("/conversations/{conversation_id}", status_code=204)
@limiter.limit(ADMIN_LIMIT)
async def delete_conversation(
    request: Request,
    conversation_id: int,
    service: AdminService = Depends(get_admin_service),
) -> None:
    await service.delete_conversation(conversation_id)


@router.get("/knowledge", response_model=list[KnowledgeDocumentRead])
@limiter.limit(ADMIN_LIMIT)
async def list_knowledge_documents(
    request: Request,
    service: AdminService = Depends(get_admin_service),
) -> list[KnowledgeDocumentRead]:
    """Documents currently indexed in the vector store."""
    return [
        KnowledgeDocumentRead.model_validate(d) for d in await service.list_documents()
    ]


@router.get("/knowledge/search", response_model=list[KnowledgeSearchHit])
@limiter.limit(ADMIN_LIMIT)
async def search_knowledge(
    request: Request,
    q: str = Query(..., min_length=2, description="Question to test retrieval with"),
    limit: int = Query(5, ge=1, le=20),
    service: AdminService = Depends(get_admin_service),
) -> list[KnowledgeSearchHit]:
    """Preview exactly which chunks the model would receive for a question."""
    return [
        KnowledgeSearchHit(
            source=doc.source, score=doc.score or 0.0, content=doc.content
        )
        for doc in await service.search_knowledge(q, limit=limit)
    ]
