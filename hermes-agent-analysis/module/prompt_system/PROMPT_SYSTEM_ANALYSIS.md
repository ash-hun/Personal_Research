# Hermes Agent — Prompt System 정밀 분석

> 원본: `_reference/hermes-agent/` (read-only)
> 분석 대상: **시스템 프롬프트 조립(assembly) + Anthropic 캐시 경계(cache breakpoint) 삽입**
> 진입점: `agent/system_prompt.py:61` `build_system_prompt_parts()` → `agent/system_prompt.py:347` `build_system_prompt()`
> 캐시 경계 진입점: `agent/prompt_caching.py:49` `apply_anthropic_cache_control()`

---

## 1. 개요

Hermes Agent의 한 번의 LLM 요청은 거대한 `system` 메시지 하나와 그 뒤를 잇는 대화 히스토리로 구성된다. 이 `system` 메시지(정체성 + 도구 가이드 + 스킬 인덱스 + 환경 힌트 + 메모리 + 타임스탬프)는 수천 토큰에 달하는데, **멀티턴 대화에서 매 턴 다시 토큰화·청구되면 비용이 폭증한다.**

Prompt System은 이 비용을 두 개의 독립된 메커니즘으로 해결한다.

1. **세션당 1회 조립, 이후 verbatim 재사용** — 시스템 프롬프트는 세션 첫 턴에 한 번 만들어 `agent._cached_system_prompt`에 캐시하고, 이후 모든 턴은 그 문자열을 byte 단위로 그대로 다시 보낸다. **컨텍스트 압축(compression) 이벤트일 때만** 무효화 후 재조립한다 (`agent/system_prompt.py:347-363`, `agent/system_prompt.py:366-374`). byte 단위로 동일한 프리픽스를 유지해야 상위 프로바이더의 prefix KV-cache가 warm 상태로 유지된다.
2. **변하는 빈도로 3계층(tier) 분리 + 캐시 친화적 순서 배치** — 모든 섹션을 `stable → context → volatile` 세 묶음으로 나눠 "안정적인 것 먼저, 휘발성 맨 뒤" 순서로 join하고 (`agent/system_prompt.py:340-344`, `agent/system_prompt.py:363`), 그 위에 최대 4개의 Anthropic `cache_control` 마커를 찍는다 (`agent/prompt_caching.py:49-79`). 멀티턴 입력 토큰 비용이 약 75% 절감된다 (`agent/prompt_caching.py:1-6` docstring).

이 문서는 위 두 진입점을 **호출 경계(caller boundary)부터 반환까지** step별로 추적한다. 호출 경계는 `agent/conversation_loop.py`이며, 여기서 "캐시 복원 vs. 신규 빌드"를 결정하고(`:218`, `:582`), 최종 API 메시지에 캐시 마커를 주입한다(`:1019`).

---

## 2. 입출력 인터페이스

### 2-A. `build_system_prompt_parts(agent, system_message=None) -> Dict[str, str]`

| 구분 | 타입 / 구조 | 출처 |
|------|------------|------|
| **입력** `agent` | `AIAgent` (Any로 받음). 읽는 상태: `load_soul_identity`, `skip_context_files`, `valid_tool_names: set[str]`, `model`, `provider`, `platform`, `_tool_use_enforcement`, `_memory_store`, `_memory_enabled`, `_user_profile_enabled`, `_memory_manager`, `session_id`, `pass_session_id`, `_task_completion_guidance`, `_environment_probe`, `_kanban_worker_guidance` | `agent/system_prompt.py:61` |
| **입력** `system_message` | `Optional[str]` — 호출자(gateway/CLI)가 주입하는 추가 system 텍스트. context tier로 들어감 | `agent/system_prompt.py:287-288` |
| **출력** | `Dict[str, str]` = `{"stable": ..., "context": ..., "volatile": ...}` — 각 tier별 섹션을 `"\n\n"`로 join한 문자열 3개 | `agent/system_prompt.py:340-344` |

### 2-B. `build_system_prompt(agent, system_message=None) -> str`

- `build_system_prompt_parts()`를 호출해 받은 3계층을 다시 `"\n\n"`로 join한 **최종 system 문자열 하나**. 빈 tier는 스킵 (`agent/system_prompt.py:362-363`).

### 2-C. `apply_anthropic_cache_control(api_messages, cache_ttl="5m", native_anthropic=False) -> List[Dict]`

| 구분 | 타입 / 구조 | 출처 |
|------|------------|------|
| **입력** `api_messages` | `List[Dict[str, Any]]` — `[{role, content}, ...]`. `[0]`은 보통 `{"role": "system", "content": <문자열>}` | `agent/prompt_caching.py:50` |
| **입력** `cache_ttl` | `str` — `"5m"`(기본) 또는 `"1h"` | `agent/prompt_caching.py:51`, `agent/prompt_caching.py:41-46` |
| **입력** `native_anthropic` | `bool` — native Anthropic API면 `tool` role 메시지에도 마커 가능 | `agent/prompt_caching.py:52`, `:20-23` |
| **출력** | 입력의 **deep copy** + `cache_control` 마커 주입본. 원본 불변 | `agent/prompt_caching.py:60-62`, `:79` |
| **부수효과** | 없음 (pure function, deep copy로 입력 보호) | `agent/prompt_caching.py:8` |

---

## 3. 핵심 소스 파일 매핑

| 파일 | 역할 |
|------|------|
| `agent/system_prompt.py` | **진입점.** 3계층 조립(`build_system_prompt_parts`), join(`build_system_prompt`), 캐시 무효화(`invalidate_system_prompt`) |
| `agent/prompt_caching.py` | **캐시 경계.** `system_and_3` 전략으로 `cache_control` 마커 최대 4개 주입 |
| `agent/conversation_loop.py` | **호출 경계.** 캐시 복원 vs. 신규 빌드 결정(`_restore_or_build_system_prompt`), 최종 system 합성, 캐시 마커 적용 호출 |
| `agent/prompt_builder.py` | 가이드 상수(`DEFAULT_AGENT_IDENTITY` 등)와 섹션 생성 헬퍼(`build_skills_system_prompt`, `build_environment_hints`, `build_context_files_prompt`, `build_nous_subscription_prompt`) — **외부 서브시스템 경계** |
| `run_agent.py` | `AIAgent`에 thin forwarder (`_build_system_prompt_parts` → `build_system_prompt_parts`) 제공. `run_agent` namespace를 통한 lazy lookup으로 테스트 patch 계약 유지 (`agent/system_prompt.py:46-58`) |

---

## 4. step별 동작 흐름

전체 흐름은 **두 페이즈**다.
**페이즈 A (조립)** = step 0~5, `agent/system_prompt.py`.
**페이즈 B (캐시 경계)** = step 6~9, `agent/conversation_loop.py` + `agent/prompt_caching.py`.

---

### step 0 — 진입 & lazy import (테스트 patch 보존)

`build_system_prompt_parts()` 첫 줄에서 `_ra()`로 `run_agent` 모듈을 lazy 참조한다. `load_soul_md`, `build_skills_system_prompt`, `get_toolset_for_tool` 등을 **직접 import하지 않고** `run_agent` namespace를 통해 호출하는 이유는, 테스트가 `patch("run_agent.load_soul_md", ...)`로 갈아끼우는 계약을 지키기 위함이다.

```python
# agent/system_prompt.py:82
_r = _ra()
stable_parts: List[str] = []
```
출처: `agent/system_prompt.py:46-58`(`_ra` 정의), `:82`, `:85`

---

### step 1 — Stable tier 조립 (캐시 가능, 가장 큰 프리픽스)

세 tier 중 **에이전트 수명 내내 고정**되는 묶음. 아래 하위 step이 조건부로 `stable_parts`에 append된다. 핵심은 **모든 주입이 deterministic** — 같은 agent 상태면 같은 텍스트가 나와야 캐시가 깨지지 않는다.

#### step 1a — 정체성 (SOUL.md 또는 fallback)
`load_soul_identity`가 켜져 있거나 `skip_context_files`가 꺼져 있으면 `load_soul_md()`를 시도. 내용이 있으면 그걸 정체성으로, 없으면 하드코딩된 `DEFAULT_AGENT_IDENTITY`로 fallback. `_soul_loaded` 플래그는 나중에 step 2에서 SOUL 중복 로드를 막는 데 쓰인다.

```python
# agent/system_prompt.py:90-99
_soul_loaded = False
if agent.load_soul_identity or not agent.skip_context_files:
    _soul_content = _r.load_soul_md()
    if _soul_content:
        stable_parts.append(_soul_content); _soul_loaded = True
if not _soul_loaded:
    stable_parts.append(DEFAULT_AGENT_IDENTITY)
```
출처: `agent/system_prompt.py:90-99`, 상수 `agent/prompt_builder.py:121`

#### step 1b — Hermes 셀프-헬프 가이드 (무조건)
`HERMES_AGENT_HELP_GUIDANCE`를 항상 추가. 출처: `agent/system_prompt.py:102`, 상수 `agent/prompt_builder.py:131`

#### step 1c — 작업완수 가이드 (config 게이팅)
`_task_completion_guidance`(기본 True)이고 도구가 하나라도 있으면 `TASK_COMPLETION_GUIDANCE` 추가. 모델 패밀리 무관 — "stub 후 멈춤 / 막히면 출력 조작" 실패모드는 보편적이기 때문.

```python
# agent/system_prompt.py:110-111
if getattr(agent, "_task_completion_guidance", True) and agent.valid_tool_names:
    stable_parts.append(TASK_COMPLETION_GUIDANCE)
```
출처: `agent/system_prompt.py:110-111`, 상수 `agent/prompt_builder.py:286`

#### step 1d — 도구별 가이드 (도구 존재 게이팅)
**해당 도구 이름이 `valid_tool_names`에 있을 때만** 주입. `memory`→`MEMORY_GUIDANCE`, `session_search`→`SESSION_SEARCH_GUIDANCE`, `skill_manage`→`SKILLS_GUIDANCE`. kanban은 `_kanban_worker_guidance`(dispatcher가 spawn 시 세팅)가 있으면 그걸, 없고 `kanban_show` 도구가 있으면 `KANBAN_GUIDANCE` fallback. 모인 가이드들은 `" ".join`으로 한 블록에 합쳐 append.

```python
# agent/system_prompt.py:114-132
tool_guidance = []
if "memory" in agent.valid_tool_names: tool_guidance.append(MEMORY_GUIDANCE)
if "session_search" in agent.valid_tool_names: tool_guidance.append(SESSION_SEARCH_GUIDANCE)
if "skill_manage" in agent.valid_tool_names: tool_guidance.append(SKILLS_GUIDANCE)
...
if tool_guidance: stable_parts.append(" ".join(tool_guidance))
```
출처: `agent/system_prompt.py:114-132`

#### step 1e — Computer-use 가이드 (도구 게이팅, 별도 블록)
`computer_use` 도구가 있으면 multi-paragraph라 `tool_guidance`에 합치지 않고 독립 블록으로 append. 출처: `agent/system_prompt.py:136-138`

#### step 1f — Nous 구독 블록
`build_nous_subscription_prompt(valid_tool_names)` 결과가 있으면 append. 출처: `agent/system_prompt.py:140-142`

#### step 1g — Tool-use enforcement (모델 패밀리 게이팅) ⭐ 분기 핵심
도구가 있을 때, `_tool_use_enforcement` 값에 따라 4-way 분기로 주입 여부 `_inject` 결정:
- `True`/`"true"/"always"/...` → 무조건 inject
- `False`/`"false"/"never"/...` → inject 안 함
- `list` → 모델명에 substring 매칭되면 inject
- 그 외("auto" 등) → 하드코딩 `TOOL_USE_ENFORCEMENT_MODELS` 튜플과 substring 매칭

`_inject`면 `TOOL_USE_ENFORCEMENT_GUIDANCE` 추가 후, **모델별 추가 가이드** 분기:
- `gemini`/`gemma` → `GOOGLE_MODEL_OPERATIONAL_GUIDANCE`
- `gpt`/`codex`/`grok` → `OPENAI_MODEL_EXECUTION_GUIDANCE`

```python
# agent/system_prompt.py:150-177
if agent.valid_tool_names:
    _enforce = agent._tool_use_enforcement
    ... # 4-way로 _inject 결정
    if _inject:
        stable_parts.append(TOOL_USE_ENFORCEMENT_GUIDANCE)
        _model_lower = (agent.model or "").lower()
        if "gemini" in _model_lower or "gemma" in _model_lower:
            stable_parts.append(GOOGLE_MODEL_OPERATIONAL_GUIDANCE)
        if "gpt" in _model_lower or "codex" in _model_lower or "grok" in _model_lower:
            stable_parts.append(OPENAI_MODEL_EXECUTION_GUIDANCE)
```
출처: `agent/system_prompt.py:150-177`, 상수 `agent/prompt_builder.py:251`, `:268`

#### step 1h — 스킬 인덱스 (스킬 도구 게이팅)
`skills_list`/`skill_view`/`skill_manage` 중 하나라도 있으면, 각 도구의 toolset을 모아 `build_skills_system_prompt(...)` 호출 → 스킬 인덱스 문자열. [→ 외부 서브시스템 §6]

```python
# agent/system_prompt.py:179-195
has_skills_tools = any(name in agent.valid_tool_names for name in ['skills_list','skill_view','skill_manage'])
if has_skills_tools:
    avail_toolsets = {...}
    skills_prompt = _r.build_skills_system_prompt(available_tools=..., available_toolsets=...)
else:
    skills_prompt = ""
if skills_prompt: stable_parts.append(skills_prompt)
```
출처: `agent/system_prompt.py:179-195`, 헬퍼 `agent/prompt_builder.py:1040`

#### step 1i — Alibaba 모델명 워크어라운드 (provider 게이팅)
`provider == "alibaba"`면 API가 항상 "glm-4.7"을 반환하는 버그를 우회하기 위해 실제 모델 ID를 명시하는 문장 주입. 출처: `agent/system_prompt.py:202-209`

#### step 1j — 환경 힌트 + Python toolchain probe
`build_environment_hints()`(WSL/Termux 등) 결과가 있으면 append. 이어서 `_environment_probe`(기본 True)면 `get_environment_probe_line()`으로 비표준 python/pip/uv 상태를 한 줄 주입(깨끗하면 아무것도 안 넣음, 토큰 0). probe 실패는 **절대 빌드를 막지 않도록** try/except로 삼킨다.

```python
# agent/system_prompt.py:214-233
_env_hints = _r.build_environment_hints()
if _env_hints: stable_parts.append(_env_hints)
if getattr(agent, "_environment_probe", True):
    try:
        from tools.env_probe import get_environment_probe_line
        _probe_line = get_environment_probe_line()
        if _probe_line: stable_parts.append(_probe_line)
    except Exception: pass
```
출처: `agent/system_prompt.py:214-233`, 헬퍼 `agent/prompt_builder.py:767`

#### step 1k — Active profile 힌트
`_resolve_active_profile_name()`로 활성 프로필명 확인(실패 시 `"default"`). `default`면 일반 안내문, 아니면 해당 프로필 경로/cross-profile 쓰기 가드 안내문 주입. 프로필명은 세션 중 안 바뀌므로 캐시를 안 깬다.

```python
# agent/system_prompt.py:242-267
try:
    from agent.file_safety import _resolve_active_profile_name
    active_profile = _resolve_active_profile_name()
except Exception:
    active_profile = "default"
if active_profile == "default":
    stable_parts.append("Active Hermes profile: default. ...")
else:
    stable_parts.append(f"Active Hermes profile: {active_profile}. ...")
```
출처: `agent/system_prompt.py:242-267`

#### step 1l — 플랫폼 힌트
`platform_key`가 `PLATFORM_HINTS` dict에 있으면 그 값을, 없지만 키가 있으면 plugin registry(`platform_registry.get`)에서 `platform_hint` 조회 후 append. 출처: `agent/system_prompt.py:269-280`

---

### step 2 — Context tier 조립 (세션마다 다를 수 있음, 캐시 가능)

세션 단위로는 안정적이지만 cwd/호출자에 따라 달라지는 묶음.

- **2a**: `system_message`가 `None`이 아니면 그대로 추가. (단, `ephemeral_system_prompt`는 여기 **안** 들어감 — API 호출 시점에만 주입해 캐시/저장 프롬프트 밖에 둔다. `:285-286` 주석)
- **2b**: `skip_context_files`가 꺼져 있으면 `build_context_files_prompt(cwd=resolve_context_cwd(), skip_soul=_soul_loaded)` 호출 → cwd에서 발견한 프로젝트 파일(HERMES.md/AGENTS.md/CLAUDE.md/.cursorrules 중 하나). `_soul_loaded`(step 1a)면 SOUL 중복 로드 스킵. [→ 외부 서브시스템 §6]

```python
# agent/system_prompt.py:287-298
if system_message is not None:
    context_parts.append(system_message)
if not agent.skip_context_files:
    context_files_prompt = _r.build_context_files_prompt(
        cwd=resolve_context_cwd(), skip_soul=_soul_loaded)
    if context_files_prompt: context_parts.append(context_files_prompt)
```
출처: `agent/system_prompt.py:282-298`, 헬퍼 `agent/prompt_builder.py:1469`

---

### step 3 — Volatile tier 조립 (턴마다 변함, 절대 캐시 안 함)

휘발성 묶음 — 그래서 **반드시 맨 뒤**에 배치돼 앞의 캐시 프리픽스를 안 깬다.

- **3a**: 메모리 스냅샷 — `_memory_store`가 있고 `_memory_enabled`면 `format_for_system_prompt("memory")`, `_user_profile_enabled`면 `format_for_system_prompt("user")`(USER.md).
- **3b**: 외부 메모리 프로바이더 — `_memory_manager`가 있으면 `build_system_prompt()` 결과 추가(실패는 try/except로 삼킴).
- **3c**: ⭐ **날짜 단위 타임스탬프** — `hermes_time.now()`를 `'%A, %B %d, %Y'`(분/초 없이 **날짜만**)로 포맷. 분 단위면 매 rebuild마다 문자열이 달라져 prefix-cache가 무효화되므로 의도적으로 날짜 단위. 정확한 시각은 모델이 도구로 조회. (credit PR #20451) 이어서 `session_id`/`model`/`provider`를 줄바꿈으로 덧붙임.

```python
# agent/system_prompt.py:323-338
from hermes_time import now as _hermes_now
now = _hermes_now()
timestamp_line = f"Conversation started: {now.strftime('%A, %B %d, %Y')}"
if agent.pass_session_id and agent.session_id: timestamp_line += f"\nSession ID: {agent.session_id}"
if agent.model:    timestamp_line += f"\nModel: {agent.model}"
if agent.provider: timestamp_line += f"\nProvider: {agent.provider}"
volatile_parts.append(timestamp_line)
```
출처: `agent/system_prompt.py:300-338`

---

### step 4 — 3계층 반환 & join

각 tier 리스트를 `p.strip()` 후 빈 것 제거하고 `"\n\n"`로 join해 dict 반환(step 4-1). `build_system_prompt()`는 그 dict의 `stable → context → volatile` 순서를 다시 `"\n\n"`로 join해 **최종 문자열 하나** 생성(step 4-2). 이 "안정→세션안정→휘발성" 순서가 캐시 전략의 핵심.

```python
# agent/system_prompt.py:340-344
return {
    "stable":   "\n\n".join(p.strip() for p in stable_parts   if p and p.strip()),
    "context":  "\n\n".join(p.strip() for p in context_parts  if p and p.strip()),
    "volatile": "\n\n".join(p.strip() for p in volatile_parts if p and p.strip()),
}
# agent/system_prompt.py:362-363
parts = build_system_prompt_parts(agent, system_message=system_message)
return "\n\n".join(p for p in (parts["stable"], parts["context"], parts["volatile"]) if p)
```
출처: `agent/system_prompt.py:340-344`, `:362-363`

---

### step 5 — 세션 캐싱 / 무효화 (호출 경계: conversation_loop)

여기부터 호출 경계 `agent/conversation_loop.py`. 루프 진입 시 `_cached_system_prompt`가 `None`이면 `_restore_or_build_system_prompt()`를 호출(`:582-583`), 그 결과를 `active_system_prompt`로 고정(`:585`).

`_restore_or_build_system_prompt()`의 4-way 상태 분기 (`:218-317`):
- **continuing 세션이고 stored_prompt가 present** → DB의 프롬프트를 **verbatim 재사용**해 캐시 프리픽스 일치 유지 (`:267-271`). gateway는 턴마다 fresh `AIAgent`를 만들기에 이 DB roundtrip이 캐시 재사용의 핵심.
- **stored가 `null`/`empty`** → 경고 로그 후 재빌드 (silent cache miss를 가시화, `:273-284`).
- **first turn (missing)** → `agent._build_system_prompt(system_message)`로 신규 빌드(`:288`) → `on_session_start` hook(`:294`) → `update_system_prompt`로 DB 영속화(`:310`).

```python
# agent/conversation_loop.py:267-271
if stored_prompt:
    agent._cached_system_prompt = stored_prompt
    return
# agent/conversation_loop.py:288
agent._cached_system_prompt = agent._build_system_prompt(system_message)
```
무효화: 압축 이벤트 시 `invalidate_system_prompt()`가 `_cached_system_prompt=None` + 메모리 디스크 재로드 → 다음 턴 재빌드 (`agent/system_prompt.py:366-374`, `agent/conversation_compression.py:497-499`).
출처: `agent/conversation_loop.py:218-317`, `:582-585`

---

### step 6 — 최종 system 메시지 합성 (API 호출 시점)

캐시된 프롬프트에 `ephemeral_system_prompt`만 API 호출 시점에 덧붙여 `effective_system` 생성(이건 DB에 저장 안 됨 → 캐시 프리픽스 불변 유지). 이걸 `{"role":"system", ...}` **단일 content 문자열**로 메시지 맨 앞에 prepend. 단일 문자열이어야 byte-stable.

```python
# agent/conversation_loop.py:1000-1004
effective_system = active_system_prompt or ""
if agent.ephemeral_system_prompt:
    effective_system = (effective_system + "\n\n" + agent.ephemeral_system_prompt).strip()
if effective_system:
    api_messages = [{"role": "system", "content": effective_system}] + api_messages
```
출처: `agent/conversation_loop.py:985-1011`

---

### step 7 — 캐시 마커 적용 호출 (게이팅)

`_use_prompt_caching`가 켜져 있을 때만 `apply_anthropic_cache_control()` 호출. `cache_ttl`, `native_anthropic`는 agent 상태에서 전달.

```python
# agent/conversation_loop.py:1019-1024
if agent._use_prompt_caching:
    api_messages = apply_anthropic_cache_control(
        api_messages, cache_ttl=agent._cache_ttl,
        native_anthropic=agent._use_native_cache_layout)
```
출처: `agent/conversation_loop.py:1013-1024`

---

### step 8 — `system_and_3` 캐시 경계 삽입

`apply_anthropic_cache_control()` 내부 (`agent/prompt_caching.py:49-79`):
- **8a**: 입력을 `copy.deepcopy`로 복사(원본 불변). 비었으면 즉시 반환. `_build_marker(ttl)`로 마커 dict 생성 — `{"type":"ephemeral"}`, `ttl=="1h"`면 `"ttl":"1h"` 추가(5m은 키 생략).
- **8b**: `messages[0]`이 `system`이면 거기 마커 1개 → **stable+context 프리픽스 전체를 한 방에 캐싱**. `breakpoints_used += 1`.
- **8c**: 남은 개수 `remaining = 4 - used`. system이 아닌 메시지 인덱스 중 **마지막 `remaining`개**(`non_sys[-remaining:]`)에 마커 → 대화가 길어질 때 최근 턴을 rolling 캐싱.

```python
# agent/prompt_caching.py:62-78
messages = copy.deepcopy(api_messages)
if not messages: return messages
marker = _build_marker(cache_ttl)
breakpoints_used = 0
if messages[0].get("role") == "system":
    _apply_cache_marker(messages[0], marker, native_anthropic=native_anthropic)
    breakpoints_used += 1
remaining = 4 - breakpoints_used
non_sys = [i for i in range(len(messages)) if messages[i].get("role") != "system"]
for idx in non_sys[-remaining:]:
    _apply_cache_marker(messages[idx], marker, native_anthropic=native_anthropic)
```
출처: `agent/prompt_caching.py:49-79`, `_build_marker` `:41-46`

---

### step 9 — 단일 메시지 마커 부착 (포맷별 분기)

`_apply_cache_marker()`가 메시지 content 형태별로 다르게 붙인다 (`agent/prompt_caching.py:15-38`):
- `role=="tool"` → native Anthropic일 때만 메시지 레벨 `cache_control`, 아니면 무시(early return).
- content가 `None`/`""` → 메시지 레벨에 마커.
- content가 `str` → `[{"type":"text","text":content,"cache_control":marker}]`로 **승격**하며 마커 부착.
- content가 `list` → 마지막 블록(dict)에 마커 부착.

출처: `agent/prompt_caching.py:15-38`

---

## 5. 상태 전이 다이어그램

```
┌──────────────────────────── 페이즈 A: 조립 (system_prompt.py) ────────────────────────────┐
│                                                                                            │
│  build_system_prompt_parts(agent, system_message)                                          │
│      │ step 0: _ra() lazy import (patch 계약 보존)                                          │
│      ▼                                                                                      │
│  [STABLE tier]  step1a 정체성(SOUL│DEFAULT) → 1b help → 1c 작업완수(cfg) →                  │
│      │          1d 도구가이드(도구존재 게이팅) → 1e computer_use → 1f nous →                │
│      │          1g enforcement(4-way)─┬─gemini→GOOGLE                                        │
│      │                                └─gpt/codex/grok→OPENAI                                │
│      │          → 1h 스킬인덱스(스킬도구) → 1i alibaba(provider) →                          │
│      │          1j env+probe(try/except) → 1k profile → 1l platform           [CACHEABLE]  │
│      ▼                                                                                      │
│  [CONTEXT tier] step2a system_message → 2b context_files(cwd, skip_soul)      [CACHEABLE]  │
│      ▼                                                                                      │
│  [VOLATILE tier] step3a memory → 3b ext-memory(try/except) →                                │
│      │           3c 날짜단위 타임스탬프+model+provider             [DYNAMIC / 캐시 안 함]   │
│      ▼                                                                                      │
│  step4: 각 tier "\n\n".join → dict → build_system_prompt이 stable→context→volatile join     │
│                                                                                            │
└────────────────────────────────────────────────────────────────────────────────────────┘
                                    │  최종 system 문자열
                                    ▼
┌──────────────────── step5: 세션 캐싱 분기 (conversation_loop.py:582) ────────────────────┐
│  _cached_system_prompt is None?                                                            │
│      │ no → 그대로 active_system_prompt 사용 (재빌드 안 함 = 캐시 warm)                     │
│      │ yes → _restore_or_build_system_prompt():                                             │
│              ├─ stored "present" → DB 프롬프트 verbatim 재사용 ───────┐                     │
│              ├─ stored "null"/"empty" → 경고 후 재빌드               │                      │
│              └─ "missing"(첫턴) → 신규빌드 → on_session_start → DB저장 ┘                    │
│   (압축 이벤트 → invalidate_system_prompt → 다음 턴 재빌드)                                 │
└────────────────────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌──────────────── 페이즈 B: 캐시 경계 (conversation_loop.py + prompt_caching.py) ────────────┐
│  step6: effective_system = cached + ephemeral → [{system}] + api_messages                  │
│      ▼                                                                                      │
│  step7: _use_prompt_caching?  ── no ──▶ (마커 없이 전송)                                     │
│      │ yes                                                                                  │
│      ▼                                                                                      │
│  step8: apply_anthropic_cache_control                                                       │
│      ├─ deepcopy (원본 불변) → 비었으면 즉시 반환                                            │
│      ├─ marker = {"type":"ephemeral"(, "ttl":"1h")}                                         │
│      ├─ [0]==system → 마커 1개 (프리픽스 전체 캐싱), used=1                                  │
│      └─ non-system 마지막 (4-used)개 → rolling 캐싱                                          │
│      ▼                                                                                      │
│  step9: _apply_cache_marker (content: tool│None│str→승격│list→마지막블록)                    │
│      ▼                                                                                      │
│  cache_control 주입된 api_messages (deep copy) → API 전송                                   │
└────────────────────────────────────────────────────────────────────────────────────────┘
```

---

## 6. 외부 서브시스템 경계

진입점이 위임하는 지점들. (깊이 들어가지 않되 "여기서 무엇을 한다"를 명시)

| 경계 | 위임 대상 | 무엇을 하는가 | 위치 |
|------|----------|--------------|------|
| 정체성 로드 | `run_agent.load_soul_md()` | HERMES_HOME의 SOUL.md 읽어 정체성 텍스트 반환 | `agent/system_prompt.py:92` |
| 스킬 인덱스 | `build_skills_system_prompt()` | 2-layer 캐시(in-proc LRU + 디스크 스냅샷)로 스킬 디렉토리 스캔→컴팩트 인덱스 생성 | `agent/system_prompt.py:188` → `agent/prompt_builder.py:1040` |
| 환경 힌트 | `build_environment_hints()` | local/remote 백엔드별 OS·home·cwd 등 환경 블록 생성 | `agent/system_prompt.py:214` → `agent/prompt_builder.py:767` |
| Python probe | `tools.env_probe.get_environment_probe_line()` | 비표준 python/pip/uv/PEP-668 상태 한 줄 (clean이면 빈 문자열) | `agent/system_prompt.py:227-229` |
| 프로젝트 컨텍스트 파일 | `build_context_files_prompt()` | cwd에서 HERMES.md/AGENTS.md/CLAUDE.md/.cursorrules 우선순위로 1개 로드(각 20K자 캡) + SOUL.md | `agent/system_prompt.py:295` → `agent/prompt_builder.py:1469` |
| cwd 해석 | `resolve_context_cwd()` | TERMINAL_CWD(gateway) 또는 launch dir 결정 | `agent/system_prompt.py:296` → `agent/runtime_cwd.py` |
| Nous 구독 | `build_nous_subscription_prompt()` | 구독 기능 capability 블록 생성 | `agent/system_prompt.py:140` → `agent/prompt_builder.py:1274` |
| 메모리 스냅샷 | `agent._memory_store.format_for_system_prompt(...)` | memory/USER.md를 system prompt용 블록으로 포맷 | `agent/system_prompt.py:305,310` |
| 외부 메모리 | `agent._memory_manager.build_system_prompt()` | 외부 메모리 프로바이더 블록 (built-in에 additive) | `agent/system_prompt.py:317`, `agent/memory_manager.py:16` |
| 현재 시각 | `hermes_time.now()` | wall-clock (날짜 단위로만 포맷) | `agent/system_prompt.py:323-324` |
| 활성 프로필 | `agent.file_safety._resolve_active_profile_name()` | ~/.hermes/profiles/<name> 활성 프로필명 | `agent/system_prompt.py:243-244` |
| 플랫폼 힌트 | `gateway.platform_registry.platform_registry.get()` | plugin 등록 플랫폼별 LLM 가이드 | `agent/system_prompt.py:275-277` |
| 세션 영속화 | `agent._session_db.get_session / update_system_prompt` | system prompt를 SQLite에 read/write (gateway 캐시 재사용 핵심) | `agent/conversation_loop.py:249,310` |
| 세션 시작 hook | `hermes_cli.plugins.invoke_hook("on_session_start")` | 신규 세션 1회 플러그인 hook | `agent/conversation_loop.py:294-300` |
| 압축 무효화 | `agent/conversation_compression.py` | 압축 후 `_invalidate_system_prompt` → 재빌드 | `agent/conversation_compression.py:497-499` |
| forwarder | `run_agent.AIAgent._build_system_prompt_parts` | thin forwarder, patch 계약 보존 | `run_agent.py:2716-2719` |

> 참고: 서브디렉토리 힌트(`agent/subdirectory_hints.py`)는 system 프롬프트를 건드리지 않고 **tool result에** 붙인다 — 캐시 프리픽스를 깨지 않으려는 같은 철학.

---

## 7. 검증 매트릭스

문서 내 모든 라인 인용을 `grep -n`/`sed -n`/Read로 원본과 재대조한 결과.

| step / 주장 | 원본 위치 | 상태 |
|------------|----------|------|
| 진입점 `build_system_prompt_parts` | `system_prompt.py:61` | ✅ |
| `_ra()` lazy import / patch 계약 | `system_prompt.py:46-58`, `:82` | ✅ |
| step1a SOUL/DEFAULT 정체성 | `system_prompt.py:90-99` | ✅ |
| step1c 작업완수 가이드 게이팅 | `system_prompt.py:110-111` | ✅ |
| step1d 도구별 가이드 (도구 존재 게이팅) | `system_prompt.py:114-132` | ✅ |
| step1g enforcement 4-way + 모델별 분기 | `system_prompt.py:150-177` | ✅ |
| step1h 스킬 인덱스 게이팅 | `system_prompt.py:179-195` | ✅ |
| step1i alibaba provider 워크어라운드 | `system_prompt.py:202-209` | ✅ |
| step1j env hints + probe(try/except) | `system_prompt.py:214-233` | ✅ |
| step1k active profile | `system_prompt.py:242-267` | ✅ |
| step1l platform hints | `system_prompt.py:269-280` | ✅ |
| step2 context tier (system_message + context_files) | `system_prompt.py:282-298` | ✅ |
| step3c 날짜 단위 타임스탬프 (PR #20451) | `system_prompt.py:323-338` | ✅ |
| step4 3계층 join 반환 | `system_prompt.py:340-344`, `:362-363` | ✅ |
| `invalidate_system_prompt` | `system_prompt.py:366-374` | ✅ |
| 상수 `DEFAULT_AGENT_IDENTITY` | `prompt_builder.py:121` | ✅ |
| 상수 `HERMES_AGENT_HELP_GUIDANCE` | `prompt_builder.py:131` | ✅ |
| 상수 `MEMORY/SESSION_SEARCH/SKILLS_GUIDANCE` | `prompt_builder.py:137,160,166` | ✅ |
| 상수 `TOOL_USE_ENFORCEMENT_GUIDANCE/MODELS` | `prompt_builder.py:251,268` | ✅ |
| 상수 `TASK_COMPLETION_GUIDANCE` | `prompt_builder.py:286` | ✅ |
| `build_environment_hints` | `prompt_builder.py:767` | ✅ |
| `build_skills_system_prompt` (2-layer 캐시) | `prompt_builder.py:1040` | ✅ |
| `build_nous_subscription_prompt` | `prompt_builder.py:1274` | ✅ |
| `build_context_files_prompt` (우선순위 1개, 20K캡) | `prompt_builder.py:1469-1508` | ✅ |
| step5 캐시 복원/빌드 4-way | `conversation_loop.py:218-317` | ✅ |
| step5 루프 진입 게이팅 | `conversation_loop.py:582-585` | ✅ |
| step6 effective_system 합성 | `conversation_loop.py:1000-1004` | ✅ |
| step7 caching 게이팅 호출 | `conversation_loop.py:1019-1024` | ✅ |
| step8 `apply_anthropic_cache_control` system_and_3 | `prompt_caching.py:49-79` | ✅ |
| step8 `_build_marker` 5m/1h | `prompt_caching.py:41-46` | ✅ |
| step9 `_apply_cache_marker` 포맷 분기 | `prompt_caching.py:15-38` | ✅ |
| 압축 시 재빌드 | `conversation_compression.py:497-499` | ✅ |
| forwarder | `run_agent.py:2716-2719` | ✅ |
| `~75%` 절감 수치 | `prompt_caching.py:1-6` (docstring 근거) | ⚠️ 원본 docstring의 주장값(측정 코드는 아님) |
| 서브디렉토리 힌트는 tool result에 부착 | `subdirectory_hints.py` (파일 존재 확인, 미정독) | ⚠️ 철학적 참고 — 본 분석은 system_prompt 경로만 정독 |

---

### 분석 요약

Prompt System의 본질은 **"한 번 만들고, byte 단위로 그대로 다시 보낸다"** 는 단일 불변식(invariant)이다. 이를 위해 (1) 변하는 빈도로 섹션을 3계층 분리해 휘발성을 맨 뒤로 몰고, (2) 세션 DB에 프롬프트를 영속화해 gateway의 fresh-agent 경로에서도 verbatim 재사용하며, (3) 그 위에 Anthropic `cache_control` 마커를 "거대 프리픽스 1개 + 최근 대화 3개" = `system_and_3`로 찍는다. 타임스탬프를 날짜 단위로만 찍는 디테일(PR #20451)과 ephemeral/플러그인 컨텍스트를 system이 아닌 user 메시지에 주입하는 규율이 이 불변식을 지키는 결정적 장치다.
