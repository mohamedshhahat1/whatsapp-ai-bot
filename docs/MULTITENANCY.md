# Multi-tenancy

Status: **Phase 0 complete (discovery only). No implementation yet.**

Branch: `feat/production-saas-multitenancy`, based on `main` @ `48e1e6ba`.

This document is the working record for converting a single-tenant deployment
into a multi-tenant SaaS platform. It exists because the change touches almost
every table and every repository, and a decision made twice in two places is a
cross-tenant leak waiting to happen.

The programme extends the existing architecture. It does not replace it. Where
an existing mechanism already provides a guarantee -- reserve-before-send
idempotency, the append-only audit trigger, the one-active-conversation partial
index -- the work is to give it a tenant dimension, not a rewrite.

## Branch base

The brief named `feat/multi-channel-instagram-comments-analytics` as the
starting point. That branch was squash-merged into `main` as PR #11 before this
work began, so `main` already contains it. Branching from the merged branch
would have duplicated 5,012 lines already on `main`. Base is therefore `main`
@ `48e1e6ba`, which contains the multi-channel work, the Flutter analyzer
cleanup and the security hardening.

## Validation baseline

**UNVERIFIED.** No local checkout exists for this work; the repository is read
through the GitHub API. Tests, lint, type checks, migrations, Docker builds and
Flutter checks cannot be executed locally, so every claim in this document is
derived from reading committed source, never from running it.

GitHub Actions is the only executor. This document is committed on its own, with
no code change, specifically so that the CI run it triggers records a baseline
for this branch before any behaviour is modified. Pre-existing failures found
that way belong to the base commit and must not be attributed to later phases.

## Confirmed schema facts

15 migrations, linear `0000` to `0014`, no branch points. 12 tables.

Two constraints on any new migration:

* **Revision ids must be 32 characters or fewer.** `alembic_version.version_num`
  is `VARCHAR(32)`. Migration 0007 is named `0007_conversation_session_lifecycle`
  on disk but carries the revision id `0007_session_lifecycle`, because the long
  form executed every DDL statement and then failed on the final version stamp.
  Only transactional DDL prevented a half-migrated schema.
* **`alembic upgrade head` needs application settings, not just a database.**
  Migration 0002 calls `app.config.get_settings()` inside `upgrade()` to seed
  model prices.

## Tenant ownership

Ownership is decided per entity. `tenant_id` is not added mechanically.

| Entity | Classification | Notes |
| --- | --- | --- |
| `tenants` | GLOBAL | New. The boundary itself. |
| `tenant_memberships` | JOIN | New. Global operator to tenant, carries role. |
| `tenant_invitations` | TENANT OWNED | New. |
| `tenant_settings` | TENANT OWNED | New. |
| `operators` | USER OWNED | Login identity stays global. Tenant reach comes from membership, never from a column on the operator. |
| `operator_sessions` | USER OWNED | A session authenticates a person, not a tenant. **No `tenant_id`.** Tenant is resolved per request from membership. |
| `users` (customers) | TENANT OWNED | The same phone number can be two different tenants' customer. |
| `conversations` | TENANT OWNED | |
| `messages` | TENANT OWNED | Derived through `conversation_id` in Phase 1. Denormalising `tenant_id` onto the largest table is a performance decision that needs a measurement, not a guess. |
| `documents` | TENANT OWNED | |
| `document_chunks` | TENANT OWNED | `tenant_id` must be denormalised here. A vector index cannot be filtered through a join. See the HNSW note below. |
| `ai_logs` | TENANT OWNED | Needs its own `tenant_id`, not a derived one. See D5. |
| `audit_logs` | TENANT OWNED, nullable | NULL means a platform-level event. Keeping system events distinguishable from tenant events is a requirement, and a nullable column expresses it directly. |
| `device_tokens` | TENANT OWNED via operator | Currently ownerless. See D3. |
| `analytics_daily` | TENANT OWNED | Primary key must become `(tenant_id, day)`. |
| `model_pricing` | GLOBAL / SYSTEM | OpenAI list prices. Deployment-wide fact. **Do not tenant-scope.** Per-tenant plan pricing is a separate concept and a separate table. |
| `alembic_version` | GLOBAL / SYSTEM | |

Missing entirely, no table and no code: tenants, memberships, invitations,
tenant settings, per-tenant integrations and credentials, per-tenant AI
configuration, plans, subscriptions, trials, entitlements, usage metering,
invoices, self-service signup, durable notification inbox, per-tenant retention
configuration.

## Uniqueness constraints

Deployment-global uniqueness is the sharpest edge in the current schema.

| Constraint | Added by | Decision |
| --- | --- | --- |
| `uq_users_channel_external_id (channel, external_id)` | 0009 | Becomes `(tenant_id, channel, external_id)`. |
| `ix_users_wa_id UNIQUE (wa_id)` | 0000 | Becomes tenant-scoped **in the same migration step**. See D2 -- changing one without the other is worse than changing neither. |
| `ix_documents_source UNIQUE (source)` | 0001 | Becomes `(tenant_id, source)`. Two tenants may both upload `pricing.pdf`. |
| `analytics_daily PRIMARY KEY (day)` | 0014 | Becomes `(tenant_id, day)`. The rollup's idempotent upsert must key on both. |
| `ix_operators_username UNIQUE` | 0010 | **Unchanged.** Operators are global identities, so a globally unique username is correct. |
| `uq_device_tokens_token UNIQUE` | 0008 | **Unchanged.** A device token is globally unique by nature. |
| `uq_messages_reply_to_wa_message_id UNIQUE` | 0006 | **Deferred, deliberately.** This is the reserve-before-send anchor. See D7. |
| `uq_active_conversation_per_user (user_id) WHERE status='active'` | 0003 | **Unchanged.** Once `users` is tenant-scoped this partial index becomes tenant-correct for free. Changing it would add risk and no isolation. |
| `uq_chunk_position (document_id, chunk_index)` | 0001 | **Unchanged.** Already scoped by its parent document. |

## Confirmed defects

Each of these was read in source at `48e1e6ba`, not inferred.

### D1 -- Inbound traffic merges customers across tenants

`app/repositories/user.py`, `get_or_create` and `get_or_create_by_channel`.

Customer identity resolves on `wa_id`, or on `(channel, external_id)`, with no
tenant term. If two tenants ever serve the same phone number or the same
Instagram commenter, the second tenant's inbound message resolves to the first
tenant's `users` row, attaches to that tenant's active conversation, is answered
from that tenant's knowledge base in that tenant's persona, and appears in that
tenant's inbox.

This is not a read leak. It is a silent cross-tenant merge of customer identity
and conversation history, and it is the single highest-priority fix in the
programme.

### D2 -- The two customer uniqueness constraints must change together

`app/repositories/user.py`, `get_or_create`.

The conflict target of the upsert is deliberately unnamed, and the docstring
explains why: the row can violate either `ix_users_wa_id` or
`uq_users_channel_external_id`, Postgres reports whichever it reaches first, and
naming one leaves the other free to raise `IntegrityError`.

The consequence for this work is severe. Scoping only
`uq_users_channel_external_id` to include `tenant_id` leaves the global
`ix_users_wa_id` in force; a second tenant inserting a known phone number then
trips it, `on_conflict_do_nothing()` swallows the conflict, the re-read returns
the **other tenant's row**, and the merge in D1 happens with no error anywhere.
A partial fix here is more dangerous than no fix.

Both constraints change in one migration step, and the regression test must
cover a second tenant inserting a `wa_id` the first tenant already has.

### D3 -- Push notifications fan out to every device in the deployment

`alembic/versions/0008_device_tokens.py`, `app/core/events.py` `publish` ->
`_notify_devices`, `app/services/push_dispatcher.py`.

Migration 0008 states the position plainly: there is no `operator_id` column,
because at the time there was no operators table and nothing in the database
identified a person. Tokens are device-scoped and notifications fan out to every
enabled device.

Every event published for any tenant therefore attempts a push to every
registered phone. Operators are now real rows, so the missing owner can finally
be supplied.

### D4 -- The realtime event bus has no identity and no tenant boundary

`app/routers/events.py`, `app/core/events.py`.

The `/ws/events` WebSocket authenticates by comparing the first frame against
the deployment-wide `admin_api_key`. There is no operator identity on the
connection at all, so of the four things a connection must establish -- user,
membership, authorised tenant, authorised event scope -- it establishes none.

`CHANNEL` is a single global Redis pub/sub channel, `dashboard:events`, and the
router forwards every message on it verbatim to every connected dashboard.

The events are well designed in one important respect: they carry no message
content, no phone number and no customer name, and that rule is documented and
held. What they do carry is `conversation_id`, `user_id`, status transitions,
assigned operator names and lead tags. Across tenants that is both a metadata
leak and an enumeration oracle for ids that can then be aimed at the admin API.

**Conflict with the existing design, and the resolution.** `app/core/events.py`
justifies the single channel on the grounds that per-conversation channels would
force a resubscribe whenever an operator clicks a different row. That objection
is real, and it does not apply to per-tenant channels: an operator's tenant does
not change during a connection, so one subscription per connection still holds.
Publishing to `dashboard:events:<tenant_id>` makes the subscription itself the
boundary, which is stronger than parsing each event and filtering, because there
is no filter to get wrong. It also preserves the verbatim-forward path in the
router, which server-side filtering would have had to break.

### D5 -- AI usage records can lose their tenant

`alembic/versions/0000_initial_schema.py`, `ai_logs.conversation_id`.

The column is nullable with `ON DELETE SET NULL`. If tenant attribution is
derived through the conversation, deleting a conversation silently detaches its
usage and cost records. That is acceptable for a global cost dashboard and
unacceptable for per-tenant metering and billing, so `ai_logs` carries its own
`tenant_id`.

### D6 -- Analytics are a single global row per day

`alembic/versions/0014_analytics_daily_rollup.py`.

Primary key is `day` alone, by design: the rollup is made idempotent by writing
the same key and letting the upsert collide. Every tenant's requests, errors,
tokens, latency and cost are summed into one row. The key becomes
`(tenant_id, day)` and the rollup task upserts on both. The six CHECK
constraints stay as they are.

### D7 -- The invitation reservation key is deployment-global

`app/services/comment_invite.py`, `alembic/versions/0006_reply_idempotency.py`.

A comment-to-DM invitation is reserved as `dm_invite:<comment_id>` in
`messages.reply_to_wa_message_id`, which is globally UNIQUE. The prefix exists
because the public reply reserves the bare comment id in the same index.

Meta comment ids are globally unique, so in normal operation two tenants do not
collide. They can collide when two tenants both hold an integration on the same
Facebook page, which is legitimate in agency and reseller arrangements and
during a migration of a page between tenants. The loser is told the work is
already done and sends nothing.

This is the reserve-before-send anchor and the guarantee that Meta's one private
reply per commenter is not spent twice. It is **deferred on purpose** until the
integration ownership model exists, because a wrong change here produces
duplicate sends to customers, and duplicates are worse than the narrow collision
being fixed.

### D8 -- The admin flag exists and is never checked

`operators.is_admin`, added by 0010 and seeded true for the legacy account.

The column has existed since operator accounts landed and no endpoint consults
it. Role enforcement is the RBAC phase's job; the point recorded here is that
the hook already exists, so RBAC extends a column rather than inventing one.

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
  backfill must not map it to the default tenant, silently or otherwise.
* Platform-admin access is a distinct authorisation class from tenant-scoped
  access, not a role inside a tenant.
* Any platform-admin operation that touches tenant-owned data must establish an
  explicit authorised tenant context, and that access must be audited.

Migrating the mobile client off the shared key onto real user authentication is
a later dedicated phase and is not attempted here.

## Phase 1 plan

Revision id `0015_tenancy_foundation` (22 characters).

New tables:

* `tenants` -- id, name, slug (unique), status, created_at, updated_at. Nothing
  more; fields get added when something reads them.
* `tenant_memberships` -- tenant_id, operator_id, role, timestamps, unique on
  `(tenant_id, operator_id)`. `tenant_id` cascades. `operator_id` restricts: a
  membership is an authorisation record and should not disappear silently.
* `tenant_invitations` -- tenant_id, email, role, `token_hash`, status,
  expires_at, inviter, acceptance fields. The token is hashed and never stored,
  reusing the `operator_sessions` pattern rather than inventing a second one.
  One pending invitation per tenant and address, enforced with a partial unique
  index on `status = 'pending'`, following the `uq_active_conversation_per_user`
  precedent.
* `tenant_settings` -- deliberately minimal. Per the requirement that tenant
  settings must not silently fall back to deployment-global configuration, the
  resolver requires the caller to state whether a fallback is permitted rather
  than defaulting to one.

Backfill order, following the expand/contract sequence proven by 0013:

1. Create the tables.
2. Insert one default tenant.
3. Add `tenant_id` nullable everywhere it belongs.
4. Backfill: customers, conversations, documents, chunks, ai_logs, device
   tokens, analytics to the default tenant.
5. Assign existing operators to the default tenant by membership, mapping
   `is_admin` to `admin` and everything else to `operator`. **The legacy
   operator is skipped.** If no eligible operator exists, the default tenant is
   left without an owner and the first real owner is established explicitly
   later; an unowned tenant is a visible state, whereas quietly promoting the
   shared key would be an invisible one.
6. Validate: no NULL `tenant_id`, no orphans, row counts unchanged.
7. Add foreign keys and indexes.
8. Apply NOT NULL.
9. Swap the uniqueness constraints, with both `users` constraints in one step
   per D2.

The repository chokepoint is `ConversationService.__init__`, which constructs
`UserRepository`, `ConversationRepository` and `MessageRepository` from one
session. Threading tenant context through that constructor, and through a
tenant-scoped base repository, gives every caller isolation without relying on
each caller remembering a filter.

## Needs measurement, not a guess

`ix_document_chunks_embedding` is an HNSW index over
`Vector(1536)` with `vector_cosine_ops`, `m=16`, `ef_construction=64`, and no
tenant predicate.

Adding `WHERE tenant_id = ?` to an approximate-nearest-neighbour query does not
simply narrow it. Postgres either post-filters the index's output -- returning
fewer than `rag_top_k` rows, sometimes zero, **specifically for small tenants in
a table dominated by a large one** -- or abandons the index for a sequential
scan. A new customer with three documents is exactly the case that degrades.

Three candidate strategies, to be chosen by benchmark and not by preference:
iterative index scans, which require pgvector 0.8 or newer; partitioning
`document_chunks` by tenant with a per-partition index; or a tenant-filtered CTE
followed by exact re-ranking, which is correct and slower.

The pgvector version actually present in the Postgres image must be confirmed
before this is decided.

## Still unverified

Not read at this ref, and therefore not described anywhere above: the ten
repository modules beyond `base` and `user`; `app/core/quota.py`,
`ratelimit.py`, `idempotency.py` and `secrets.py`, which hold the Redis key
shapes that tenant namespacing depends on; `app/services/push_dispatcher.py`
beyond its call site; the entire Flutter application; `docker-compose.prod.yml`;
`.env.example`; and the test suite.

No phase that depends on those files will be designed before they are read.
