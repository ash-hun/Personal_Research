"""
demo.py — run with ``python3 demo.py`` (no external deps).

Shows the Hermes gateway mirror end-to-end:
  * Register fake Telegram + Discord (+ CLI) platforms via the PlatformRegistry.
  * Feed a few platform-native inbound payloads from each into the Gateway.
  * A fake agent produces replies (with session transcript as context).
  * Responses are delivered back to the *correct* platform/channel.
  * Session continuity: the same user across two messages keeps context, while a
    different user / different platform gets an isolated session.
"""

from __future__ import annotations

from typing import Any, List

from gateway_platforms import (
    Gateway,
    InboundMessage,
    OutboundMessage,
    Platform,
    PlatformEntry,
    PlatformRegistry,
    Platform_,
    SendResult,
    Session,
    SessionSource,
    SessionStore,
)


# ─────────────────────────────────────────────────────────────────────────────
# Fake platform adapters. Each implements receive() (normalize inbound) and
# send() (deliver outbound). The "raw" payloads are deliberately platform-shaped
# so you can see normalization happen.
# ─────────────────────────────────────────────────────────────────────────────
class FakeTelegramAdapter(Platform_):
    """Mirrors gateway/platforms/telegram.py: unwraps an Update-like dict."""

    platform = Platform.TELEGRAM

    def __init__(self, config: Any = None) -> None:
        self.sent: List[OutboundMessage] = []

    def name(self) -> str:
        return "Telegram"

    def receive(self, raw: dict) -> InboundMessage:
        # Telegram-shaped payload: {update_id, message: {chat, from, text}}
        msg = raw["message"]
        chat = msg["chat"]
        frm = msg["from"]
        source = SessionSource(
            platform=Platform.TELEGRAM,
            chat_id=str(chat["id"]),
            chat_name=chat.get("title") or frm.get("first_name"),
            chat_type="dm" if chat.get("type") == "private" else "group",
            user_id=str(frm["id"]),
            user_name=frm.get("first_name"),
            message_id=str(msg.get("message_id")),
        )
        return InboundMessage(text=msg["text"], source=source, message_id=source.message_id, raw_message=raw)

    def send(self, out: OutboundMessage) -> SendResult:
        self.sent.append(out)
        print(f"   📨 [Telegram → chat {out.chat_id}] {out.content}")
        return SendResult(success=True, message_id=f"tg-{len(self.sent)}")


class FakeDiscordAdapter(Platform_):
    """Mirrors gateway/platforms/ (Discord): unwraps a discord.py-style payload."""

    platform = Platform.DISCORD

    def __init__(self, config: Any = None) -> None:
        self.sent: List[OutboundMessage] = []

    def name(self) -> str:
        return "Discord"

    def receive(self, raw: dict) -> InboundMessage:
        # Discord-shaped payload: {id, content, channel_id, guild_id, author}
        author = raw["author"]
        source = SessionSource(
            platform=Platform.DISCORD,
            chat_id=str(raw["channel_id"]),
            chat_name=raw.get("channel_name"),
            chat_type="channel" if raw.get("guild_id") else "dm",
            user_id=str(author["id"]),
            user_name=author.get("username"),
            guild_id=str(raw["guild_id"]) if raw.get("guild_id") else None,
            message_id=str(raw["id"]),
        )
        return InboundMessage(text=raw["content"], source=source, message_id=source.message_id, raw_message=raw)

    def send(self, out: OutboundMessage) -> SendResult:
        self.sent.append(out)
        print(f"   📨 [Discord → channel {out.chat_id}] {out.content}")
        return SendResult(success=True, message_id=f"dc-{len(self.sent)}")


class FakeCLIAdapter(Platform_):
    """Mirrors the LOCAL/CLI surface: a bare line of text from the terminal."""

    platform = Platform.LOCAL

    def __init__(self, config: Any = None) -> None:
        self.sent: List[OutboundMessage] = []

    def name(self) -> str:
        return "CLI"

    def receive(self, raw: str) -> InboundMessage:
        source = SessionSource(
            platform=Platform.LOCAL,
            chat_id="cli",
            chat_type="dm",
            user_id="local-user",
            user_name="you",
        )
        return InboundMessage(text=raw, source=source, raw_message=raw)

    def send(self, out: OutboundMessage) -> SendResult:
        self.sent.append(out)
        print(f"   📨 [CLI] {out.content}")
        return SendResult(success=True, message_id=f"cli-{len(self.sent)}")


# ─────────────────────────────────────────────────────────────────────────────
# A fake agent. Uses the session transcript so we can *observe* continuity:
# it remembers a name the user gave it earlier in the same session.
# ─────────────────────────────────────────────────────────────────────────────
def fake_run_agent(text: str, session: Session) -> str:
    lower = text.lower()
    # Remember a name if the user introduces themselves (scan prior turns too).
    remembered = None
    for turn in session.transcript:
        if turn["role"] == "user" and "my name is" in turn["content"].lower():
            remembered = turn["content"].lower().split("my name is", 1)[1].strip(" .!").title()

    if "my name is" in lower:
        name = text.lower().split("my name is", 1)[1].strip(" .!").title()
        return f"Nice to meet you, {name}! (session {session.session_id[-8:]})"
    if "what is my name" in lower or "who am i" in lower:
        if remembered:
            return f"You told me your name is {remembered}. I remember!"
        return "I don't think you've told me your name yet."
    return f"You said: '{text}'. (session {session.session_id[-8:]}, {len(session.transcript)} msgs)"


def main() -> None:
    # 1) Build the registry and self-register the fake platforms.
    registry = PlatformRegistry()
    registry.register(PlatformEntry(
        name="telegram", label="Telegram", emoji="✈️",
        adapter_factory=lambda cfg: FakeTelegramAdapter(cfg), source="builtin",
    ))
    registry.register(PlatformEntry(
        name="discord", label="Discord", emoji="🎮",
        adapter_factory=lambda cfg: FakeDiscordAdapter(cfg), source="builtin",
    ))
    registry.register(PlatformEntry(
        name="local", label="CLI", emoji="💻",
        adapter_factory=lambda cfg: FakeCLIAdapter(cfg), source="builtin",
    ))
    # A platform whose deps are unavailable → create_adapter returns None.
    registry.register(PlatformEntry(
        name="signal", label="Signal", adapter_factory=lambda cfg: None,
        check_fn=lambda: False,  # missing dependency / not configured
    ))

    print("Registered platforms:",
          ", ".join(f"{e.emoji} {e.label}" for e in registry.all_entries()))

    # 2) Build the session store and gateway with the pluggable agent.
    store = SessionStore()
    gw = Gateway(registry=registry, session_store=store, run_agent=fake_run_agent)

    for name in ("telegram", "discord", "local"):
        ok = gw.connect_platform(name)
        print(f"connect {name}: {'OK' if ok else 'FAILED'}")
    print(f"connect signal: {'OK' if gw.connect_platform('signal') else 'SKIPPED (deps missing)'}")

    # 3) Feed inbound messages from each platform.
    print("\n── Telegram: same user, two messages (continuity) ──")
    gw.handle(Platform.TELEGRAM, {
        "update_id": 1,
        "message": {"message_id": 11, "chat": {"id": 555, "type": "private"},
                    "from": {"id": 42, "first_name": "Ada"}, "text": "Hi! My name is Ada."},
    })
    gw.handle(Platform.TELEGRAM, {
        "update_id": 2,
        "message": {"message_id": 12, "chat": {"id": 555, "type": "private"},
                    "from": {"id": 42, "first_name": "Ada"}, "text": "What is my name?"},
    })

    print("\n── Discord: a different user in a guild channel (isolated session) ──")
    gw.handle(Platform.DISCORD, {
        "id": 901, "content": "Hello from Discord!", "channel_id": 7777,
        "channel_name": "general", "guild_id": 1, "author": {"id": 99, "username": "Linus"},
    })
    gw.handle(Platform.DISCORD, {
        "id": 902, "content": "what is my name?", "channel_id": 7777,
        "channel_name": "general", "guild_id": 1, "author": {"id": 99, "username": "Linus"},
    })

    print("\n── CLI: a slash command resets the session ──")
    gw.handle(Platform.LOCAL, "My name is Grace")
    gw.handle(Platform.LOCAL, "/new")
    gw.handle(Platform.LOCAL, "what is my name?")

    # 4) Show the resulting session store: continuity keys per platform/user.
    print("\n── Session store ──")
    for s in store.list_sessions():
        print(f"   key={s.session_key}")
        print(f"       id={s.session_id}  platform={s.platform.value}  turns={len(s.transcript)}")

    # Assertions so a non-zero exit signals a regression.
    sessions = store.list_sessions()
    keys = {s.session_key for s in sessions}
    assert "agent:main:telegram:dm:555" in keys, "Telegram DM continuity key missing"
    assert any(k.startswith("agent:main:discord:channel:7777") for k in keys), "Discord channel key missing"
    assert "agent:main:local:dm:cli" in keys, "CLI session key missing"
    # Telegram session kept both turns (continuity); CLI reset wiped the name.
    tg = next(s for s in sessions if s.platform == Platform.TELEGRAM)
    assert len(tg.transcript) == 4, "Telegram session should retain both turns"
    print("\nAll continuity + routing assertions passed. ✅")


if __name__ == "__main__":
    main()
