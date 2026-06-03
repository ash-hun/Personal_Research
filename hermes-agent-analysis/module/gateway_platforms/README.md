# gateway_platforms — Hermes 멀티플랫폼 게이트웨이 미러

Nous Research의 **Hermes Agent**가 하나의 에이전트를 Telegram, Discord, Slack,
WhatsApp, Signal, CLI 등 여러 메신저에 동시에 연결하는 방식을 stdlib만으로 충실히
재현한 학습용 미러입니다. 외부 의존성 없이 `python3 demo.py` 한 줄로 전체 흐름이
돌아갑니다.

---

## 1. 기능 개요 — 단일 게이트웨이로 멀티플랫폼

Hermes는 플랫폼마다 봇을 따로 띄우지 않습니다. **단일 게이트웨이 프로세스**가 모든
플랫폼의 인바운드 메시지를 받아 하나의 공통 형태로 정규화하고, 같은 에이전트를
실행한 뒤, 응답을 **원래 들어온 플랫폼·채널로 정확히 되돌려** 보냅니다.

여기서 핵심은 두 가지입니다.

- **크로스플랫폼 대화 연속성**: 같은 사용자가 같은 채널에서 보낸 두 메시지는 동일한
  세션 키로 묶여 맥락이 이어집니다. 다른 사용자/다른 플랫폼은 자동으로 격리됩니다.
- **플러그형 플랫폼 레지스트리**: 새 플랫폼은 `if/elif` 분기를 고치지 않고 레지스트리에
  자기 자신을 등록(self-register)하기만 하면 게이트웨이가 발견·인스턴스화합니다.

---

## 2. Hermes 실제 구현 방식

### 정규화 (normalize)
각 플랫폼 어댑터는 플랫폼 고유 페이로드(텔레그램 `Update`, 디스코드 메시지 객체 등)를
받아 공통 타입 `MessageEvent`(미러에서는 `InboundMessage`)로 변환합니다. 게이트웨이
코어는 이 정규화된 형태만 보므로 플랫폼별 분기가 사라집니다.

### 세션 (session)
정규화된 출처 정보(`SessionSource`)로부터 **결정론적 세션 키**를 만듭니다
(`build_session_key`). DM은 `agent:main:<platform>:dm:<chat_id>` 형태로, 그룹/채널은
`chat_id`(+옵션 `thread_id`, +참여자 `user_id`)로 키를 구성합니다. 같은 출처면 항상
같은 키 → 같은 대화. 이것이 연속성의 메커니즘입니다. `SessionStore`는 이 키로 세션을
조회/생성하며, idle 타임아웃 등 reset 정책에 걸리면 같은 키를 유지한 채 `session_id`만
새로 발급해 "새 대화"를 시작합니다.

### 라우팅 (routing)
응답을 어디로 보낼지는 `DeliveryTarget`이 표현합니다. `"origin"`은 들어온 채널로 그대로
회신, `"telegram:12345"`는 특정 채팅, `"telegram"`은 홈 채널을 뜻합니다. 채팅 회신의
일반적 경우는 항상 `origin`입니다.

### delivery
`DeliveryRouter`가 `DeliveryTarget`의 플랫폼을 보고 등록된 어댑터를 찾아 `send()`를
호출합니다. 실제 Hermes는 여기서 길이 초과 분할(chunking), 로컬 파일 저장, 무한루프
방지용 silence-narration 필터링까지 처리합니다 (미러에서는 생략).

### streaming
에이전트는 "무슨 일이 일어났는지"만 기술하는 타입 이벤트(`MessageChunk`,
`ToolCallChunk`, `MessageStop` …)를 흘려보내고, **어댑터가 그것을 어떻게 렌더링할지**
결정합니다. 텔레그램은 네이티브 draft로 스트리밍, 툴 chrome을 못 그리는 플랫폼은
`ToolCallChunk`를 그냥 먹어버립니다(eat). `GatewayEventDispatcher`가 이 분배를 담당하며,
표현 계층 에러가 절대 에이전트 루프를 깨지 않도록 방어합니다.

---

## 3. 핵심 소스 파일 매핑

| 미러 구성요소 | Hermes 원본 |
| --- | --- |
| `Platform` (enum) | `gateway/config.py` → `class Platform` |
| `SessionSource` | `gateway/session.py` → `class SessionSource` |
| `InboundMessage` | `gateway/platforms/base.py` → `class MessageEvent` |
| `OutboundMessage` / `SendResult` | `gateway/platforms/base.py` → `class SendResult` |
| `Session` | `gateway/session.py` → `class SessionEntry` |
| `build_session_key` | `gateway/session.py` → `def build_session_key` |
| `SessionStore` | `gateway/session.py` → `class SessionStore` |
| `Platform_` (어댑터 인터페이스) | `gateway/platforms/base.py` → `class BasePlatformAdapter` |
| `PlatformEntry` / `PlatformRegistry` | `gateway/platform_registry.py` |
| `DeliveryTarget` / `DeliveryRouter` | `gateway/delivery.py` |
| `Gateway` (메인 루프) | `gateway/run.py` (게이트웨이 엔트리포인트) |
| `MessageChunk` 등 이벤트 | `gateway/stream_events.py` |
| `GatewayEventDispatcher` | `gateway/stream_dispatch.py` |

---

## 4. I/O 인터페이스

### InboundMessage (정규화된 인바운드)
```python
@dataclass
class InboundMessage:
    text: str
    source: SessionSource          # 출처(플랫폼/채팅/사용자/스레드)
    message_id: Optional[str] = None
    reply_to_message_id: Optional[str] = None
    media_urls: List[str] = []
    raw_message: Any = None        # 원본 플랫폼 페이로드(디버깅용)
    timestamp: datetime = now()
```

### OutboundMessage (회신) / SendResult (전송 결과)
```python
@dataclass
class OutboundMessage:
    platform: Platform
    chat_id: str
    content: str
    thread_id: Optional[str] = None
    reply_to_message_id: Optional[str] = None
    metadata: Dict[str, Any] = {}

@dataclass
class SendResult:
    success: bool
    message_id: Optional[str] = None
    error: Optional[str] = None
    retryable: bool = False
```

### Session (세션 스토어 엔트리)
```python
@dataclass
class Session:
    session_key: str               # 연속성 핸들(같은 사용자+채팅 = 같은 키)
    session_id: str                # reset 때마다 회전
    created_at: datetime
    updated_at: datetime
    origin: SessionSource          # delivery 라우팅용 최초 출처
    transcript: List[Dict[str, str]]  # 대화 기록(Hermes는 SQLite)
    ...
```

### Platform 어댑터 시그니처
```python
class Platform_(ABC):
    platform: Platform                          # 이 어댑터가 담당하는 플랫폼

    def name(self) -> str: ...
    def receive(self, raw: Any) -> InboundMessage:  # 인바운드 정규화
    def send(self, out: OutboundMessage) -> SendResult:  # 아웃바운드 전송
    # 선택: 스트리밍 렌더링 훅
    def render_message_event(self, event) -> None: ...
    def render_tool_event(self, event) -> None: ...
```

---

## 5. 데이터 흐름

```
플랫폼 네이티브 이벤트 (텔레그램 Update / 디스코드 메시지 / CLI 입력)
        │
        ▼  adapter.receive(raw)
InboundMessage  (정규화 — 플랫폼 무관 공통 형태)
        │
        ▼  Gateway.handle()
build_session_key(source)  →  SessionStore.get_or_create_session()
        │                        (같은 출처 = 같은 키 = 같은 대화 / idle 시 자동 reset)
        ▼
[/new·/reset 슬래시 커맨드는 에이전트 실행 전 가로채기]
        │
        ▼  run_agent(text, session)   ← 플러그형 에이전트 (transcript 맥락 활용)
reply 텍스트
        │
        ▼  DeliveryTarget.parse("origin", origin=source)
DeliveryRouter.deliver()  →  adapter.send(OutboundMessage)
        │
        ▼
응답이 원래 들어온 플랫폼·채널로 정확히 전달 → SendResult
```

`demo.py` 실행 시 관찰 포인트:
- 텔레그램에서 Ada가 이름을 말한 뒤 다음 메시지에서 "내 이름이 뭐야?"라고 물으면
  **같은 세션이라 기억**합니다.
- 디스코드의 다른 사용자는 **격리된 세션**이라 이름을 모릅니다.
- CLI에서 `/new`를 보내면 **세션이 리셋**되어 직전 이름을 잊습니다.

---

## 6. 커스터마이징 · 응용 포인트

### 새 플랫폼 추가 (예: IRC)
1. `Platform_`를 상속해 `receive()`(정규화)와 `send()`(전송)를 구현합니다.
2. `PlatformRegistry`에 `PlatformEntry`로 등록합니다.
   ```python
   registry.register(PlatformEntry(
       name="irc", label="IRC",
       adapter_factory=lambda cfg: IRCAdapter(cfg),
       check_fn=lambda: irc_deps_available(),   # 의존성/자격증명 게이트
       validate_config=lambda cfg: bool(cfg.server),
   ))
   ```
3. `gateway.connect_platform("irc")` — 끝. 코어 코드는 수정하지 않습니다.

`check_fn`이 `False`면 `create_adapter`는 `None`을 반환하고 게이트웨이는 그 플랫폼을
조용히 건너뜁니다(데모의 Signal 케이스). 이것이 Hermes의 플러그형 발견 메커니즘입니다.

### 그 밖의 응용
- **에이전트 교체**: `run_agent` 콜러블만 바꾸면 됩니다 (LangGraph 런, LLM 호출 등).
- **세션 정책 변경**: `ResetPolicy(mode, idle_minutes)`와 `SessionStore`의
  `group_sessions_per_user` / `thread_sessions_per_user` 플래그로 격리 단위를 조절합니다.
- **다중 전송**: `DeliveryTarget.parse("telegram:123")`처럼 원본이 아닌 다른 채널로도
  보낼 수 있어, 크론 작업 결과를 홈 채널로 푸시하는 패턴에 그대로 응용됩니다.
- **스트리밍 렌더링**: 어댑터에서 `render_message_event` / `render_tool_event`를
  오버라이드해 플랫폼별 표현(네이티브 draft, 툴 chrome 생략 등)을 구현합니다.
```
