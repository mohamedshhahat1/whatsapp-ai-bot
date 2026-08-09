# Channels: the verified Meta provider contracts

This file records what was **verified against Meta's own documentation**, and
what was not. That distinction is the reason it exists.

The Messenger, Instagram DM, and comment surfaces all arrive at one URL, and
their envelopes are similar enough that a wrong assumption does not raise. It
parses, produces plausible events, and writes them under the wrong channel.
Nothing downstream can detect that afterwards, and `conversations.channel` is
what every per-channel analytics figure groups by. So each fact below carries
the URL it came from, and anything not listed here should be treated as
unverified until somebody checks it.

None of this was inferred from the existing Messenger adapter.

---

## 1. Routing: one URL, many surfaces

Meta subscribes a single webhook URL per app and names the surface in the
envelope's `object` field.

| `object` | `changes[].field` / array | Channel |
| --- | --- | --- |
| `page` | `messaging[]` | `messenger` |
| `page` | `feed` (`item: comment`) | `facebook_comment` |
| `instagram` | `messaging[]` | `instagram_dm` |
| `instagram` | `comments` | `instagram_comment` |

The object is the **only** safe thing to route on. The DM mapping lives in
`app/channels/registry.py` as `META_DM_CHANNELS`.

---

## 2. Instagram DM

### Inbound webhook

- `object: "instagram"`; `entry[].id` is **your** Instagram professional
  account id; `entry[].messaging[]` carries the items.
- `timestamp` is **epoch milliseconds** (13 digits, e.g. `1502905976377`).
- Each item has `sender.id` (IGSID), `recipient.id`, and then one of
  `message`, `postback`, `reaction`, or `read`.
- `message` fields: `mid`, `text`, `is_echo`, `is_self`, `is_deleted`,
  `is_unsupported`, `quick_reply.payload`, `reply_to`, and
  `attachments[].type` / `.payload.url`.
- Attachment types include `image`, `video`, `audio`, `file`, `share`,
  `story_mention`, `ig_reel`, `reel`, `ig_post`, `story`, `ig_story`.
- On receive, `recipient.id` is your account and `sender.id` is the customer.
  On send and on echoes, the two swap.

<https://developers.facebook.com/documentation/business-messaging/instagram-messaging/webhooks>
<https://developers.facebook.com/documentation/instagram-platform/webhooks/examples>

### Echoes and self-messages

Instagram delivers echoes **on the `messages` subscription itself**. This is a
real divergence from Messenger, which uses a separate `message_echoes` field.
Echoes carry `is_echo: true`; messages the account sent to itself carry
`is_self: true`.

Missing this means the bot answers its own replies, and each turn costs a
completion until somebody notices.

<https://developers.facebook.com/documentation/instagram-platform/self-messaging>

### Outbound send

```
POST https://graph.facebook.com/<API_VERSION>/me/messages
Authorization: Bearer <PAGE_ACCESS_TOKEN>

{"recipient": {"id": "<IGSID>"}, "messaging_type": "RESPONSE",
 "message": {"text": "..."}}
```

- The documented path is `/me/messages` (equivalently `/<PAGE_ID>/messages`).
  `me` resolves to the page the token belongs to, so **no id is interpolated
  into the URL**. This is why `REQUIRED_CREDENTIALS[instagram_dm]` asks for
  `instagram_account_id` and not `facebook_page_id`: the account id is the
  inbound echo guard, not part of the outbound path.
- Success returns `{"recipient_id": "<IGSID>", "message_id": "<MID>"}`.
- **Text must be 1,000 bytes or less** — Meta states this in *bytes*, and
  separately as "less than 1,000 characters". Arabic is two bytes per letter in
  UTF-8, so the byte reading is the binding one and a character-based clamp
  would send roughly twice the limit. `app/channels/instagram.py` clamps on
  bytes for exactly this reason. Messenger's limit is 2,000 characters; the two
  are **not** interchangeable.
- `messaging_type: "RESPONSE"` **is** valid on Instagram — it appears in Meta's
  own Instagram quick-replies sample, so it is not a Messenger-only field.

<https://developers.facebook.com/documentation/business-messaging/instagram-messaging/features/send-message>

### Quick replies

Maximum **13** replies, title truncated at **20 characters**, payload up to
1,000 characters, `content_type: "text"`, plain text only. Same numbers as
Messenger, but verified independently rather than assumed.

<https://developers.facebook.com/documentation/business-messaging/instagram-messaging/features/quick-replies>

---

## 3. Facebook Page comments — *for Step 2*

- `object: "page"`; `entry[].id` is the Page id; `entry[].changes[]` with
  `field: "feed"`.
- A comment is `field == "feed"` **and** `value.item == "comment"` **and**
  `value.verb == "add"`.
- `value` fields: `from{id,name}`, `item`, `verb`, `comment_id`, `post_id`,
  `parent_id`, `created_time`, `message`, `permalink_url`, `is_hidden`, and
  others.
- **`created_time` is epoch SECONDS, not milliseconds.** Reusing the DM
  timestamp helper (which divides by 1000) would date every comment to 1970 and
  the inbound freshness gate would silently discard all of them.
- A separate `group_feed` field exists for Page posts inside a Group. Out of
  scope.

<https://developers.facebook.com/docs/graph-api/webhooks/reference/page/>

---

## 4. Instagram comments — *for Step 3*

Two shapes exist, and **they use different keys for the comment id**:

- *Facebook Login for Business* (this repo's setup): `entry[].changes[]`,
  `field: "comments"`, `value{from{id,username}, comment_id, parent_id, text,
  media{...}}` — key is **`comment_id`**.
- *Business Login for Instagram*: `entry[]` carries `field` and `value`
  directly with no `changes` wrapper, and `value{id, from, text, media}` — key
  is **`id`**.

Accept both defensively; treat `media` as optional. `live_comments` is a
separate field. Boosted or ad posts can produce **duplicate notifications**, so
comment ingestion must be idempotent on the comment id.

<https://developers.facebook.com/docs/graph-api/webhooks/reference/instagram/>

---

## 5. Comment → DM: Private Replies — *for Steps 2 and 3*

Both surfaces use the same mechanism:

```
POST /<PAGE_ID>/messages
{"recipient": {"comment_id": "<COMMENT_ID>"}, "message": {"text": "..."}}
```

Provider-enforced limits, which are **not** optional product choices:

- **Exactly one private reply per commenter**, and only **within 7 days** of
  the comment. Duplicate-invitation protection is therefore required by the
  provider, not merely desirable.
- Continuation is only possible after the person replies, which opens the
  standard 24-hour window.
- You cannot private-reply to another Page.
- Permissions: a page token with the `MESSAGING` task and `pages_messaging`;
  Instagram additionally needs `instagram_manage_comments`.
- Delivered to the Inbox if the person follows the account, otherwise to the
  Request folder.

**The success response returns `recipient_id`** — the PSID or IGSID of the
private thread. This is the provider's own, authoritative mapping from a public
comment to a private conversation, and it is what comment-to-DM conversion must
be measured on. It must be persisted explicitly: `users` is keyed on
`(channel, external_id)`, so the same human on a comment surface and a DM
surface is deliberately two rows, and joining them by external identity would
be wrong by design.

<https://developers.facebook.com/documentation/business-messaging/messenger-platform/discovery/private-replies>
<https://developers.facebook.com/documentation/business-messaging/instagram-messaging/features/private-replies>

### Public comment replies

- Instagram: `POST /<IG_COMMENT_ID>/replies` — top-level comments only, not on
  hidden comments.
- Facebook: publish to the comment or post `/comments` edge.

<https://developers.facebook.com/docs/instagram-platform/instagram-graph-api/reference/ig-comment/replies>
<https://developers.facebook.com/docs/graph-api/reference/object/comments>

---

## 6. Still unverified

Nothing in Steps 1–3 currently depends on an unverified provider fact. If that
changes, the gap belongs in this section rather than in an assumption inside an
adapter.
