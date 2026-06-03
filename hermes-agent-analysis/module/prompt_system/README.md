# prompt_system — Hermes Agent 시스템 프롬프트 조립 & 캐싱 미러

Hermes Agent(Nous Research)가 **시스템 프롬프트를 어떻게 조립하고, 어디에 캐시 경계(cache breakpoint)를 찍는지**를 표준 라이브러리만으로 재현한 자기완결형 모듈입니다. 실제 동작 메커니즘을 그대로 옮기되, 외부 의존성 없이 바로 돌려보며 이해할 수 있게 만들었습니다.

```bash
python3 demo.py   # 외부 의존성 없음, exit 0
```

---

## 1. 기능 개요

LLM에 보내는 한 번의 요청은 보통 이렇게 생겼습니다.

```
[system 메시지]  ← 거대하고 안정적인 프롬프트 (정체성 + 도구 안내 + 스킬 + 환경)
[user / assistant ...]  ← 대화 히스토리
```

문제는 **이 system 프롬프트가 매 턴마다 다시 토큰화되어 청구된다**는 점입니다. 멀티턴 대화에서는 같은 수천 토큰을 계속 다시 보내게 되죠.

Hermes의 해법은 두 가지입니다.

1. **세션당 한 번만 조립**한다. 시스템 프롬프트는 세션 시작 시 한 번 만들어 모든 턴에서 그대로 재사용하고, **컨텍스트 압축(compression) 이벤트일 때만** 다시 만듭니다. byte 단위로 동일한 프리픽스를 유지하면 상위 프로바이더의 prefix KV-cache가 계속 warm 상태로 유지됩니다.
2. **변하는 정도에 따라 섹션을 3계층으로 묶고**, 캐시에 친화적인 순서(안정적인 것 먼저)로 배치한 뒤, Anthropic `cache_control` 마커를 가장 크고 안정적인 프리픽스에 찍습니다. 멀티턴 입력 토큰 비용이 약 75% 절감됩니다.

이 모듈은 그 `PromptBuilder`(섹션 조립)와 `apply_anthropic_cache_control`(캐시 경계 삽입) 두 축을 그대로 미러링합니다.

---

## 2. Hermes 실제 구현 방식

### 섹션 구성 — 3계층 (tier)

`agent/system_prompt.py`의 `build_system_prompt_parts()`는 모든 섹션을 **변하는 빈도**에 따라 세 묶음으로 나눕니다.

| tier | 변하는 빈도 | 포함 섹션 | 캐시 가능? |
|------|------------|----------|-----------|
| **stable** | 에이전트 수명 내내 고정 | 정체성(SOUL.md 또는 DEFAULT_AGENT_IDENTITY), Hermes 셀프-헬프, 작업완수 가이드, 도구별 가이드(memory/session_search/skills), tool-use enforcement, 스킬 인덱스, 환경 힌트, 플랫폼 힌트 | ✅ |
| **context** | 세션마다 다를 수 있음 | 호출자 system_message, cwd에서 발견한 프로젝트 파일(AGENTS.md / CLAUDE.md / .cursorrules) | ✅ |
| **volatile** | 세션·턴마다 변함 | 메모리 스냅샷, USER.md 프로필, **날짜 단위** 타임스탬프/모델/프로바이더 줄 | ❌ |

### 섹션 순서 — 왜 이 순서인가

캐시에 가장 친화적인 순서, 즉 **안정적인 것 → 세션-안정 → 휘발성** 순으로 배치합니다(`build_system_prompt()`가 `stable → context → volatile`를 `"\n\n"`로 join). 휘발성 부분이 **맨 뒤**에 와야, 그 앞의 거대한 캐시 가능 프리픽스를 깨뜨리지 않습니다.

핵심 디테일 하나: 타임스탬프를 **분 단위가 아니라 날짜 단위**로 찍습니다(`agent/system_prompt.py:323-338`, Hermes PR #20451). 분 단위면 매 rebuild마다 프롬프트가 달라져 prefix 캐시가 무효화되기 때문입니다. 모델이 정확한 시각이 필요하면 도구로 조회하면 됩니다.

또 하나: 도구별 가이드는 **해당 도구 이름이 `valid_tool_names`에 있을 때만** 주입됩니다(`memory` 도구가 없으면 MEMORY_GUIDANCE도 없음). tool-use enforcement는 **모델 패밀리**(gpt/codex/gemini/grok/glm/qwen/deepseek 등)에 매칭될 때만 들어갑니다.

### 캐싱 전략 — "system_and_3"

`agent/prompt_caching.py`의 `apply_anthropic_cache_control()`는 **최대 4개의 `cache_control` 브레이크포인트**를 찍습니다.

- **1개**: system 메시지 → stable+context 프리픽스 전체를 한 방에 캐싱
- **3개**: 마지막 비-system 메시지 3개 → 대화가 길어질 때 최근 턴을 굴러가며(rolling) 캐싱

TTL은 `5m`(기본) 또는 `1h`. 모든 브레이크포인트는 동일 TTL을 씁니다. 메시지 content가 문자열이면 단일 text 블록으로 승격시키며 마커를 붙이고, 리스트면 마지막 블록에 붙입니다.

---

## 3. 핵심 소스 파일 매핑

| 이 미러의 요소 | Hermes 원본 |
|---------------|------------|
| `PromptBuilder.build()` / 3계층 순서 | `agent/system_prompt.py` → `build_system_prompt_parts()` (L61-344) |
| `render_system_text()` | `agent/system_prompt.py` → `build_system_prompt()` (L347-363) |
| 도구·모델 게이팅 (tool guidance / enforcement) | `agent/system_prompt.py` L110-195 |
| `_render_skills_index()` | `agent/prompt_builder.py` → `build_skills_system_prompt()` (L1040-1271) |
| `_render_env_hints()` | `agent/prompt_builder.py` → `build_environment_hints()` (L767-) |
| context_files 처리 | `agent/prompt_builder.py` → `build_context_files_prompt()` (L1469-1508) |
| 가이드 상수(DEFAULT_AGENT_IDENTITY 등) | `agent/prompt_builder.py` L121-443 |
| `apply_anthropic_cache_control()` / `_apply_cache_marker()` / `_build_marker()` | `agent/prompt_caching.py` 전체 (L1-79) |
| (참고) 서브디렉토리 힌트는 system 프롬프트를 건드리지 않고 tool result에 붙임 | `agent/subdirectory_hints.py` — 캐시를 깨지 않으려는 같은 철학 |

---

## 4. I/O 인터페이스

모든 입출력은 타입 명시된 `@dataclass`입니다.

**입력 섹션 타입 →**
- `ToolSpec(name, description)` — `name`만 가이드 게이팅에 쓰임(스키마는 API에 별도 전송)
- `SkillSpec(name, description, category)` — 스킬 인덱스 한 줄
- `MemorySnapshot(memory_facts, user_profile)` — 휘발성 메모리
- `EnvHints(host, home, cwd, platform)` — 환경/채널 힌트
- (그 외) `system_message: str`, `context_files: dict[파일명, 내용]`, `conversation: list[message]`

**→ `BuiltPrompt` 출력**
- `sections: list[PromptSection]` — 정렬된 섹션들 (`name`, `tier`, `text`, `cacheable`)
- `system_text: str` — 최종 system 프롬프트 문자열
- `messages: list[dict]` — `cache_control`이 주입된 API 메시지 리스트
- `breakpoints: list[CacheBreakpoint]` — 각 마커가 어디에/왜 찍혔는지 (`message_index`, `role`, `ttl`, `reason`)
- `stable_prefix_chars: int` — 캐시 가능 프리픽스(stable+context) 크기

진입점은 `build_prompt(builder, ...) -> BuiltPrompt` 하나입니다.

---

## 5. 데이터 흐름

```
ToolSpec / SkillSpec / MemorySnapshot / EnvHints / system_message / context_files
        │
        ▼
PromptBuilder.build()
   ├─ _build_stable_sections()    → 정체성·가이드·스킬·환경  (CACHEABLE)
   ├─ _build_context_sections()   → system_message·프로젝트파일 (CACHEABLE)
   └─ _build_volatile_sections()  → 메모리·USER·날짜타임스탬프  (DYNAMIC)
        │  (TIER_ORDER로 stable→context→volatile 정렬 보장)
        ▼
render_system_text()  → "\n\n".join(섹션)  = system_text
        │
        ▼
messages = [{system: system_text}] + conversation
        │
        ▼
apply_anthropic_cache_control(messages, ttl)
   ├─ system 메시지에 마커 1개       (프리픽스 전체 캐싱)
   └─ 마지막 비-system 3개에 마커     (rolling 캐싱)
        │
        ▼
BuiltPrompt(sections, system_text, messages, breakpoints, stable_prefix_chars)
```

`demo.py`의 섹션 4는 **턴이 바뀌어도 stable tier가 byte 단위로 동일**함을 검증합니다 — 이것이 캐시가 warm하게 유지되는 핵심 증거입니다.

---

## 6. 커스터마이징 · 응용 포인트

**섹션 추가하기.** `_build_stable_sections()`(또는 context/volatile)에 `PromptSection(name, tier, text, cacheable)` 하나를 추가하면 끝입니다. 추가하는 tier에 따라 자동으로 올바른 위치(=올바른 캐시 측)에 들어갑니다. 단, **자주 바뀌는 내용은 반드시 `volatile`로** 넣으세요 — stable에 넣으면 매 턴 프리픽스 캐시를 깨뜨립니다.

**도구/모델 게이팅 바꾸기.** 특정 도구가 있을 때만 가이드를 넣고 싶다면 `_build_stable_sections()`의 `if "도구이름" in self.valid_tool_names:` 패턴을 따르세요. 모델 패밀리 기반 주입은 `TOOL_USE_ENFORCEMENT_MODELS` 튜플과 `_should_enforce_tool_use()`를 조정합니다.

**캐시 경계 조정하기.** `apply_anthropic_cache_control()`의 `4 - used`(=trailing 메시지 수)를 바꾸면 캐싱하는 최근 턴 수가 달라집니다. Anthropic은 최대 4개 브레이크포인트를 허용하므로, system을 포기하고 대화에 4개를 다 쓰거나, TTL을 `1h`로 올려(`_build_marker`) 긴 세션에서 캐시 수명을 늘릴 수 있습니다.

**프리픽스 안정성 검증하기.** 새 섹션을 stable에 넣었다면 `demo.py` 섹션 4처럼 서로 다른 시각으로 두 번 build해서 `stable tier identical == True`인지 꼭 확인하세요. False가 나오면 그 섹션은 volatile로 내려야 합니다.
