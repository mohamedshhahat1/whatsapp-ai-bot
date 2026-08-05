"""Which channels are switched on, and the Meta credentials behind them.

Separate from :mod:`app.config` for the same reason ``inbound_config`` and
``push_config`` are: the main settings module can only be rewritten whole by
the tooling that edits it, and a previous whole-file rewrite silently dropped
a hundred lines of comments. Both read the environment identically, so
nothing about operating them differs.

WhatsApp's credentials are NOT duplicated here. They live in
``Settings.REQUIRED_IN_PRODUCTION``, which already refuses to boot production
without them; a second copy would be a second thing to keep true.

Everything new defaults OFF
---------------------------
Deploying this changes nothing until somebody sets a flag. That is the point:
the existing WhatsApp bot is live traffic for a real business, and a channel
that switched itself on at deploy time would start answering customers on a
page whose copy nobody had reviewed.
"""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict

from app.channels.constants import (
    FACEBOOK_COMMENT,
    INSTAGRAM_COMMENT,
    INSTAGRAM_DM,
    MESSENGER,
    WHATSAPP,
)


class ChannelSettings(BaseSettings):
    """Per-channel switches and the Meta app credentials they need."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Switches -----------------------------------------------------------
    #: On, because it is the channel that already exists. Turning it off is a
    #: deliberate act with an obvious consequence.
    enable_whatsapp: bool = True
    enable_messenger: bool = False
    enable_instagram_dm: bool = False
    enable_facebook_comments: bool = False
    enable_instagram_comments: bool = False

    # --- Meta credentials ---------------------------------------------------
    #: The page the bot answers as, and the token it answers with. That token
    #: is a credential: supply it by Docker secret, a *_FILE path or Vault,
    #: never a committed .env. See docs/SECRETS.md.
    facebook_page_id: str = ""
    facebook_page_access_token: str = ""
    #: Subscription handshake token for the Messenger/Instagram webhook.
    #: Falls back to the WhatsApp one through ``verify_token`` below, because
    #: a single Meta app is the normal setup and two identical values in .env
    #: is one more pair to drift apart.
    facebook_verify_token: str = ""
    #: The IG professional account behind the page, and its token. Instagram
    #: messaging is usually served by the page token; a separate one is
    #: supported for the setups where it is not.
    instagram_account_id: str = ""
    instagram_access_token: str = ""
    #: X-Hub-Signature-256 secret. One Meta app signs every surface, so this
    #: defaults to the WhatsApp app secret through ``app_secret`` rather than
    #: standing as a second copy of the same value.
    meta_app_secret: str = ""
    #: Pinned deliberately, like whatsapp_api_version: Meta ships breaking
    #: changes between versions and keeps the old ones serving.
    meta_api_version: str = "v21.0"

    # --- Comment behaviour --------------------------------------------------
    #: Answer public comments publicly at all.
    reply_to_comments: bool = True
    #: Follow a public reply with a private message inviting the customer to
    #: continue there. Off by default on both surfaces: whether a page may
    #: message a commenter is a platform-rules question with a different
    #: answer per page, and an unsolicited DM is a worse first impression
    #: than a good public answer on its own.
    facebook_comment_dm_invite: bool = False
    instagram_comment_dm_invite: bool = False
    #: Never answer the page's own comments. Without this the bot replies to
    #: itself, and every reply is another webhook.
    ignore_own_comments: bool = True

    # --- Derived ------------------------------------------------------------

    def verify_token(self, whatsapp_verify_token: str) -> str:
        """Handshake token for the Meta webhook, falling back to WhatsApp's."""
        return self.facebook_verify_token.strip() or whatsapp_verify_token

    def app_secret(self, whatsapp_app_secret: str) -> str:
        """Signing secret for the Meta webhook, falling back to WhatsApp's."""
        return self.meta_app_secret.strip() or whatsapp_app_secret

    def instagram_token(self) -> str:
        """Token for Instagram sends: its own, else the page's."""
        return self.instagram_access_token.strip() or self.facebook_page_access_token

    @property
    def switches(self) -> dict[str, bool]:
        """Channel id -> whether it is switched on."""
        return {
            WHATSAPP: self.enable_whatsapp,
            MESSENGER: self.enable_messenger,
            INSTAGRAM_DM: self.enable_instagram_dm,
            FACEBOOK_COMMENT: self.enable_facebook_comments,
            INSTAGRAM_COMMENT: self.enable_instagram_comments,
        }

    def dm_invite_enabled(self, channel: str) -> bool:
        """Whether a public reply on ``channel`` is followed by a DM."""
        return {
            FACEBOOK_COMMENT: self.facebook_comment_dm_invite,
            INSTAGRAM_COMMENT: self.instagram_comment_dm_invite,
        }.get(channel, False)


@lru_cache
def get_channel_settings() -> ChannelSettings:
    return ChannelSettings()
