# Multi-tenancy

Status: **Phase 1b implemented.** Tenant foundation (`0015`) and tenant
ownership (`0016`) are in the schema. Application-level isolation is **not**
done; that is Phase 1c.

Branch: `feat/production-saas-multitenancy`, based on `main` @ `48e1e6ba`.

This document is the working record for converting a single-tenant deployment
into a multi-tenant SaaS platform. It exists because the change touches almost
every table and every repository, and a decision made twice in two places is a
cross-tenant leak waiting to happen.

The programme extends the existing architecture. It does not replace it. Where
an existing mechanism already provides a guarantee -- reserve-before-send
idempotency, the append-only audit trigger, the one-active-conversation partial
index -- the work is to give it a tenant dimension, not a rewrite.

## Where the work stands

| Phase | State |
| --- | --- |
| 0 -- discovery and baseline | Complete. This document's first version. |
| 1a -- tenant foundation | Complete, CI green. `0015_tenancy_foundation`: `tenants`, `tenant_memberships`, the default tenant, the platform-admin separation. |
| 1b -- tenant ownership rollout | Complete pending CI. `0016_tenant_ownership`: `tenant_id` on the business tables, backfill, keys, tenant-scoped uniqueness, and the minimum writer changes needed to keep the schema consistent. |
| 1c -- application tenant isolation | **Next.** Tenant context, repository and service scoping, API authorisation, Celery and Redis namespacing, the negative tests. |
| 2 onwards | Unchanged from the roadmap: auth/RBAC, integrations, RAG, analytics, notifications, plans, billing, onboarding, clients, hardening. |

## Branch base

The brief named `feat/multi-channel-instagram-comments-analytics` as the
starting point. That branch was squash-merged into `main` as PR #11 before this
work began, so `main` already contains it. Branching from the merged branch
would have duplicated 5,012 lines already on `main`. Base is therefore `main`
@ `48e1e6ba`, which contains the multi-channel work, the Flutter analyzer
cleanup and the security hardening.

## Validation baseline

No local checkout exists for this work; the repository is read and written
through the GitHub API. Tests, lint, type checks, migrations, Docker builds and
Flutter checks cannot be executed locally, so no claim here is derived from
running code on a workstation.

GitHub Actions is the only executor. Phase 1a's CI run is the recorded
baseline for this branch. Pre-existing failures found that way belong to the
base commit and are not attributed to later phases.

## Confirmed schema facts

17 migrations, linear `0000` to `0016`, no branch points. 14 tables.

Three constraints on any new migration:

* **Revision ids must be 32 characters or fewer.** `alembic_version.version_num`
  is `VARCHAR(32)`. Migration 0007 is named `0007_conversation_session_lifecycle`
  on disk but carries the revision id `0007_session_lifecycle`, because the long
  form executed every DDL statement and then failed on the final version stamp.
  Only transactional DDL prevented a half-migrated schema.
* **`alembic upgrade head` needs application settings, not just a database.**
  Migration 0002 calls `app.config.get_settings()` inside `upgrade()` to seed
  model prices.
* **`alembic/versions` is excluded from ruff, black and mypy.** Migrations are
  ungated by the lint job, so anything they get wrong surfaces only when they
  run. Everything under `app/` is gated and must stay formatted at 88 columns.

## Tenant ownership

Ownership is decided per entity. `tenant_id` is not added mechanically.

| Entity | Classification | State after 1b |
| --- | --- | --- |
| `tenants` | GLOBAL | The boundary itself. |
| `tenant_memberships` | JOIN | Global operator to tenant, carries role. |
| `operators` | USER OWNED | Login identity stays global. Tenant reach comes from membership, never from a column on the operator. **No `tenant_id`.** |
| `operator_sessions` | USER OWNED | A session authenticates a person, not a tenant. **No `tenant_id`.** |
| `users` (customers) | TENANT OWNED | `tenant_id` NOT NULL. Identity is tenant-scoped; the same phone number can be two tenants' customer. |
| `conversations` | TENANT OWNED | `tenant_id` NOT NULL, reached from the customer through a composite key. |
| `messages` | TENANT OWNED | `tenant_id` NOT NULL, **denormalised** -- see the revision below. |
| `documents` | TENANT OWNED | `tenant_id` NOT NULL, source unique per tenant. |
| `document_chunks` | TENANT OWNED | `tenant_id` NOT NULL, denormalised. A vector index cannot be filtered through a join. See the HNSW note. |
| `ai_logs` | TENANT OWNED | Its own `tenant_id`, not derived. See D5. |
| `audit_logs` | TENANT OWNED, nullable | NULL means platform-level or pre-tenancy. Historical rows stay NULL. See A below. |
| `device_tokens` | DEFERRED | **Untouched in 1b.** Devices belong to operators; the ownership model is completed in Phase 6 alongside the notification inbox. See D3. |
| `analytics_daily` | TENANT OWNED | Primary key is now `(tenant_id, day)`. |
| `model_pricing` | GLOBAL / SYSTEM | OpenAI list prices. Deployment-wide fact. **Do not tenant-scope.** Per-tenant plan pricing is a separate concept and a separate table. |
| `alembic_version` | GLOBAL / SYSTEM | |

Still missing entirely, no table and no code: invitations, tenant settings,
per-tenant integrations and credentials, per-tenant AI configuration, plans,
subscriptions, trials, entitlements, usage metering, invoices, self-service
signup, durable notification inbox, per-tenant retention configuration. Each
belongs to its own phase and none was pulled forward into 1b.

### Revision: `messages` carries its own `tenant_id`

Phase 0 recorded that messages would derive tenancy through `conversation_id`,
and that denormalising onto the largest table needed a measurement rather than
a guess. That was overruled deliberately, and the reason is correctness rather
than performance.

With the column present, `(tenant_id, conversation_id)` can reference
`conversations (tenant_id, id)`, and a message whose tenant disagrees with its
conversation's becomes **unwritable**. Deriving the tenant through a join gives
no such guarantee: every future query would have to remember the join, and the
one that forgets is a leak. The composite key is enforced by Postgres on every
write, which is worth more during a phase where the application layer is
explicitly not yet scoped.

The write path pays nothing for it. `claim_inbound` and `reserve_reply` read
the parent's tenant with a scalar subquery **inside the same INSERT**, so both
remain single statements and both keep their single-column conflict targets.

## Uniqueness constraints

Deployment-global uniqueness was the sharpest edge in the schema.

| Constraint | Added by | Outcome in 1b |
| --- | --- | --- |
| `uq_users_channel_external_id` | 0009 | Now `(tenant_id, channel, external_id)`. **Same name, still a constraint**, because `get_or_create_by_channel` names it in `ON CONFLICT ON CONSTRAINT`. |
| `ix_users_wa_id UNIQUE (wa_id)` | 0000 | Uniqueness moved to `uq_users_tenant_wa_id (tenant_id, wa_id)`. The index keeps its name and its lookup, and is no longer unique. Changed **in the same step** as the constraint above. See D2. |
| `ix_documents_source UNIQUE (source)` | 0001 | Uniqueness moved to `uq_documents_tenant_source (tenant_id, source)`; the index keeps its name, no longer unique. |
| `analytics_daily PRIMARY KEY (day)` | 0014 | Now `pk_analytics_daily (tenant_id, day)`. The rollup upserts on both. |
| `ix_messages_wa_message_id UNIQUE` | 0000 | **Unchanged.** Reserve-before-send anchor. |
| `uq_messages_reply_to_wa_message_id` | 0006 | **Unchanged.** Reserve-before-send anchor. See D7. |
| `ix_operators_username UNIQUE` | 0010 | **Unchanged.** Operators are global identities. |
| `uq_device_tokens_token UNIQUE` | 0008 | **Unchanged.** A device token is globally unique by nature. |
| `uq_active_conversation_per_user` | 0003 | **Unchanged.** `user_id` is itself tenant-scoped now, so one active conversation per user row already means one per person per tenant. |
| `uq_chunk_position` | 0001 | **Unchanged.** Already scoped by its parent document. |

Both message anchors stay global on purpose. Meta's ids are globally unique, so
the tenant adds nothing to either key -- and a conflict target that fails to
fire means a customer is answered twice. Widening them would weaken
idempotency to buy nothing.

## Phase 1b: what `0016_tenant_ownership` does

Upgrade, in order:

1. Add `tenant_id` nullable to the seven owned tables and to `audit_logs`.
2. Backfill **parent before child**: customers to the default tenant, then
   conversations from their customer, then messages from their conversation,
   documents, chunks from their document, `ai_logs` from their conversation and
   detached `ai_logs` to the default tenant, then `analytics_daily`.
3. Assert nothing is left unbackfilled, and fail loudly if it is.
4. `SET NOT NULL` on the seven owned tables. `audit_logs` stays nullable.
5. Index `tenant_id` on every table that gained it except `analytics_daily`,
   where it leads the primary key already.
6. Tenant foreign keys, `ON DELETE RESTRICT`: a tenant with data cannot be
   deleted out from under it.
7. `(tenant_id, id)` unique constraints on `users`, `conversations` and
   `documents`, so the composite child keys have something to reference.
8. Replace the three parent keys with composite ones, **preserving
   `ON DELETE CASCADE`**.
9. Swap the uniqueness constraints, both `users` constraints together.
10. Repoint the `analytics_daily` primary key to `(tenant_id, day)`.

The default tenant is resolved by slug, falling back to the lowest id, and the
unique-constraint drops are driven from `pg_constraint` rather than from
assumed names. A migration that guesses a constraint name fails on exactly the
deployments it was written for.

### A -- why `audit_logs` is nullable and backfilled to nothing

`0010` installs `audit_logs_no_change`, a `BEFORE UPDATE OR DELETE ... FOR EACH
ROW` trigger, and `0012` rewrites the function so DELETE is permitted only
while `audit.allow_purge` is set -- while **UPDATE raises unconditionally**.

So historical rows cannot be attributed, at all, without weakening the one
control whose entire value is that it has no exceptions. The column is
therefore nullable, historical rows keep NULL, and NULL is documented as a
real value meaning "platform-level, or before tenancy". An unattributed old
row is a far better trade than a trigger with a bypass in it.

### The downgrade contract

`downgrade()` **refuses** when more than one tenant owns data, naming the
tenants it found. This is not caution, it is arithmetic: the old
`analytics_daily` primary key is `day` alone, so two tenants' rows for one day
cannot both exist, and the old global unique indexes cannot hold two tenants'
customers with the same phone number. There is no correct way to keep both.

It never deletes, merges or rewrites a row to make itself succeed. With a
single tenant it reverses every step and drops the columns; with more than one
it stops and says so. A test asserts, at the source level, that no data
statement was ever added to "fix" it.

### Writer changes included, and the ones deliberately excluded

Only what the schema makes mandatory:

* `UserRepository.get_or_create` and `get_or_create_by_channel` resolve a
  tenant, insert with it, and **re-read within it**. Without this, the second
  tenant's insert trips a unique constraint, `on_conflict_do_nothing()`
  swallows it, and the re-read returns the other tenant's customer. That is
  D1, and it is a write-path defect, not a read-path one.
* `DocumentRepository.get_by_source` is scoped because `upsert` and
  `delete_by_source` are built on it -- an unscoped lookup makes a second
  tenant's upload a silent overwrite. `replace_chunks` takes the tenant from
  the document, never from an argument.
* `MessageRepository` and `ConversationRepository` derive the tenant from the
  parent row inside the INSERT.
* `AnalyticsRollupRepository` writes one row per tenant per day.
* `AILogRepository.create` inherits the conversation's tenant when there is
  one, and falls back to the default when there is not.

Every one of these takes `tenant_id` as an **optional** keyword defaulting to
the deployment's original tenant, so no service or worker file needed to
change and the single-tenant development path behaves exactly as before.

Deliberately **not** done here, and left to 1c: `get_by_wa_id`,
`get_by_channel_id`, `list_documents`, `count_chunks` and the vector `search`
stay unscoped; there is no tenant context object, no request-level resolution,
no Redis namespacing, no Celery context, no API authorisation.

## Confirmed defects

Each of these was read in source, not inferred.

### D1 -- Inbound traffic merges customers across tenants

**Fixed in 1b, on the write path.** Customer identity resolved on `wa_id`, or
on `(channel, external_id)`, with no tenant term, so a second tenant's inbound
message resolved to the first tenant's `users` row, attached to that tenant's
active conversation, was answered from that tenant's knowledge base in that
tenant's persona, and appeared in that tenant's inbox. Not a read leak: a
silent cross-tenant merge of customer identity and conversation history.

The uniqueness constraints and both writers now carry the tenant. Reads remain
unscoped until 1c.

### D2 -- The two customer uniqueness constraints must change together

**Fixed in 1b, in one step.** The upsert's conflict target is deliberately
unnamed, because the row can violate either `ix_users_wa_id` or
`uq_users_channel_external_id` and Postgres reports whichever it reaches first.
Scoping only one leaves the other global: the second tenant trips it,
`on_conflict_do_nothing()` swallows the conflict, and the re-read returns the
wrong tenant's row with no error anywhere. A partial fix would have been more
dangerous than none.

### D3 -- Push notifications fan out to every device in the deployment

**Open, deferred to Phase 6.** There is no `operator_id` on `device_tokens`;
tokens are device-scoped and notifications fan out to every enabled device.
Operators are real rows now, so the missing owner can finally be supplied --
but it belongs with the notification inbox rather than with a schema change,
and `device_tokens` is untouched in 1b.

### D4 -- The realtime event bus has no identity and no tenant boundary

**Open, Phase 1c.** `/ws/events` authenticates by comparing the first frame
against the deployment-wide `admin_api_key`, so of the four things a connection
must establish -- user, membership, authorised tenant, authorised event scope
-- it establishes none. `CHANNEL` is one global Redis channel,
`dashboard:events`, forwarded verbatim to every dashboard.

The events carry no message content, no phone number and no customer name, and
that rule is documented and held. They do carry `conversation_id`, `user_id`,
status transitions, operator names and lead tags -- across tenants, both a
metadata leak and an enumeration oracle for ids that can then be aimed at the
admin API.

`app/core/events.py` justifies the single channel on the grounds that
per-conversation channels would force a resubscribe whenever an operator clicks
a different row. That objection is real and does not apply to per-tenant
channels: an operator's tenant does not change during a connection. Publishing
to `dashboard:events:<tenant_id>` makes the subscription itself the boundary,
which is stronger than filtering each event because there is no filter to get
wrong, and it preserves the verbatim-forward path.

### D5 -- AI usage records can lose their tenant

**Fixed in 1b.** `ai_logs.conversation_id` is nullable with `ON DELETE SET
NULL`, so tenancy derived through the conversation would detach usage and cost
records when a conversation is deleted -- acceptable for a global cost
dashboard, unacceptable for per-tenant metering and billing.

`ai_logs` therefore has its own `tenant_id` with a **single-column** foreign
key. Composite would have nulled the tenant alongside the conversation, which
is the defect restated rather than fixed.

### D6 -- Analytics are a single global row per day

**Fixed in 1b.** The key is now `(tenant_id, day)` and the rollup upserts on
both. The six CHECK constraints are unchanged.

The rollup is **not** a `GROUP BY tenant_id`, and the existing test suite is
why. A grouped aggregate over an empty range returns no rows, so an idle day
would vanish -- and `test_a_day_with_no_activity_is_stored_as_zeros` exists
because "rolled up, nothing happened" must stay distinguishable from "never
ran", which is the only way a stalled scheduler is visible. The statement is
driven `FROM tenants` with the aggregates LEFT JOINed and `COALESCE` supplying
the zeros. Still one statement, one round trip, any number of tenants.

### D7 -- The invitation reservation key is deployment-global

**Open, deferred to Phase 3 on purpose.** A comment-to-DM invitation is
reserved as `dm_invite:<comment_id>` in `messages.reply_to_wa_message_id`,
which is globally UNIQUE. Meta comment ids are globally unique, so two tenants
collide only when both hold an integration on the same Facebook page --
legitimate in agency and reseller arrangements, and during a page migration.
The loser is told the work is already done and sends nothing.

This is the reserve-before-send anchor and the guarantee that Meta's one
private reply per commenter is not spent twice. It waits for the integration
ownership model, because a wrong change here produces duplicate sends to
customers, and duplicates are worse than the narrow collision being fixed.

### D8 -- The admin flag exists and is never checked

**Open, Phase 2.** `operators.is_admin` has existed since 0010 and no endpoint
consults it. 1a uses it to choose the default tenant's owner. RBAC extends a
column rather than inventing one.

### D9 -- The restore drill stopped proving data restoration

**Fixed in 1b.** Found while checking whether 1b would break the drill: it was
already broken. The seed inserted `users (wa_id, name, created_at)`, and
`0013` made `external_id` NOT NULL with no default, so the insert had been
failing since then -- swallowed by a trailing
`|| echo "::warning::seed insert skipped"`. The job stayed green and every
later step verified an empty database restoring as an empty database.

The seed now inserts a customer, a conversation and a message with real
foreign keys between them, resolves the tenant from `tenants`, and has no
fallback. A following step fails the job if any of the three tables is empty,
or if the rows are not referentially connected. A warning nobody reads is not
a check.

## Architectural decisions

### Naming

`tenants`, `tenant_id`, `tenant_memberships`. Used in schema, code and API
without synonyms.

### Login identity

Operators stay **global**. Tenant reach comes from `tenant_memberships`, never
from a column on `operators`. One person can therefore hold membership in
several tenants without duplicate credentials, and `ix_operators_username`
remains globally unique and correct.

`operator_sessions` gets no `tenant_id`. A session proves who you are; which
tenant you are acting in is resolved per request from membership. Binding a
tenant into the session would make the boundary depend on when the token was
issued.

### Platform administration

The shared `ADMIN_API_KEY` and its seeded `legacy-api-key` operator remain a
**platform-level** administrative identity for now. They are not a tenant user:

* **No `tenant_memberships` row is created for the legacy operator.** The
  backfill does not map it to the default tenant, silently or otherwise.
* Platform-admin access is a distinct authorisation class from tenant-scoped
  access, not a role inside a tenant.
* Any platform-admin operation that touches tenant-owned data must establish an
  explicit authorised tenant context, and that access must be audited.

Migrating the mobile client off the shared key onto real user authentication is
a later dedicated phase.

### The default tenant and the empty-database path

`0015` seeds one tenant, and `0016` attaches existing data to it. An empty
database legitimately ends with a tenant that has zero memberships -- there is
nobody to make an owner, and inventing one from the shared admin key would be
an invisible privilege grant. Populated data with no eligible administrator
fails explicitly instead.

Every tenant-aware writer defaults to that tenant, so a single-tenant
deployment and the development path behave exactly as they did before.

## Needs measurement, not a guess

`ix_document_chunks_embedding` is an HNSW index over `Vector(1536)` with
`vector_cosine_ops`, `m=16`, `ef_construction=64`, and no tenant predicate.

`document_chunks.tenant_id` now exists, so filtering is *possible*. It is
deliberately not applied: adding `WHERE tenant_id = ?` to an approximate
nearest-neighbour query does not simply narrow it. Postgres either post-filters
the index's output -- returning fewer than `rag_top_k` rows, sometimes zero,
**specifically for small tenants in a table dominated by a large one** -- or
abandons the index for a sequential scan. A new customer with three documents
is exactly the case that degrades, so a filter added here without a benchmark
would quietly return fewer results than asked for.

Three candidate strategies, to be chosen by benchmark and not by preference:
iterative index scans, which require pgvector 0.8 or newer; partitioning
`document_chunks` by tenant with a per-partition index; or a tenant-filtered
CTE followed by exact re-ranking, which is correct and slower.

The pgvector version actually present in the Postgres image must be confirmed
before this is decided. It has not been.

## Still unverified

Not read, and therefore not described anywhere above: `app/repositories/
analytics.py`, whose cost expressions the rollup imports; `app/core/quota.py`,
`ratelimit.py`, `idempotency.py`, `secrets.py` and `events.py`, which hold the
Redis key shapes that tenant namespacing depends on; `app/services/
push_dispatcher.py` and `comment_invite.py` beyond their call sites; the
routers; the entire Flutter application; `docker-compose.prod.yml`;
`.env.example`; `scripts/restore-drill.sh` itself, as opposed to the workflow
that invokes it.

No phase that depends on those files will be designed before they are read.
