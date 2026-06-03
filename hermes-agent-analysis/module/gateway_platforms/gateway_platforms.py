"""
gateway_platforms.py — a self-contained, stdlib-only mirror of Hermes's
multi-platform messaging gateway.

This distills the *shape* of how Hermes (Nous Research) connects a single agent
process to many messaging platforms (Telegram, Discord, Slack, WhatsApp, Signal,
CLI, ...) with cross-platform conversation continuity and a pluggable platform
registry.  It is deliberately small and synchronous so a researcher can read it
top-to-bottom and customize it.

The end-to-end path mirrored here:

    inbound (platform-native) ─▶ adapter.receive() ─▶ InboundMessage (normalized)
        ─▶ Gateway.handle() ─▶ SessionStore.get_or_create_session(...)
        ─▶ run_agent(text, session)  (pluggable)
        ─▶ DeliveryRouter.deliver(reply, target=origin)
        ─▶ adapter.send() ─▶ OutboundMessage delivered to the originating channel

SOURCE MAPPING (gateway/ in the Hermes repo):
  * Platform enum                  → gateway/config.py            (class Platform)
  * SessionSource (normalized src) → gateway/session.py           (class SessionSource)
  * InboundMessage                 → gateway/platforms/base.py    (class MessageEvent)
  * OutboundMessage / SendResult   → gateway/platforms/base.py    (class SendResult)
  * Session / SessionEntry         → gateway/session.py           (class SessionEntry)
  * build_session_key              → gateway/session.py           (def build_session_key)
  * SessionStore                   → gateway/session.py           (class SessionStore)
  * Platform / BasePlatformAdapter → gateway/platforms/base.py    (class BasePlatformAdapter)
  * PlatformRegistry / Entry       → gateway/platform_registry.py (class PlatformRegistry/PlatformEntry)
  * DeliveryTarget / DeliveryRouter→ gateway/delivery.py          (class DeliveryTarget/DeliveryRouter)
  * Gateway loop                   → gateway/run.py               (the gateway entrypoint / main loop)
  * StreamEvent vocabulary         → gateway/stream_events.py     (MessageChunk, ToolCallChunk, ...)
  * GatewayEventDispatcher         → gateway/stream_dispatch.py   (class GatewayEventDispatcher)

Everything below uses only the Python standard library.
"""

from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Callable, Dict, List, Optional


def _now() -> datetime:
    """UTC clock. Mirror of gateway/session.py:_now()."""
    return datetime.now(timezone.utc)


# ─────────────────────────────────────────────────────────────────────────────
# Platform identity
# Source: gateway/config.py  (class Platform(Enum))
# ─────────────────────────────────────────────────────────────────────────────
class Platform(str, Enum):
    """Stable identity for each messaging surface.

    Hermes enumerates ~20 platforms here (telegram, discord, slack, whatsapp,
    signal, matrix, email, ...).  We mirror a representative subset.  ``LOCAL``
    is the CLI terminal — special-cased in several places just like in Hermes.
    """

    LOCAL = "local"
    TELEGRAM = "telegram"
    DISCORD = "discord"
    SLACK = "slack"
    WHATSAPP = "whatsapp"
    SIGNAL = "signal"


# ─────────────────────────────────────────────────────────────────────────────
# Normalized I/O dataclasses
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class SessionSource:
    """Describes where a message originated from.

    Source: gateway/session.py (class SessionSource).  Used to (1) route
    responses back to the right place, (2) inject context into the system
    prompt, and (3) build the deterministic session key for continuity.
    """

    platform: Platform
    chat_id: str
    chat_name: Optional[str] = None
    chat_type: str = "dm"  # "dm" | "group" | "channel" | "thread"
    user_id: Optional[str] = None
    user_name: Optional[str] = None
    thread_id: Optional[str] = None  # forum topics, Discord threads, ...
    guild_id: Optional[str] = None  # Discord guild / Slack workspace scope
    message_id: Optional[str] = None  # triggering message (for reply/react)

    @property
    def description(self) -> str:
        """Human-readable origin, mirroring SessionSource.description."""
        if self.platform == Platform.LOCAL:
            return "CLI terminal"
        if self.chat_type == "dm":
            base = f"DM with {self.user_name or self.user_id or 'user'}"
        elif self.chat_type == "group":
            base = f"group: {self.chat_name or self.chat_id}"
        elif self.chat_type == "channel":
            base = f"channel: {self.chat_name or self.chat_id}"
        else:
            base = self.chat_name or self.chat_id
        return base + (f", thread: {self.thread_id}" if self.thread_id else "")


@dataclass
class InboundMessage:
    """A platform-native message normalized into a common shape.

    Source: gateway/platforms/base.py (class MessageEvent).  Every adapter's
    ``receive()`` produces one of these regardless of the platform's wire
    format, so the gateway core never branches on platform.
    """

    text: str
    source: SessionSource
    message_id: Optional[str] = None
    reply_to_message_id: Optional[str] = None
    media_urls: List[str] = field(default_factory=list)
    raw_message: Any = None  # the original platform payload, kept for debugging
    timestamp: datetime = field(default_factory=_now)

    def is_command(self) -> bool:
        """True for slash commands like /new or /reset (MessageEvent.is_command)."""
        return self.text.startswith("/")

    def get_command(self) -> Optional[str]:
        """Extract the command name, e.g. '/new foo' -> 'new'."""
        if not self.is_command():
            return None
        parts = self.text.split(maxsplit=1)
        raw = parts[0][1:].lower() if parts else None
        if raw and "@" in raw:  # strip Telegram-style /cmd@botname
            raw = raw.split("@", 1)[0]
        return raw or None


@dataclass
class OutboundMessage:
    """A message to be delivered back to a platform/channel.

    Combines Hermes's DeliveryTarget intent (where) with the content (what).
    The adapter's ``send()`` consumes this and returns a SendResult.
    """

    platform: Platform
    chat_id: str
    content: str
    thread_id: Optional[str] = None
    reply_to_message_id: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class SendResult:
    """Result of an adapter ``send()``.

    Source: gateway/platforms/base.py (class SendResult).
    """

    success: bool
    message_id: Optional[str] = None
    error: Optional[str] = None
    retryable: bool = False


@dataclass
class Session:
    """An entry in the session store — maps a session key to a live session id.

    Source: gateway/session.py (class SessionEntry).  The ``session_key`` is the
    stable continuity handle (same user+chat ⇒ same key ⇒ same conversation);
    ``session_id`` rotates whenever the session is reset.  ``origin`` is the
    SessionSource that first created it, used for delivery routing.
    """

    session_key: str
    session_id: str
    created_at: datetime
    updated_at: datetime
    origin: SessionSource
    display_name: Optional[str] = None
    platform: Optional[Platform] = None
    chat_type: str = "dm"
    # Lightweight transcript so the demo can show real cross-message continuity.
    # In Hermes the transcript lives in SQLite (SessionDB); here it is in-memory.
    transcript: List[Dict[str, str]] = field(default_factory=list)
    was_auto_reset: bool = False
    auto_reset_reason: Optional[str] = None


# ─────────────────────────────────────────────────────────────────────────────
# Session key + store (cross-platform continuity)
# Source: gateway/session.py  (build_session_key, class SessionStore)
# ─────────────────────────────────────────────────────────────────────────────
def build_session_key(
    source: SessionSource,
    group_sessions_per_user: bool = True,
    thread_sessions_per_user: bool = False,
) -> str:
    """Build a deterministic session key from a message source.

    Source: gateway/session.py (def build_session_key).  This is the single
    source of truth for continuity:

      * DMs   → ``agent:main:<platform>:dm:<chat_id>[:<thread_id>]`` so each
                private conversation is isolated (and survives across messages).
      * Groups/channels → keyed by chat_id (+ optional thread_id), and by
                participant ``user_id`` when ``group_sessions_per_user`` is on,
                so members in the same group get their own sessions.
      * Threads default to *shared* (all participants share one session) unless
                ``thread_sessions_per_user`` is enabled.

    Because the key is derived only from the normalized SessionSource, the same
    human on the same platform/chat always lands on the same key — that is the
    mechanism behind conversation continuity.
    """
    platform = source.platform.value

    if source.chat_type == "dm":
        if source.chat_id:
            if source.thread_id:
                return f"agent:main:{platform}:dm:{source.chat_id}:{source.thread_id}"
            return f"agent:main:{platform}:dm:{source.chat_id}"
        if source.thread_id:
            return f"agent:main:{platform}:dm:{source.thread_id}"
        return f"agent:main:{platform}:dm"

    key_parts = ["agent:main", platform, source.chat_type]
    if source.chat_id:
        key_parts.append(source.chat_id)
    if source.thread_id:
        key_parts.append(source.thread_id)

    isolate_user = group_sessions_per_user
    if source.thread_id and not thread_sessions_per_user:
        isolate_user = False
    if isolate_user and source.user_id:
        key_parts.append(str(source.user_id))

    return ":".join(key_parts)


@dataclass
class ResetPolicy:
    """When an idle session should auto-reset to a fresh conversation.

    Source: gateway/config.py (SessionResetPolicy) + SessionStore._should_reset.
    """

    mode: str = "idle"  # "none" | "idle"
    idle_minutes: int = 60


class SessionStore:
    """Manages session storage and retrieval, keyed for cross-platform continuity.

    Source: gateway/session.py (class SessionStore).  Hermes persists to SQLite
    (with a JSONL fallback); here the store is an in-memory dict, which is all
    the continuity logic actually needs to demonstrate.
    """

    def __init__(
        self,
        reset_policy: Optional[ResetPolicy] = None,
        group_sessions_per_user: bool = True,
        thread_sessions_per_user: bool = False,
    ) -> None:
        self._entries: Dict[str, Session] = {}
        self.reset_policy = reset_policy or ResetPolicy()
        self.group_sessions_per_user = group_sessions_per_user
        self.thread_sessions_per_user = thread_sessions_per_user

    def _generate_session_key(self, source: SessionSource) -> str:
        return build_session_key(
            source,
            group_sessions_per_user=self.group_sessions_per_user,
            thread_sessions_per_user=self.thread_sessions_per_user,
        )

    def _should_reset(self, entry: Session) -> Optional[str]:
        """Return a reset reason if the session is stale, else None.

        Source: gateway/session.py (SessionStore._should_reset / _is_session_expired).
        """
        if self.reset_policy.mode == "none":
            return None
        deadline = entry.updated_at + timedelta(minutes=self.reset_policy.idle_minutes)
        if _now() > deadline:
            return "idle"
        return None

    def get_or_create_session(
        self, source: SessionSource, force_new: bool = False
    ) -> Session:
        """Get an existing session or create a new one.

        Source: gateway/session.py (SessionStore.get_or_create_session).
        Evaluates the reset policy to decide whether an existing session is
        stale; if so it rotates ``session_id`` (a fresh conversation) while
        keeping the same ``session_key`` (same routing identity).
        """
        session_key = self._generate_session_key(source)
        now = _now()

        if session_key in self._entries and not force_new:
            entry = self._entries[session_key]
            reset_reason = self._should_reset(entry)
            if not reset_reason:
                entry.updated_at = now
                return entry
            # Stale: rotate the session id but preserve the key.
            entry.session_id = f"{now.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}"
            entry.created_at = now
            entry.updated_at = now
            entry.transcript = []
            entry.was_auto_reset = True
            entry.auto_reset_reason = reset_reason
            return entry

        session_id = f"{now.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}"
        entry = Session(
            session_key=session_key,
            session_id=session_id,
            created_at=now,
            updated_at=now,
            origin=source,
            display_name=source.chat_name,
            platform=source.platform,
            chat_type=source.chat_type,
        )
        self._entries[session_key] = entry
        return entry

    def append_to_transcript(self, session: Session, role: str, content: str) -> None:
        """Record a turn so continuity is observable.

        Source: gateway/session.py (SessionStore.append_to_transcript).
        """
        session.transcript.append({"role": role, "content": content})
        session.updated_at = _now()

    def reset_session(self, session_key: str) -> Optional[Session]:
        """Explicit /new or /reset: rotate the session id, clear transcript.

        Source: gateway/session.py (SessionStore.reset_session).
        """
        entry = self._entries.get(session_key)
        if entry is None:
            return None
        now = _now()
        entry.session_id = f"{now.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}"
        entry.created_at = now
        entry.updated_at = now
        entry.transcript = []
        return entry

    def list_sessions(self) -> List[Session]:
        return list(self._entries.values())


# ─────────────────────────────────────────────────────────────────────────────
# Streaming event vocabulary (presentation contract)
# Source: gateway/stream_events.py + gateway/stream_dispatch.py
# ─────────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class MessageChunk:
    """A delta of streamed assistant text. Source: stream_events.MessageChunk."""

    text: str


@dataclass(frozen=True)
class ToolCallChunk:
    """A tool invocation started. Source: stream_events.ToolCallChunk."""

    tool_name: str
    preview: Optional[str] = None
    index: int = 0


@dataclass(frozen=True)
class MessageStop:
    """The assistant message segment is complete. Source: stream_events.MessageStop."""

    final: bool = False


class GatewayEventDispatcher:
    """Route typed stream events through an adapter onto a delivery sink.

    Source: gateway/stream_dispatch.py (class GatewayEventDispatcher).  The key
    idea Hermes encodes here: the *agent* emits structured events describing
    *what happened*, and the *adapter* decides *how* (or whether) to render each
    one — Telegram may stream a native draft while CLI just prints, and a
    platform that can't render tool chrome simply eats ToolCallChunk events.
    """

    def __init__(self, adapter: "Platform_", tool_mode: str = "all") -> None:
        self.adapter = adapter
        self.tool_mode = tool_mode  # "all" | "new" | "off"
        self._last_tool: Optional[str] = None

    def dispatch(self, event: Any) -> None:
        """Never raises into the agent loop (presentation must not break it)."""
        try:
            self._dispatch(event)
        except Exception:  # pragma: no cover - defensive, matches Hermes
            pass

    def _dispatch(self, event: Any) -> None:
        if isinstance(event, (MessageChunk, MessageStop)):
            self.adapter.render_message_event(event)
            return
        if isinstance(event, ToolCallChunk):
            if self.tool_mode == "off":
                return
            if self.tool_mode == "new" and event.tool_name == self._last_tool:
                return
            self._last_tool = event.tool_name
            self.adapter.render_tool_event(event)


# ─────────────────────────────────────────────────────────────────────────────
# Platform interface (per-platform adapter)
# Source: gateway/platforms/base.py (class BasePlatformAdapter)
# ─────────────────────────────────────────────────────────────────────────────
class Platform_(ABC):
    """The per-platform interface every adapter implements.

    Source: gateway/platforms/base.py (class BasePlatformAdapter).  Named with a
    trailing underscore so it does not collide with the ``Platform`` enum above
    (Hermes keeps the enum and the adapter base in separate modules).

    Two responsibilities, mirroring Hermes:
      * INBOUND  — ``receive(raw)`` normalizes a platform-native payload into an
                   InboundMessage (this is what each adapter's message handler
                   does before handing off to the gateway core).
      * OUTBOUND — ``send(out)`` delivers an OutboundMessage and returns a
                   SendResult (the abstract ``send`` in BasePlatformAdapter).

    Plus optional presentation hooks (``render_message_event`` /
    ``render_tool_event``) consumed by GatewayEventDispatcher.
    """

    #: Which Platform enum value this adapter speaks for.
    platform: Platform

    @abstractmethod
    def name(self) -> str:
        """Human/display name. Source: BasePlatformAdapter.name."""

    @abstractmethod
    def receive(self, raw: Any) -> InboundMessage:
        """Normalize a platform-native payload into an InboundMessage.

        In Hermes this is the body of each adapter's message handler (e.g. the
        Telegram adapter unwrapping a python-telegram-bot ``Update`` into a
        MessageEvent before calling ``handle_message``).
        """

    @abstractmethod
    def send(self, out: OutboundMessage) -> SendResult:
        """Deliver an OutboundMessage. Source: BasePlatformAdapter.send (abstract)."""

    # ── Optional presentation hooks (BasePlatformAdapter provides defaults) ──
    def render_message_event(self, event: Any) -> None:
        """Default rendering of a streamed assistant text event (no-op here)."""

    def render_tool_event(self, event: ToolCallChunk) -> None:
        """Default rendering of a tool-progress event (no-op here)."""


# ─────────────────────────────────────────────────────────────────────────────
# Platform registry (pluggable discovery)
# Source: gateway/platform_registry.py (PlatformEntry, PlatformRegistry)
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class PlatformEntry:
    """Metadata + factory for a single platform adapter.

    Source: gateway/platform_registry.py (class PlatformEntry).  Using a factory
    instead of a bare class lets plugins do custom init.  ``check_fn`` gates on
    dependency/credential availability; ``validate_config`` gates on
    configuration.
    """

    name: str
    label: str
    adapter_factory: Callable[[Any], Platform_]
    check_fn: Callable[[], bool] = lambda: True
    validate_config: Optional[Callable[[Any], bool]] = None
    source: str = "plugin"  # "builtin" | "plugin"
    emoji: str = "🔌"


class PlatformRegistry:
    """Central registry of platform adapters.

    Source: gateway/platform_registry.py (class PlatformRegistry).  Lets adapters
    self-register so the gateway can discover and instantiate them without a
    hardcoded if/elif chain.  Last writer wins, so a plugin can override a
    built-in.
    """

    def __init__(self) -> None:
        self._entries: Dict[str, PlatformEntry] = {}

    def register(self, entry: PlatformEntry) -> None:
        self._entries[entry.name] = entry

    def unregister(self, name: str) -> bool:
        return self._entries.pop(name, None) is not None

    def get(self, name: str) -> Optional[PlatformEntry]:
        return self._entries.get(name)

    def all_entries(self) -> List[PlatformEntry]:
        return list(self._entries.values())

    def is_registered(self, name: str) -> bool:
        return name in self._entries

    def create_adapter(self, name: str, config: Any = None) -> Optional[Platform_]:
        """Create an adapter instance, returning None if gated out.

        Source: gateway/platform_registry.py (PlatformRegistry.create_adapter).
        Returns None if no entry, ``check_fn`` fails, ``validate_config`` fails,
        or the factory raises.
        """
        entry = self._entries.get(name)
        if entry is None:
            return None
        if not entry.check_fn():
            return None
        if entry.validate_config is not None:
            try:
                if not entry.validate_config(config):
                    return None
            except Exception:
                return None
        try:
            return entry.adapter_factory(config)
        except Exception:
            return None


# ─────────────────────────────────────────────────────────────────────────────
# Delivery routing (sending responses back)
# Source: gateway/delivery.py (DeliveryTarget, DeliveryRouter)
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class DeliveryTarget:
    """Where a message should be sent.

    Source: gateway/delivery.py (class DeliveryTarget).  ``"origin"`` routes the
    reply back to the source channel — the common case for a chat reply.
    """

    platform: Platform
    chat_id: Optional[str] = None
    thread_id: Optional[str] = None
    is_origin: bool = False

    @classmethod
    def parse(
        cls, target: str, origin: Optional[SessionSource] = None
    ) -> "DeliveryTarget":
        """Parse a target string.

        Source: gateway/delivery.py (DeliveryTarget.parse).  Supported forms:
          * ``"origin"``            → back to the source channel
          * ``"telegram"``          → that platform's home channel (chat_id=None)
          * ``"telegram:12345"``    → a specific chat on that platform
          * ``"telegram:12345:67"`` → a specific chat + thread
        """
        t = target.strip().lower()
        if t == "origin":
            if origin:
                return cls(
                    platform=origin.platform,
                    chat_id=origin.chat_id,
                    thread_id=origin.thread_id,
                    is_origin=True,
                )
            return cls(platform=Platform.LOCAL, is_origin=True)
        if ":" in target.strip():
            parts = target.strip().split(":", 2)
            try:
                platform = Platform(parts[0].lower())
            except ValueError:
                return cls(platform=Platform.LOCAL)
            return cls(
                platform=platform,
                chat_id=parts[1] if len(parts) > 1 else None,
                thread_id=parts[2] if len(parts) > 2 else None,
            )
        try:
            return cls(platform=Platform(t))
        except ValueError:
            return cls(platform=Platform.LOCAL)


class DeliveryRouter:
    """Routes a response to the right platform adapter and dispatches it.

    Source: gateway/delivery.py (class DeliveryRouter).  Resolves a
    DeliveryTarget to a registered adapter and calls its ``send()``; in Hermes
    this also handles local-file delivery, chunking and silence-narration
    filtering, which we elide.
    """

    def __init__(self, adapters: Optional[Dict[Platform, Platform_]] = None) -> None:
        # NB: use ``is None`` (not ``or {}``) so a caller-supplied empty dict is
        # kept by reference — the Gateway shares its live ``adapters`` map here
        # and mutates it later via connect_platform().
        self.adapters: Dict[Platform, Platform_] = {} if adapters is None else adapters

    def deliver(self, content: str, target: DeliveryTarget) -> SendResult:
        """Deliver content to a single target.

        Source: gateway/delivery.py (DeliveryRouter.deliver / _deliver_to_platform).
        """
        adapter = self.adapters.get(target.platform)
        if adapter is None:
            return SendResult(success=False, error=f"No adapter for {target.platform.value}")
        if not target.chat_id:
            return SendResult(success=False, error=f"No chat_id for {target.platform.value}")
        out = OutboundMessage(
            platform=target.platform,
            chat_id=target.chat_id,
            content=content,
            thread_id=target.thread_id,
        )
        return adapter.send(out)


# ─────────────────────────────────────────────────────────────────────────────
# The gateway loop
# Source: gateway/run.py (the gateway entrypoint / main loop)
# ─────────────────────────────────────────────────────────────────────────────
#: A pluggable agent callable. Given the user's text and the resolved session,
#: returns the assistant's reply. In Hermes this is the full agent run
#: (run_agent.py) driven through GatewayStreamConsumer; here it is any function.
RunAgent = Callable[[str, Session], str]


class Gateway:
    """The single process that connects the agent to every platform.

    Source: gateway/run.py (the gateway main loop).  The mirrored core loop is:

        1. An adapter receives a platform-native event and normalizes it to an
           InboundMessage (``adapter.receive``).
        2. ``handle()`` resolves/creates the Session via the SessionStore using
           the deterministic session key (cross-platform continuity).
        3. The pluggable ``run_agent`` produces a reply (with the session's
           transcript available as context).
        4. The reply is routed back to the *originating* channel via the
           DeliveryRouter (target ``"origin"``).

    Slash commands (/new, /reset) are intercepted before the agent runs, exactly
    as Hermes intercepts them in run.py.
    """

    def __init__(
        self,
        registry: PlatformRegistry,
        session_store: SessionStore,
        run_agent: RunAgent,
    ) -> None:
        self.registry = registry
        self.session_store = session_store
        self.run_agent = run_agent
        self.adapters: Dict[Platform, Platform_] = {}
        self.delivery = DeliveryRouter(self.adapters)

    def connect_platform(self, name: str, config: Any = None) -> bool:
        """Instantiate and wire one platform adapter via the registry.

        Mirrors how run.py builds its ``self.adapters`` map at startup from the
        registry, then shares that map with the DeliveryRouter.
        """
        adapter = self.registry.create_adapter(name, config)
        if adapter is None:
            return False
        self.adapters[adapter.platform] = adapter
        return True

    def handle(self, platform: Platform, raw: Any) -> Optional[SendResult]:
        """Process one inbound platform event end-to-end.

        Source: gateway/run.py message pipeline (normalize → session → agent →
        deliver).  Returns the SendResult of delivering the reply, or None if
        the turn produced no reply (e.g. a handled slash command with no echo).
        """
        adapter = self.adapters.get(platform)
        if adapter is None:
            return SendResult(success=False, error=f"Platform {platform.value} not connected")

        # 1) Normalize inbound (per-platform → common InboundMessage).
        inbound = adapter.receive(raw)
        source = inbound.source

        # 2) Resolve/create the session (continuity via the session key).
        session = self.session_store.get_or_create_session(source)

        # 2a) Intercept slash commands before the agent runs (run.py behavior).
        if inbound.is_command():
            reply = self._handle_command(inbound, session)
            if reply is None:
                return None
        else:
            # 3) Run the pluggable agent with transcript context available.
            self.session_store.append_to_transcript(session, "user", inbound.text)
            reply = self.run_agent(inbound.text, session)
            self.session_store.append_to_transcript(session, "assistant", reply)

        # 4) Route the reply back to the originating channel.
        target = DeliveryTarget.parse("origin", origin=source)
        return self.delivery.deliver(reply, target)

    def _handle_command(self, inbound: InboundMessage, session: Session) -> Optional[str]:
        """Built-in slash commands. Source: run.py slash-command handlers."""
        cmd = inbound.get_command()
        if cmd in ("new", "reset"):
            self.session_store.reset_session(session.session_key)
            return "Started a fresh conversation. Previous context cleared."
        return f"Unknown command: /{cmd}"
