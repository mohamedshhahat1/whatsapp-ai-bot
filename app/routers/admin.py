"""Admin REST API (protected by X-API-Key and rate limited per client IP)."""

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status

from app.core.ratelimit import ADMIN_LIMIT, limiter
from app.dependencies.deps import (
    get_admin_service,
    get_analytics_service,
    get_pricing_service,
    get_reply_service,
    require_admin,
)
from app.schemas.admin import StatsRead
from app.schemas.analytics import (
    AnalyticsOverview,
    CustomerActivityRead,
    DailyUsageRead,
    ManualReplyRequest,
    ManualReplyResponse,
    MessageHitRead,
    TopQuestionRead,
)
from app.schemas.conversation import (
    ConversationDetail,
    ConversationRead,
    HandoffRequest,
)
from app.schemas.knowledge import KnowledgeDocumentRead, KnowledgeSearchHit
from app.schemas.pricing import ModelCostRead, ModelPricingCreate, ModelPricingRead
from app.schemas.user import UserRead
from app.services.admin_service import AdminService
from app.services.analytics_service import AnalyticsService
from app.services.pricing_service import DuplicatePricingError, PricingService
from app.services.reply_service import ReplyService

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


@router.post(
    "/conversations/{conversation_id}/takeover", response_model=ConversationRead
)
@limiter.limit(ADMIN_LIMIT)
async def take_over_conversation(
    request: Request,
    conversation_id: int,
    payload: HandoffRequest | None = None,
    service: AdminService = Depends(get_admin_service),
) -> ConversationRead:
    """Take a conversation over. From here the bot stops answering it.

    The body is optional: an operator name is a label for other operators, not
    a credential, and omitting it still stops the bot.
    """
    conversation = await service.take_over(
        conversation_id, operator=payload.operator if payload else None
    )
    return ConversationRead.model_validate(conversation)


@router.post(
    "/conversations/{conversation_id}/resume-ai", response_model=ConversationRead
)
@limiter.limit(ADMIN_LIMIT)
async def resume_ai(
    request: Request,
    conversation_id: int,
    service: AdminService = Depends(get_admin_service),
) -> ConversationRead:
    """Hand the conversation back to the bot."""
    conversation = await service.resume_ai(conversation_id)
    return ConversationRead.model_validate(conversation)


@router.post(
    "/conversations/{conversation_id}/reply", response_model=ManualReplyResponse
)
@limiter.limit(ADMIN_LIMIT)
async def send_manual_reply(
    request: Request,
    conversation_id: int,
    payload: ManualReplyRequest,
    service: ReplyService = Depends(get_reply_service),
) -> ManualReplyResponse:
    """Let a human operator take over and reply directly to the customer.

    Sending a reply does NOT stop the bot on its own: use /takeover for that.
    Coupling them would mean a single clarifying message silences the assistant
    permanently without the operator choosing to.
    """
    message = await service.send_manual_reply(conversation_id, payload.text)
    return ManualReplyResponse(
        message_id=message.id,
        conversation_id=message.conversation_id,
        wa_message_id=message.wa_message_id,
        sent_at=message.created_at,
    )


@router.get("/stats", response_model=StatsRead)
@limiter.limit(ADMIN_LIMIT)
async def stats(
    request: Request,
    service: AdminService = Depends(get_admin_service),
) -> StatsRead:
    return await service.stats()


@router.get("/search", response_model=list[MessageHitRead])
@limiter.limit(ADMIN_LIMIT)
async def search_messages(
    request: Request,
    q: str = Query(..., min_length=2, description="Text to look for in messages"),
    limit: int = Query(50, ge=1, le=200),
    service: AnalyticsService = Depends(get_analytics_service),
) -> list[MessageHitRead]:
    """Full conversation search across inbound and outbound message bodies."""
    return await service.search_messages(q, limit=limit)


@router.get("/analytics/overview", response_model=AnalyticsOverview)
@limiter.limit(ADMIN_LIMIT)
async def analytics_overview(
    request: Request,
    days: int = Query(30, ge=1, le=365),
    service: AnalyticsService = Depends(get_analytics_service),
) -> AnalyticsOverview:
    """Headline KPIs: tokens, spend, latency and error rate."""
    return await service.overview(days=days)


@router.get("/analytics/daily", response_model=list[DailyUsageRead])
@limiter.limit(ADMIN_LIMIT)
async def analytics_daily(
    request: Request,
    days: int = Query(30, ge=1, le=365),
    service: AnalyticsService = Depends(get_analytics_service),
) -> list[DailyUsageRead]:
    """Day-by-day message volume, token usage and cost."""
    return await service.daily(days=days)


@router.get("/analytics/models", response_model=list[ModelCostRead])
@limiter.limit(ADMIN_LIMIT)
async def analytics_models(
    request: Request,
    days: int = Query(30, ge=1, le=365),
    service: AnalyticsService = Depends(get_analytics_service),
) -> list[ModelCostRead]:
    """Spend split per model, each costed at its own historical rates."""
    return await service.cost_by_model(days=days)


@router.get("/analytics/questions", response_model=list[TopQuestionRead])
@limiter.limit(ADMIN_LIMIT)
async def analytics_questions(
    request: Request,
    days: int = Query(30, ge=1, le=365),
    limit: int = Query(10, ge=1, le=50),
    service: AnalyticsService = Depends(get_analytics_service),
) -> list[TopQuestionRead]:
    """Most frequently asked customer questions."""
    return await service.top_questions(days=days, limit=limit)


@router.get("/analytics/customers", response_model=list[CustomerActivityRead])
@limiter.limit(ADMIN_LIMIT)
async def analytics_customers(
    request: Request,
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    service: AnalyticsService = Depends(get_analytics_service),
) -> list[CustomerActivityRead]:
    """Conversation and message counts per customer."""
    return await service.customers(offset=offset, limit=limit)


@router.get("/pricing", response_model=list[ModelPricingRead])
@limiter.limit(ADMIN_LIMIT)
async def list_pricing(
    request: Request,
    service: PricingService = Depends(get_pricing_service),
) -> list[ModelPricingRead]:
    """Full token price history, newest period first."""
    return [ModelPricingRead.model_validate(p) for p in await service.list()]


@router.post("/pricing", response_model=ModelPricingRead, status_code=201)
@limiter.limit(ADMIN_LIMIT)
async def add_pricing(
    request: Request,
    payload: ModelPricingCreate,
    service: PricingService = Depends(get_pricing_service),
) -> ModelPricingRead:
    """Record a new price period.

    Existing rows are never modified, so historical costs stay as they were.
    Calls made before ``effective_from`` keep using the previous price.
    """
    try:
        pricing = await service.add(
            model=payload.model,
            input_price_per_1m=payload.input_price_per_1m,
            output_price_per_1m=payload.output_price_per_1m,
            effective_from=payload.effective_from,
            note=payload.note,
        )
    except DuplicatePricingError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(exc)
        ) from exc
    return ModelPricingRead.model_validate(pricing)


@router.delete("/pricing/{pricing_id}", status_code=204)
@limiter.limit(ADMIN_LIMIT)
async def delete_pricing(
    request: Request,
    pricing_id: int,
    service: PricingService = Depends(get_pricing_service),
) -> None:
    """Delete a price period. This does change historical figures."""
    await service.delete(pricing_id)


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
