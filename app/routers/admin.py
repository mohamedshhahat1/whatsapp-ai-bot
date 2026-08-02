"""Admin REST API (protected by X-API-Key and rate limited per client IP)."""

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status

from app.config import Settings, get_settings
from app.core import quota
from app.core.ratelimit import ADMIN_LIMIT, limiter
from app.dependencies.deps import (
    get_admin_service,
    get_analytics_service,
    get_pricing_service,
    get_reply_service,
    require_admin,
)
from app.models.conversation import STATUS_ACTIVE, STATUS_CLOSED
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
    ConversationSummary,
    CustomerHistory,
    HandoffRequest,
)
from app.schemas.knowledge import KnowledgeDocumentRead, KnowledgeSearchHit
from app.schemas.pricing import ModelCostRead, ModelPricingCreate, ModelPricingRead
from app.schemas.quota import (
    AiToggleRequest,
    AiToggleResponse,
    QuotaStatsRead,
    UnblockResponse,
)
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
    status_filter: str | None = Query(
        None,
        alias="status",
        pattern=f"^({STATUS_ACTIVE}|{STATUS_CLOSED})$",
        description=(
            "Limit to one lifecycle status. Omit for every conversation. "
            "Note that a conversation is one SESSION, not one customer: a "
            "returning customer has several, and they are not merged."
        ),
    ),
    service: AdminService = Depends(get_admin_service),
) -> list[ConversationRead]:
    """Conversations for the operator list.

    Ordered unclaimed-sales-leads first (active ones only), then by recency.
    Recency deliberately outranks status within the second group: a session
    that ended four minutes ago is more interesting to an operator scanning
    the list than one that has been open and silent since yesterday. Filter
    with ``status=active`` for live work only.
    """
    return [
        ConversationRead.model_validate(c)
        for c in await service.list_conversations(offset, limit, status=status_filter)
    ]


@router.get("/conversations/{conversation_id}", response_model=ConversationDetail)
@limiter.limit(ADMIN_LIMIT)
async def get_conversation(
    request: Request,
    conversation_id: int,
    service: AdminService = Depends(get_admin_service),
) -> ConversationDetail:
    """One session and its full transcript.

    The transcript is this session's messages only. Earlier visits by the same
    customer are separate conversations with their own ids; see the
    ``/history`` endpoint below to find them.
    """
    return ConversationDetail.model_validate(
        await service.get_conversation(conversation_id)
    )


@router.get(
    "/conversations/{conversation_id}/history", response_model=CustomerHistory
)
@limiter.limit(ADMIN_LIMIT)
async def conversation_history(
    request: Request,
    conversation_id: int,
    limit: int = Query(20, ge=1, le=100),
    service: AdminService = Depends(get_admin_service),
) -> CustomerHistory:
    """The customer behind this session, and their previous ones.

    Sessions are never merged -- the gaps between them are meaningful -- so
    this returns navigation rather than a combined transcript: who the
    customer is, how many times they have been in touch, and their other
    sessions newest first.

    Operator-facing only. None of this is fed to the model, which still sees
    the current session alone.
    """
    user, total, previous = await service.conversation_history(
        conversation_id, limit=limit
    )
    return CustomerHistory(
        user_id=user.id,
        wa_id=user.wa_id,
        name=user.name,
        total_conversations=total,
        previous=[ConversationSummary.model_validate(c) for c in previous],
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

    A session that has already closed is REOPENED first, so the operator is
    never handed a conversation the customer cannot reply into. Returns 409
    ``conversation_superseded`` when that is impossible because the customer
    has since started a newer session -- open that one instead.
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
    """Hand the conversation back to the bot.

    Reopens a closed session first, on the same terms as ``/takeover``.
    Resuming also resets the idle timer, so the session becomes eligible for
    automatic closing again from this moment rather than immediately.
    """
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

    A closed session is reopened before the message is sent, so the reply and
    the customer's answer stay in the same conversation. Two 409s are possible
    and mean different things: ``conversation_superseded`` (the customer has
    started a newer session -- reply there) and ``outside_service_window``
    (Meta will not accept a free-form message this long after the customer's
    last one -- use a template).
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
    """Headline counters.

    ``total_conversations`` counts SESSIONS, not customers -- one returning
    customer contributes several. ``total_users`` is the customer count.
    """
    return await service.stats()


@router.get("/search", response_model=list[MessageHitRead])
@limiter.limit(ADMIN_LIMIT)
async def search_messages(
    request: Request,
    q: str = Query(..., min_length=2, description="Text to look for in messages"),
    limit: int = Query(50, ge=1, le=200),
    service: AnalyticsService = Depends(get_analytics_service),
) -> list[MessageHitRead]:
    """Full conversation search across inbound and outbound message bodies.

    Hits carry the id of the session they belong to, so the same customer can
    appear several times with different conversation ids. That is correct:
    they said it in different visits.
    """
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
    """Conversation and message counts per customer.

    The per-customer aggregate. Since sessions close, ``conversations`` here
    is a visit count rather than always 1, which makes this the right place
    to see repeat customers.
    """
    return await service.customers(offset=offset, limit=limit)


# ---------------------------------------------------------------------------
# Quota, abuse and spend protection.
#
# These read and write Redis rather than the database: the counters they
# expose are the live state of the circuit breaker, and a breaker whose
# position nobody can see will trip unannounced in the middle of a working day.
# ---------------------------------------------------------------------------


@router.get("/quota", response_model=QuotaStatsRead)
@limiter.limit(ADMIN_LIMIT)
async def quota_stats(
    request: Request,
    settings: Settings = Depends(get_settings),
) -> QuotaStatsRead:
    """Today's spend position and the state of every protection.

    Returns ``available: false`` rather than zeros when Redis cannot be
    reached. Zeros would be a lie in the most dangerous direction: they render
    as a reassuring empty chart at precisely the moment the guard has failed
    open and is protecting nothing.
    """
    return QuotaStatsRead.model_validate(await quota.usage_snapshot(settings))


@router.post("/ai-toggle", response_model=AiToggleResponse)
@limiter.limit(ADMIN_LIMIT)
async def toggle_ai(
    request: Request,
    payload: AiToggleRequest,
    settings: Settings = Depends(get_settings),
) -> AiToggleResponse:
    """Stop or resume automated replies for everyone.

    Deliberately independent of the spend ceiling. An operator who has just
    shipped a bad knowledge base, or spotted a prompt regression, needs the
    model off now -- not after editing configuration and waiting for a
    redeploy.

    Messages keep arriving, are still stored, and still appear in the
    dashboard. Only the automated answer stops; customers are routed to a
    person. The switch survives restarts because it lives in Redis, so a
    container recycling does not quietly re-enable the assistant.
    """
    try:
        await quota.set_ai_disabled(payload.disabled, settings)
    except Exception as exc:
        # Unlike the read path, this one must not fail open silently: an
        # operator who believes they have stopped the bot, and has not, will
        # act on that belief.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Could not reach Redis to change the AI switch: {exc}",
        ) from exc
    return AiToggleResponse(ai_disabled=payload.disabled)


@router.post("/customers/{wa_id}/unblock", response_model=UnblockResponse)
@limiter.limit(ADMIN_LIMIT)
async def unblock_customer(
    request: Request,
    wa_id: str,
    settings: Settings = Depends(get_settings),
) -> UnblockResponse:
    """Lift an abuse block immediately.

    Flood and spam detection are heuristics, and a genuine customer sending
    six photos of a damaged ceiling in ten seconds looks identical to a script.
    Without this an operator would have to tell a paying customer to wait
    fifteen minutes, and the real-world response to that is to turn the
    detector off entirely.

    Idempotent: unblocking someone who is not blocked returns
    ``was_blocked: false`` rather than an error.
    """
    try:
        was_blocked = await quota.unblock(wa_id, settings)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Could not reach Redis to unblock this customer: {exc}",
        ) from exc
    return UnblockResponse(wa_id=wa_id, was_blocked=was_blocked)


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
