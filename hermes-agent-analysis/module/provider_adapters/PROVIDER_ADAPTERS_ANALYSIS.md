# `provider_adapters` (ProviderProfile 시스템) 정밀 분석

> 원본: [Hermes Agent (Nous Research)](https://github.com/) — `_reference/hermes-agent/`
> 분석 대상 기능: **하나의 추론 provider를 "선언적 프로필 객체"로 기술하고, transport가 그 프로필의 hook을 호출해 provider별 quirk를 흡수하는 어댑터 메커니즘**
> 이 문서는 원본을 보지 않아도 흐름이 그려지도록 자기완결적으로 작성되었습니다. 모든 주장은 `파일:라인`으로 뒷받침됩니다.

---

## 1. 개요

Hermes Agent는 28개 이상의 추론 provider(OpenAI · Anthropic · Gemini · Bedrock · OpenRouter · Kimi · DeepSeek · xAI …)를 지원합니다. 문제는 provider마다 요청 바디의 모양이 미묘하게 다르다는 것입니다 — 누구는 `temperature`를 아예 보내면 안 되고(Kimi), 누구는 reasoning 설정을 `extra_body.reasoning`에 넣고(OpenRouter), 누구는 top-level `reasoning_effort`에 넣습니다(Kimi).

순진하게 짜면 transport 코드가 `if provider == "kimi": ... elif "openrouter": ...` 분기로 도배됩니다. Hermes는 이 분기 지옥을 **`ProviderProfile`** 이라는 선언적 dataclass 하나로 제거합니다.

> **핵심 아이디어:** provider의 모든 것(auth, endpoint, 헤더, 요청 quirk)을 한 곳(`ProviderProfile`)에 선언한다. transport는 "20개의 boolean 플래그"를 받는 대신 **프로필 객체 하나**를 받아, 정해진 hook들(`prepare_messages`, `build_extra_body`, `build_api_kwargs_extras`, `get_max_tokens`, `fixed_temperature`)만 호출한다.
>
> 프로필은 **DECLARATIVE** 하다 — provider의 동작을 *기술*할 뿐, 클라이언트 생성·크리덴셜 회전·스트리밍은 소유하지 않는다 (`providers/base.py:7-9`).

이 기능은 세 부분으로 구성됩니다.

| 부분 | 위치 | 역할 |
|---|---|---|
| **계약(스키마+hook)** | `providers/base.py:38` `ProviderProfile` | provider가 선언할 필드 + override 가능한 5개 hook 정의 |
| **레지스트리(발견+조회)** | `providers/__init__.py` | 플러그인을 lazy하게 발견·등록하고 이름/alias로 조회 |
| **소비자(hook 호출)** | `agent/transports/chat_completions.py:425` `_build_kwargs_from_profile` | 프로필 hook을 호출해 실제 API kwargs 조립 |

진입점은 두 갈래입니다.
- **조회 진입점**: `get_provider_profile(name)` — `providers/__init__.py:65`
- **소비 진입점**: `agent/chat_completion_helpers.py:701-702` 에서 프로필을 resolve → transport `_build_kwargs_from_profile` 에 위임 (`agent/transports/chat_completions.py:248-251`)

---

## 2. 입출력 인터페이스

### 2-1. 조회 인터페이스 — `get_provider_profile`

```
입력:  name: str            # provider 이름 또는 alias (예: "kimi", "or", "claude")
출력:  ProviderProfile | None   # 프로필 객체, 또는 미등록 provider면 None
```
`providers/__init__.py:65-73`. `None`이면 호출 측은 **legacy 플래그 경로**(generic OpenAI-compat)로 폴백해야 한다.

### 2-2. 소비 인터페이스 — transport `_build_kwargs_from_profile`

```
입력:
  profile:   ProviderProfile          # resolve된 프로필
  model:     str                       # 모델 ID
  sanitized: list[dict]                # codex 정제를 거친 메시지 리스트
  tools:     list[dict] | None         # OpenAI tool 정의
  params:    dict                      # 컨텍스트 (reasoning_config, session_id,
                                       #   provider_preferences, max_tokens, ...)
출력:
  api_kwargs: dict[str, Any]           # client.chat.completions.create(**api_kwargs) 에
                                       #   바로 넘길 수 있는 완성된 kwargs
```
`agent/transports/chat_completions.py:425-543`.

### 2-3. `ProviderProfile`의 선언 필드 (provider가 채우는 입력)

`providers/base.py:38-78` — 주요 필드:

| 필드 | 기본값 | 의미 |
|---|---|---|
| `name` | (필수) | canonical 이름 |
| `aliases` | `()` | 별칭들 (조회 시 canonical로 매핑) |
| `api_mode` | `"chat_completions"` | wire 프로토콜 선택 (`anthropic_messages`, `codex_responses` 등) — `base.py:44` |
| `env_vars` | `()` | API 키 환경변수명 |
| `base_url` / `models_url` | `""` | 추론 엔드포인트 / 모델 카탈로그 엔드포인트 |
| `auth_type` | `"api_key"` | `api_key\|oauth_device_code\|oauth_external\|copilot\|aws_sdk` — `base.py:56` |
| `fallback_models` | `()` | 라이브 fetch 실패 시 picker에 보여줄 큐레이션 목록 |
| `default_headers` | `{}` | 클라이언트 생성 시 1회 설정되는 헤더 |
| `fixed_temperature` | `None` | `None`=호출자 기본값, `OMIT_TEMPERATURE`=아예 안 보냄 — `base.py:73` |
| `default_max_tokens` | `None` | provider 기본 출력 상한 |

### 2-4. 5개 override hook (provider가 구현하는 동작)

| hook | 시그니처 (요약) | 기본 동작 | 정의 |
|---|---|---|---|
| `prepare_messages` | `(messages) -> messages` | pass-through | `base.py:95` |
| `build_extra_body` | `(*, session_id, **ctx) -> dict` | `{}` | `base.py:103` |
| `build_api_kwargs_extras` | `(*, reasoning_config, **ctx) -> (extra_body, top_level)` | `({}, {})` | `base.py:112` |
| `get_max_tokens` | `(model) -> int \| None` | `self.default_max_tokens` | `base.py:132` |
| `fetch_models` | `(*, api_key, timeout) -> list[str] \| None` | `{base_url}/models` GET | `base.py:146` |

---

## 3. 핵심 소스 파일 매핑

| 파일 | 역할 |
|---|---|
| `providers/base.py` | `ProviderProfile` dataclass(선언 필드) + 5개 hook의 기본 구현 + `OMIT_TEMPERATURE` 센티넬 |
| `providers/__init__.py` | 레지스트리(`_REGISTRY`/`_ALIASES`), lazy 플러그인 발견(`_discover_providers`), 조회(`get_provider_profile`/`list_providers`) |
| `plugins/model-providers/<name>/__init__.py` | 구체 프로필 — `ProviderProfile` 서브클래스 인스턴스 생성 후 `register_provider()` 호출 (예: `openrouter`, `kimi-coding`, `anthropic`) |
| `agent/chat_completion_helpers.py` | provider를 resolve(`get_provider_profile`)하고 컨텍스트를 모아 transport에 위임 (`:697-736`) |
| `agent/transports/chat_completions.py` | `build_kwargs`(진입) → 프로필 있으면 `_build_kwargs_from_profile`로 단일 경로 위임 (`:425`) |

---

## 4. step별 동작 흐름

전체 흐름은 **두 페이즈**로 나뉩니다: **(A) 발견·등록·조회** 와 **(B) 요청 조립**.

### 페이즈 A — 발견 → 등록 → 조회

#### step A0. 첫 조회가 lazy 발견을 트리거
`get_provider_profile()`(또는 `list_providers()`)가 처음 호출되면 `_discovered` 플래그를 보고 1회만 발견을 수행한다.

```python
# providers/__init__.py:65-73
def get_provider_profile(name: str) -> ProviderProfile | None:
    if not _discovered:
        _discover_providers()
    canonical = _ALIASES.get(name, name)   # alias → canonical
    return _REGISTRY.get(canonical)
```
- alias 해석: `"or"` → `"openrouter"`, `"claude"` → `"anthropic"` 등 (`:72`).
- 미등록이면 `None` 반환 → 소비자는 legacy 경로로 폴백.

#### step A1. `_discover_providers` — 3계층 import (last-writer-wins)
`providers/__init__.py:140-191`. `_discovered = True`를 **먼저** 세팅(재진입 방지, `:154`)한 뒤 3계층을 순서대로 import한다.

1. **번들 플러그인** `<repo>/plugins/model-providers/<name>/` (`:157-161`)
2. **유저 플러그인** `$HERMES_HOME/plugins/model-providers/<name>/` (`:166-171`)
3. **레거시 단일 파일** `providers/<name>.py` (back-compat, `:176-191`)

> 나중 계층이 이름 충돌 시 이긴다 → 유저가 번들 프로필을 **레포 수정 없이** 덮어쓸 수 있다 (`:163-165`). `_` 또는 `.`로 시작하는 디렉토리는 건너뛴다 (`:159`, `:169`).

#### step A2. 각 플러그인 import = self-registration
`_import_plugin_dir`(`:102-137`)가 플러그인 디렉토리의 `__init__.py`를 import하면, 그 모듈이 module-level에서 `register_provider()`를 호출한다.

```python
# providers/__init__.py:53-62
def register_provider(profile: ProviderProfile) -> None:
    _REGISTRY[profile.name] = profile        # 이름으로 등록 (나중 등록이 덮어씀)
    for alias in profile.aliases:
        _ALIASES[alias] = profile.name        # alias → canonical 매핑
```
- 번들 플러그인은 안정적 import 경로 `plugins.model_providers.<name>` 를 받고(상대 import용), 유저 플러그인은 충돌 방지를 위해 고유 이름 `_hermes_user_provider_<name>` 으로 로드된다 (`:115-119`).
- import 실패는 `try/except`로 격리되어 경고 로그만 남기고 다른 플러그인 로딩을 막지 않는다 (`:133-137`).

**예시 — 구체 프로필 등록** (`plugins/model-providers/openrouter/__init__.py:99-117`):
```python
openrouter = OpenRouterProfile(
    name="openrouter", aliases=("or",),
    env_vars=("OPENROUTER_API_KEY",),
    base_url="https://openrouter.ai/api/v1",
    models_url="https://openrouter.ai/api/v1/models",
    fallback_models=("anthropic/claude-sonnet-4.6", ...),
)
register_provider(openrouter)
```

---

### 페이즈 B — provider 선택 후 요청 조립

#### step B0. provider resolve (소비 진입점)
`agent/chat_completion_helpers.py:700-704`:
```python
try:
    from providers import get_provider_profile
    _profile = get_provider_profile(agent.provider)
except Exception:
    _profile = None
```

#### step B1. 분기 — 프로필 경로 vs legacy 경로
`agent/chat_completion_helpers.py:706`. **정상 경로**(`if _profile:`): 이미지 파트 정리 후, 컨텍스트를 한가득 실어 `build_kwargs(..., provider_profile=_profile, ...)` 호출 (`:714-736`).
- 전달되는 컨텍스트: `reasoning_config`, `session_id`, `provider_preferences`, `openrouter_min_coding_score`, `anthropic_max_output`, `supports_reasoning`, `qwen_session_metadata` 등 (`:725-735`) — 이들이 hook의 `**context`로 흘러간다.

**폴백 경로**(`:738-769`): `_profile is None`(레지스트리에 없는 완전 미지의 provider)일 때만 도달. `is_openrouter`/`is_kimi` 같은 boolean 플래그 20여 개를 직접 넘기는 옛 방식.

#### step B2. transport 진입 — codex 정제 후 단일 경로 위임
`agent/transports/chat_completions.py:192` `build_kwargs` 진입.
```python
# :244
sanitized = self.convert_messages(messages)   # codex 필드(reasoning_items/call_id 등) 제거
# :247-251
_profile = params.get("provider_profile")
if _profile:
    return self._build_kwargs_from_profile(_profile, model, sanitized, tools, params)
# 이하 :253~ 는 legacy 플래그 경로 (프로필 없을 때만)
```

#### step B3. `_build_kwargs_from_profile` — hook을 순서대로 호출
`agent/transports/chat_completions.py:425-543`. 한 줄짜리 hook 호출들이 quirk를 흡수하며 `api_kwargs`를 조립한다.

- **B3a. 메시지 전처리** (`:434`)
  `sanitized = profile.prepare_messages(sanitized)` — 기본은 pass-through(`base.py:101`).
- **B3b. developer role swap** (`:437-445`)
  GPT-5/Codex 계열 모델이면 `messages[0]`의 `role: "system"` → `"developer"` (모델명 기반, provider 무관).
- **B3c. temperature** (`:452-461`) — 3-way 분기:
  - `fixed_temperature is OMIT_TEMPERATURE` → **아예 안 넣음** (`:453-454`, Kimi 사례)
  - `fixed_temperature is not None` → 그 고정값 사용 (`:455-456`)
  - 그 외 → 호출자 `temperature`가 있으면 사용 (`:457-461`)
- **B3d. timeout / tools** (`:463-472`)
  tools가 있으면 Moonshot/Kimi 모델 한정으로 스키마 정제(`sanitize_moonshot_tools`).
- **B3e. max_tokens resolution** (`:474-491`) — 우선순위:
  `ephemeral > user_max > profile.get_max_tokens(model) > anthropic_max` (`:484-491`).
  `get_max_tokens`(`:482`)는 relay형 provider가 모델별로 다른 상한을 줄 수 있는 hook.
- **B3f. `build_api_kwargs_extras` hook** (`:495-505`)
  `(extra_body_additions, top_level)` 튜플을 반환. `top_level`은 `api_kwargs`에 바로 merge (`:505`).
  → 이 split이 존재하는 이유: OpenRouter는 reasoning을 `extra_body.reasoning`에, Kimi는 top-level `reasoning_effort`에 넣기 때문 (`base.py:120-128`).
- **B3g. `build_extra_body` hook** (`:511-520`)
  `session_id`, `provider_preferences`, `model`, `reasoning_config` 등을 받아 `extra_body` dict 생성 → merge.
- **B3h. extra_body 병합 + request_overrides** (`:522-541`)
  순서: profile_body → `extra_body_from_profile` → 호출자 `extra_body_additions` → 유저 `request_overrides`. 마지막에 `extra_body`가 비어있지 않으면 `api_kwargs["extra_body"]`로 설정 (`:540-541`).
- **B3i. 반환** (`:543`) — 완성된 `api_kwargs`.

**hook 구체화 예시 — OpenRouter** (`plugins/model-providers/openrouter/__init__.py:70-96`):
```python
def build_api_kwargs_extras(self, *, reasoning_config=None, supports_reasoning=False,
                            model=None, session_id=None, **ctx):
    extra_body = {}
    if supports_reasoning:
        extra_body["reasoning"] = dict(reasoning_config) if reasoning_config is not None \
                                  else {"enabled": True, "effort": "medium"}
    extra_headers = {}
    if session_id and model and model.startswith(("x-ai/grok-", "xai/grok-")):
        extra_headers["x-grok-conv-id"] = session_id   # xAI 캐시 핀
    return extra_body, {"extra_headers": extra_headers} if extra_headers else {}
```

**hook 구체화 예시 — Kimi** (`plugins/model-providers/kimi-coding/__init__.py:19-45, 48-57`):
- `fixed_temperature=OMIT_TEMPERATURE` (선언, `:53`) → B3c에서 temperature 완전 생략.
- `build_api_kwargs_extras`가 `extra_body["thinking"]={"type":"enabled"}` + top-level `reasoning_effort="medium"` 동시 반환 (`:28-29`).

---

## 5. 상태 전이 다이어그램

```
┌──────────────────────────── 페이즈 A: 발견·조회 (lazy, 1회) ────────────────────────────┐
│                                                                                          │
│   get_provider_profile(name)                                                             │
│            │                                                                             │
│   _discovered == False ?  ──yes──►  _discover_providers()                                │
│            │                              │ _discovered=True (재진입 방지)                │
│            │                              ├─1► 번들 플러그인 import ─┐                    │
│            │                              ├─2► 유저 플러그인 import ─┤ register_provider()│
│            │                              └─3► 레거시 .py import ────┘ → _REGISTRY/_ALIASES│
│            ▼                                                                              │
│   canonical = _ALIASES.get(name, name)                                                   │
│            │                                                                             │
│      _REGISTRY.get(canonical)                                                            │
│        ├── 있음 ─────► ProviderProfile  ───────────────┐                                 │
│        └── 없음 ─────► None ──► (legacy 플래그 경로)    │                                 │
└────────────────────────────────────────────────────────┼─────────────────────────────────┘
                                                          │
┌──────────────── 페이즈 B: 요청 조립 ────────────────────┼─────────────────────────────────┐
│                                                          ▼                                 │
│  chat_completion_helpers: _profile 있음?                                                  │
│        ├── yes ─► build_kwargs(provider_profile=_profile, **context)                      │
│        │              │                                                                   │
│        │       convert_messages() (codex 정제)                                            │
│        │              │                                                                   │
│        │       _build_kwargs_from_profile(profile, model, sanitized, tools, params)       │
│        │              ├─ prepare_messages()         ← hook                                │
│        │              ├─ developer role swap (모델명 기반)                                │
│        │              ├─ temperature: OMIT / fixed / caller                               │
│        │              ├─ max_tokens: ephemeral>user>get_max_tokens()>anthropic            │
│        │              ├─ build_api_kwargs_extras() → (extra_body, top_level)  ← hook       │
│        │              ├─ build_extra_body() → extra_body                      ← hook       │
│        │              ├─ merge: profile_body + extras + additions + overrides              │
│        │              └─► return api_kwargs ────────────────────► chat.completions.create │
│        │                                                                                   │
│        └── no  ─► (legacy 플래그 경로: is_kimi/is_openrouter/... 직접 분기)                │
└───────────────────────────────────────────────────────────────────────────────────────────┘
```

비정상/폴백 경로:
- 발견 중 플러그인 import 실패 → 경고 로그, 해당 플러그인만 누락(나머지 정상) — `__init__.py:133-137`.
- 조회 실패(미등록) → `None` → legacy 플래그 경로.
- `get_provider_profile` 자체가 예외 → `_profile=None`으로 흡수 (`chat_completion_helpers.py:703-704`).

---

## 6. 외부 서브시스템 경계

이 기능이 위임/의존하는 경계들 (잘라내지 않고 명시):

| 경계 | 위치 | 무엇을 하는가 |
|---|---|---|
| 클라이언트 생성·크리덴셜·스트리밍 | `AIAgent` (`run_agent.py`) | 프로필은 이를 **소유하지 않음** (`base.py:8-9`). 프로필은 선언만, 실제 client.create 호출/회전은 AIAgent 책임. |
| codex 메시지 정제 | `chat_completions.py:244` `convert_messages` | `_build_kwargs_from_profile` **이전**에 reasoning_items/call_id/response_item_id 제거. |
| developer role 모델 집합 | `agent.prompt_builder.DEVELOPER_ROLE_MODELS` (`chat_completions.py:17`) | GPT-5/Codex 모델 판별 집합. |
| Moonshot tool 스키마 정제 | `sanitize_moonshot_tools` / `is_moonshot_model` (`chat_completions.py:470-471`) | Kimi의 엄격한 JSON Schema flavor로 tool 정의 재작성. |
| `default_headers` 적용 | `run_agent.py:3381-3388` | URL-specific 헤더가 없을 때 `profile.default_headers`를 클라이언트 kwargs에 주입. |
| 유저 홈 경로 | `hermes_constants.get_hermes_home` (`__init__.py:94`) | 유저 플러그인 디렉토리 `$HERMES_HOME/plugins/model-providers/` 위치 확인. |
| UA 버전 | `hermes_cli.__version__` (`base.py:32`) | `fetch_models`의 User-Agent를 `hermes-cli/<ver>`로 (WAF 403 회피). |
| 모델 picker / doctor | `hermes_cli/models.py:2188`, `hermes_cli/main.py:2247-2248` | 프로필의 `fallback_models`/`fetch_models`/`supports_health_check`를 소비. |
| auxiliary client | `agent/auxiliary_client.py:249-250 외` | 압축·비전 등 보조 작업용으로 `default_aux_model`/프로필 재조회. |

---

## 7. 검증 매트릭스

분석에서 인용한 모든 라인을 원본과 대조해 재확인한 결과입니다.

| step / 주장 | 원본 위치 | 결과 |
|---|---|---|
| `OMIT_TEMPERATURE` 센티넬 | `providers/base.py:21` | ✅ |
| `ProviderProfile` dataclass | `providers/base.py:38-39` | ✅ |
| `api_mode` 기본 `chat_completions` | `providers/base.py:44` | ✅ |
| `auth_type` 종류 주석 | `providers/base.py:56` | ✅ |
| `fixed_temperature` 필드 | `providers/base.py:73` | ✅ |
| 5개 hook 정의(prepare_messages/build_extra_body/build_api_kwargs_extras/get_max_tokens/fetch_models) | `base.py:95,103,112,132,146` | ✅ |
| "프로필은 DECLARATIVE, client/회전/스트리밍 미소유" | `providers/base.py:7-9` | ✅ |
| `register_provider` 등록 | `providers/__init__.py:53-62` | ✅ |
| `get_provider_profile` + alias 해석 | `providers/__init__.py:65-73` | ✅ |
| `_discover_providers` 3계층 + `_discovered=True` 선세팅 | `providers/__init__.py:140-191`, `:154` | ✅ |
| last-writer-wins / 유저 override | `providers/__init__.py:60`, `:163-165` | ✅ |
| `_import_plugin_dir` 모듈명 분기·예외 격리 | `providers/__init__.py:115-119`, `:133-137` | ✅ |
| 소비 진입: `get_provider_profile(agent.provider)` | `agent/chat_completion_helpers.py:701-702` | ✅ |
| 프로필 경로 분기 + 컨텍스트 전달 | `agent/chat_completion_helpers.py:706-736` | ✅ |
| legacy 폴백 경로 | `agent/chat_completion_helpers.py:738-769` | ✅ |
| `convert_messages` codex 정제 | `agent/transports/chat_completions.py:244` | ✅ |
| 프로필 단일 경로 위임 | `agent/transports/chat_completions.py:247-251` | ✅ |
| `_build_kwargs_from_profile` 본문(hook 호출 순서) | `agent/transports/chat_completions.py:425-543` | ✅ |
| temperature 3-way / OMIT 처리 | `chat_completions.py:452-461` | ✅ |
| max_tokens 우선순위 + `get_max_tokens` | `chat_completions.py:474-491`, `:482` | ✅ |
| `build_api_kwargs_extras` (extra_body, top_level) split | `chat_completions.py:495-505`; 근거 주석 `base.py:120-128` | ✅ |
| `build_extra_body` 호출/merge | `chat_completions.py:511-520` | ✅ |
| OpenRouter 프로필 등록/hook | `plugins/model-providers/openrouter/__init__.py:70-117` | ✅ |
| Kimi `OMIT_TEMPERATURE` + reasoning_effort | `plugins/model-providers/kimi-coding/__init__.py:19-57` | ✅ |
| `default_headers` 주입 경계 | `run_agent.py:3381-3388` | ✅ |
| picker/doctor 소비 경계 | `hermes_cli/models.py:2188`, `hermes_cli/main.py:2247-2248` | ✅ |

> ⚠️ 표기 주의: 본 분석은 `chat_completions` (OpenAI-호환) 경로의 프로필 소비를 축으로 합니다. `api_mode="anthropic_messages"`(Anthropic) / `"codex_responses"`(Codex) 모델은 **다른 transport**로 분기하며(프로필의 `api_mode` 필드로 선택, `base.py:44`), 그 transport들의 내부 조립은 본 문서 범위 밖입니다 — 단, 그 분기 자체는 프로필 메커니즘의 일부임을 명시합니다.

---

## 부록: 한눈 요약

- **무엇:** provider별 요청 quirk를 `if/elif` 분기 대신 **선언적 프로필 객체 + 5개 hook**으로 흡수하는 어댑터 시스템.
- **어떻게 발견:** 첫 조회 시 lazy하게 번들→유저→레거시 3계층 플러그인을 import, 각자 `register_provider()`로 self-register (유저가 번들을 덮어쓸 수 있음).
- **어떻게 소비:** `get_provider_profile(provider)` → 있으면 transport `_build_kwargs_from_profile`가 hook들을 순서대로 호출해 `api_kwargs` 완성, 없으면 legacy 플래그 경로로 폴백.
- **왜 좋은가:** 새 provider 추가 = transport 수정 없이 **플러그인 디렉토리 하나** 추가. 프로필은 선언만 하고, 실행(client/스트리밍/크리덴셜)은 `AIAgent`가 소유 — 관심사 분리.
