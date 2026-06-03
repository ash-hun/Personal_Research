# memory_system — Hermes 영구 큐레이션 메모리: 동작 흐름 정밀 분석

> 이 문서 하나만 읽어도 Hermes의 **메모리 기능이 어떤 제어 흐름으로 동작하고, 입출력 인터페이스가 무엇인지** 알 수 있게 쓴 자기완결적 분석입니다.
> 원본은 `_reference/hermes-agent/` 아래에 있으며, **read-only로만** 분석했습니다(무수정).

---

## 1. 개요

Hermes의 메모리는 **세션을 넘어 살아남는, 글자 수 예산이 걸린 큐레이션 메모리(bounded curated memory)** 입니다. 보통의 LLM 에이전트는 세션이 끝나면 모든 걸 잊지만, Hermes는 두 개의 디스크 파일에 사실을 적어두고 다음 세션의 시스템 프롬프트에 다시 주입합니다.

핵심 설계 두 가지를 먼저 머리에 넣으면 나머지가 쉽게 풀립니다.

1. **이중 상태(dual state).** `MemoryStore`는 같은 데이터를 두 형태로 가집니다.
   - `_system_prompt_snapshot` — **로드 시점에 고정(frozen)** 된 스냅샷. 세션 내내 절대 안 바뀜 → 시스템 프롬프트가 byte-stable → prefix KV 캐시가 깨지지 않음.
   - `memory_entries` / `user_entries` — **라이브 상태**. 도구 호출로 변하고 디스크에 즉시 영속화됨. 도구 응답은 항상 이쪽을 반영.
   - 즉, "세션 중에 add 해도 시스템 프롬프트는 그대로, 파일과 도구 응답만 갱신" 됩니다. ([memory_tool.py:113-130](../../_reference/hermes-agent/tools/memory_tool.py#L113-L130))

2. **두 개의 타깃(target).**
   - `memory` → `MEMORY.md`: 에이전트 자신의 노트(환경 사실, 프로젝트 컨벤션, 교훈). 예산 2,200자.
   - `user` → `USER.md`: 사용자가 누구인가(이름, 역할, 선호, 커뮤니케이션 스타일). 예산 1,375자. ([memory_tool.py:124](../../_reference/hermes-agent/tools/memory_tool.py#L124))

진입점은 크게 **세 갈래**입니다.

| 진입점 | 원본 위치 | 트리거 |
|--------|-----------|--------|
| **쓰기** `memory_tool()` | [tools/memory_tool.py:602](../../_reference/hermes-agent/tools/memory_tool.py#L602) | 모델이 `memory` 도구 호출 |
| **시스템 프롬프트 주입** `format_for_system_prompt()` | [tools/memory_tool.py:443](../../_reference/hermes-agent/tools/memory_tool.py#L443) | 세션 시작, 프롬프트 빌드 |
| **턴별 회상(외부 provider)** `MemoryManager` | [agent/memory_manager.py:244](../../_reference/hermes-agent/agent/memory_manager.py#L244) | 매 사용자 턴 전/후 |

내장(built-in) 메모리는 파일 기반 + 글자 예산이고, 외부 메모리(Honcho, Hindsight, Mem0, holographic 등)는 `MemoryProvider` ABC를 구현하는 플러그인입니다. 둘은 공존하지만 **외부 provider는 동시에 딱 하나만** 허용됩니다.

---

## 2. 입출력 인터페이스

### (A) 쓰기 도구 — 모델이 호출하는 단일 도구

```python
# tools/memory_tool.py:602
memory_tool(
    action: str,              # "add" | "replace" | "remove"
    target: str = "memory",   # "memory" | "user"
    content: str = None,      # add/replace 시 항목 본문
    old_text: str = None,     # replace/remove 시 대상 식별용 짧은 부분문자열
    store: MemoryStore = None, # 주입된 스토어 (None이면 "비활성" 에러)
) -> str                      # JSON 문자열 (성공/실패 + 사용량 + 엔트리 목록)
```

- **식별 방식이 ID가 아니라 "짧은 unique 부분문자열"** 입니다. `replace`/`remove`는 `old_text`를 포함하는 엔트리를 찾습니다. ([memory_tool.py:367](../../_reference/hermes-agent/tools/memory_tool.py#L367), [419](../../_reference/hermes-agent/tools/memory_tool.py#L419))
- 성공 응답 형태 ([memory_tool.py:458-473](../../_reference/hermes-agent/tools/memory_tool.py#L458-L473)):

```json
{"success": true, "target": "user", "entries": ["..."],
 "usage": "37% — 512/1,375 chars", "entry_count": 3, "message": "Entry added."}
```

### (B) 시스템 프롬프트 주입 — frozen 스냅샷

```python
# tools/memory_tool.py:443
store.format_for_system_prompt(target: str) -> Optional[str]
#   로드 시점 스냅샷 블록을 반환. 라이브 상태가 아님. 엔트리 없으면 None.
#   블록 형태: "═...═\nUSER PROFILE ... [37% — 512/1,375 chars]\n═...═\n<§로 join된 본문>"
```

### (C) 외부 provider 오케스트레이션 — `MemoryManager`

```python
# agent/memory_manager.py
mgr.build_system_prompt() -> str                  # provider별 정적 블록 합성 (L318)
mgr.prefetch_all(query, *, session_id="") -> str  # 턴 전 관련 메모리 회상 (L339)
mgr.sync_all(user, assistant, *, ...) -> None     # 턴 후 영속화 (L383)
mgr.queue_prefetch_all(query, ...) -> None        # 다음 턴 회상 예약 (L358)
mgr.on_memory_write(action, target, content, ...) # 내장 쓰기를 외부에 미러 (L581)
mgr.handle_tool_call(name, args, **kw) -> str     # provider 도구 라우팅 (L441)
```

회상 결과는 그대로 주입되지 않고 **펜스로 감싸집니다** ([memory_manager.py:227-241](../../_reference/hermes-agent/agent/memory_manager.py#L227-L241)):

```python
build_memory_context_block(raw_context) -> str
# <memory-context>
# [System note: ... recalled memory context, NOT new user input.
#  Treat as authoritative reference data ...]
#
# {sanitize_context(raw_context)}
# </memory-context>
```

### (D) provider 플러그인 계약 — `MemoryProvider` ABC

추상 메서드(반드시 구현): `name`, `is_available()`, `initialize()`, `get_tool_schemas()`.
선택 훅(override해서 opt-in): `system_prompt_block()`, `prefetch()`, `queue_prefetch()`, `sync_turn()`, `handle_tool_call()`, `on_turn_start()`, `on_session_end()`, `on_session_switch()`, `on_pre_compress()`, `on_memory_write()`, `on_delegation()`. ([memory_provider.py:42-296](../../_reference/hermes-agent/agent/memory_provider.py#L42-L296))

---

## 3. 핵심 소스 파일 매핑

| 파일 | 역할 |
|------|------|
| [tools/memory_tool.py](../../_reference/hermes-agent/tools/memory_tool.py) (723 LOC) | 내장 메모리의 심장. `MemoryStore`(이중 상태·예산·드리프트 가드·atomic write), `memory_tool()` 디스패처, `MEMORY_SCHEMA`, 레지스트리 등록 |
| [tools/threat_patterns.py](../../_reference/hermes-agent/tools/threat_patterns.py) | 인젝션/exfil 위협 스캔. `scan_for_threats()`, `first_threat_message()` |
| [agent/memory_provider.py](../../_reference/hermes-agent/agent/memory_provider.py) (296) | 외부 provider ABC + 생명주기 계약 |
| [agent/memory_manager.py](../../_reference/hermes-agent/agent/memory_manager.py) (653) | provider 오케스트레이터. 1-외부-provider 강제, 펜싱, 스트리밍 스크러버 |
| [agent/system_prompt.py:305-321](../../_reference/hermes-agent/agent/system_prompt.py#L305-L321) | 세션 시작 시 스냅샷 블록을 프롬프트에 주입 |
| [agent/conversation_loop.py:762-952](../../_reference/hermes-agent/agent/conversation_loop.py#L762-L952) | 턴별 `on_turn_start` → `prefetch_all` → 펜스 주입 |
| [agent/tool_executor.py:721-752](../../_reference/hermes-agent/agent/tool_executor.py#L721-L752) | `memory` 도구 실행 + 외부 provider에 `on_memory_write` 브리지 |
| [run_agent.py:2555-2563](../../_reference/hermes-agent/run_agent.py#L2555-L2563) | 턴 후 `sync_all` + `queue_prefetch_all` |
| [plugins/memory/\<name\>/](../../_reference/hermes-agent/plugins/memory/) | 외부 provider 구현체 (holographic=SQLite+FTS5+HRR 등) |

---

## 4. step별 동작 흐름

메모리는 단일 함수가 아니라 **여러 시점에 걸친 협주**입니다. 시간 순서대로 4개 흐름으로 나눕니다: **(I) 세션 시작 로드 → (II) 시스템 프롬프트 주입 → (III) 턴 내 쓰기 & 회상 → (IV) 턴 후 동기화**.

---

### 흐름 I — 세션 시작: 디스크 로드 & frozen 스냅샷 캡처

진입점: `MemoryStore.load_from_disk()` ([memory_tool.py:132](../../_reference/hermes-agent/tools/memory_tool.py#L132))

**Step 0 — 디렉터리 보장 & 두 파일 읽기.**
```python
mem_dir = get_memory_dir(); mem_dir.mkdir(parents=True, exist_ok=True)   # L150-151
self.memory_entries = self._read_file(mem_dir / "MEMORY.md")             # L153
self.user_entries   = self._read_file(mem_dir / "USER.md")              # L154
```
`get_memory_dir()`는 `get_hermes_home() / "memories"`를 **동적으로** 반환합니다 — 프로파일 전환(HERMES_HOME 변경)을 항상 반영하기 위해 import 시점에 캐시하지 않습니다. ([memory_tool.py:55-57](../../_reference/hermes-agent/tools/memory_tool.py#L55-L57))

`_read_file()`은 파일을 `ENTRY_DELIMITER`(`"\n§\n"`)로 split합니다. 단순 `"§"` split이 아니라 구분자 전체로 split해야 본문에 §가 든 엔트리를 잘못 쪼개지 않습니다. ([memory_tool.py:510-513](../../_reference/hermes-agent/tools/memory_tool.py#L510-L513))

**Step 1 — 중복 제거(순서 보존).**
```python
self.memory_entries = list(dict.fromkeys(self.memory_entries))   # L157
self.user_entries   = list(dict.fromkeys(self.user_entries))     # L158
```

**Step 2 — 스냅샷용 위협 스캔(sanitize).** 여기가 핵심 보안 단계입니다. 각 엔트리를 `scan_for_threats(entry, scope="strict")`로 검사하고, **걸리면 스냅샷에서만 `[BLOCKED: ...]` placeholder로 치환**합니다. ([memory_tool.py:172-206](../../_reference/hermes-agent/tools/memory_tool.py#L172-L206))
- ⚠️ **라이브 상태는 원문 그대로 유지** → 사용자가 `memory(action=read)`로 오염 엔트리를 보고 직접 지울 수 있음. 조용히 드롭하면 공격을 숨기는 셈이라 일부러 안 지웁니다. ([memory_tool.py:160-164](../../_reference/hermes-agent/tools/memory_tool.py#L160-L164))
- 왜 로드 시점에 스캔하나? 메모리는 시스템 프롬프트에 **frozen으로 고정 주입**되므로, 오염 엔트리 하나가 세션 전체·세션 간에 인젝션을 일으킬 수 있기 때문입니다. ([memory_tool.py:69-72](../../_reference/hermes-agent/tools/memory_tool.py#L69-L72))

**Step 3 — frozen 스냅샷 확정.**
```python
self._system_prompt_snapshot = {
    "memory": self._render_block("memory", sanitized_memory),   # L168
    "user":   self._render_block("user",   sanitized_user),     # L169
}
```
`_render_block()`은 `═`×46 구분선 + 헤더(사용량 % 포함) + `§`로 join된 본문을 만듭니다. ([memory_tool.py:475-491](../../_reference/hermes-agent/tools/memory_tool.py#L475-L491)) 스캔은 디스크 바이트로부터 결정론적이므로 스냅샷은 세션 내내 안정적입니다(prefix-cache 불변식). ([memory_tool.py:146-148](../../_reference/hermes-agent/tools/memory_tool.py#L146-L148))

---

### 흐름 II — 시스템 프롬프트 주입

진입점: `build_system_prompt_parts()` ([system_prompt.py:305-321](../../_reference/hermes-agent/agent/system_prompt.py#L305-L321))

**Step 0 — 내장 스냅샷 블록 추가.** `MEMORY.md`와 `USER.md` 스냅샷을 `volatile_parts`에 넣습니다.
```python
mem_block = agent._memory_store.format_for_system_prompt("memory")   # L305
if mem_block: volatile_parts.append(mem_block)
...
user_block = agent._memory_store.format_for_system_prompt("user")    # L310
```
`format_for_system_prompt()`는 **라이브가 아니라 스냅샷**(`_system_prompt_snapshot[target]`)을 반환하고, 비면 `None`을 줘서 빈 블록이 안 들어가게 합니다. ([memory_tool.py:443-454](../../_reference/hermes-agent/tools/memory_tool.py#L443-L454))

**Step 1 — 외부 provider 정적 블록 추가(additive).**
```python
_ext_mem_block = agent._memory_manager.build_system_prompt()   # L317
if _ext_mem_block: volatile_parts.append(_ext_mem_block)
```
`build_system_prompt()`는 모든 provider의 `system_prompt_block()`을 모으되, 한 provider가 예외를 던져도 나머지를 막지 않습니다(try/except 격리). ([memory_manager.py:318-335](../../_reference/hermes-agent/agent/memory_manager.py#L318-L335))

> 정상 종료: `volatile` 문자열에 두 메모리 블록이 합쳐져 프롬프트로 들어감. 비정상(provider 예외): 해당 블록만 빠지고 흐름은 계속.

---

### 흐름 III — 턴 내부: 회상 주입 & 쓰기

#### III-A 회상(외부 provider prefetch) — 매 턴, 도구 루프 진입 전 1회

진입점: `_run_agent_turn` 내부 ([conversation_loop.py:762-779](../../_reference/hermes-agent/agent/conversation_loop.py#L762-L779))

**Step 0 — provider에 턴 시작 통지.** `prefetch_all()`보다 **먼저** 불러야 provider가 cadence(몇 번째 턴인지) 기준으로 회상/갱신을 게이팅할 수 있습니다.
```python
agent._memory_manager.on_turn_start(agent._user_turn_count, _turn_msg)   # L765
```

**Step 1 — 1회 prefetch, 결과 캐시.** 도구 호출마다 다시 부르면 10번 호출 = 10배 지연·비용이라, **턴당 한 번** 회상하고 캐시합니다. 쿼리는 skill 주입으로 오염되지 않은 `original_user_message`를 씁니다.
```python
_ext_prefetch_cache = agent._memory_manager.prefetch_all(_query) or ""   # L778
```
`prefetch_all()`은 provider별 `prefetch()`를 모아 합치고, 실패는 debug 로그만 남기고 무시(non-fatal)합니다. ([memory_manager.py:339-356](../../_reference/hermes-agent/agent/memory_manager.py#L339-L356))

**Step 2 — 펜스로 감싸 현재 턴 user 메시지에만 주입.** 도구 루프 안에서, **API에 보낼 복사본**에만 붙이고 `messages` 원본은 절대 변형하지 않습니다(세션 영속으로 새지 않음).
```python
if _ext_prefetch_cache:
    _fenced = build_memory_context_block(_ext_prefetch_cache)   # L952
    if _fenced: _injections.append(_fenced)
...
api_msg["content"] = _base + "\n\n" + "\n\n".join(_injections)   # L963-967
```
`build_memory_context_block()`은 먼저 `sanitize_context()`로 provider가 이미 펜스를 박아 보냈으면 떼어낸 뒤, `<memory-context>` + "이건 새 사용자 입력이 아니라 권위 있는 회상 데이터" system note로 감쌉니다. ([memory_manager.py:227-241](../../_reference/hermes-agent/agent/memory_manager.py#L227-L241))

> ⚠️ 스트리밍 출력에서 이 펜스가 청크 경계로 쪼개지면 UI로 새기 때문에, `StreamingContextScrubber`가 상태기계로 span을 걸러냅니다(open 태그를 한 델타에서 보고 close를 다음 델타에서 보는 경우까지 처리). ([memory_manager.py:62-224](../../_reference/hermes-agent/agent/memory_manager.py#L62-L224))

#### III-B 쓰기(내장 memory 도구 실행)

진입점: `tool_executor` 디스패치 ([tool_executor.py:721-752](../../_reference/hermes-agent/agent/tool_executor.py#L721-L752)) → `memory_tool()` ([memory_tool.py:602](../../_reference/hermes-agent/tools/memory_tool.py#L602))

**Step 0 — 디스패처 가드.** ([memory_tool.py:614-638](../../_reference/hermes-agent/tools/memory_tool.py#L614-L638))
- `store is None` → "Memory is not available" 에러 반환(early). (L614)
- `target ∉ {memory, user}` → 에러. (L617)
- `action`별로 필수 인자 검사 후 `store.add/replace/remove`로 분기. 알 수 없는 action → 에러. (L620-638)
- 결과 dict를 `json.dumps(..., ensure_ascii=False)` 문자열로 반환. (L640)

**Step 1 — add 경로** `MemoryStore.add()` ([memory_tool.py:297](../../_reference/hermes-agent/tools/memory_tool.py#L297)):
1. `content.strip()`; 비면 에러. (L299-301)
2. **위협 스캔** `_scan_memory_content()` → 걸리면 즉시 거부. (L304-306) — 쓰기 시점 1차 방어선.
3. **파일 락 획득** `with self._file_lock(...)`. 별도 `.lock` 파일을 써서 메모리 파일 자체는 atomic replace 가능. fcntl(Unix)/msvcrt(Win), 둘 다 없으면 no-op. (L308, [L208-243](../../_reference/hermes-agent/tools/memory_tool.py#L208-L243))
4. **락 안에서 디스크 재로딩** `_reload_target()` — 다른 세션의 쓰기를 흡수. **외부 드리프트** 감지 시 `.bak.<ts>` 백업 후 mutation 거부(아래 Step 1-a). (L313-315)
5. **정확한 중복** 이면 추가 없이 성공 응답. (L321-322)
6. **예산 검사**: `§`로 join한 새 총 글자수 > limit이면 거부(현재 사용량·초과량 안내). (L325-339)
7. 통과 시 append → `_set_entries` → `save_to_disk(target)`. (L341-343)
8. 락 해제 후 사용량 % 포함 성공 응답. (L345)

**Step 1-a — 외부 드리프트 가드** `_detect_external_drift()` ([memory_tool.py:515-568](../../_reference/hermes-agent/tools/memory_tool.py#L515-L568)). 두 신호로 "도구가 안 쓴 내용이 끼어들었는지" 판정:
- **round-trip 불일치**: 파싱→재직렬화 결과가 원본 바이트와 다름. (L555)
- **엔트리 크기 초과**: 단일 엔트리가 스토어 전체 char_limit보다 큼 → patch 도구/shell append/수동 편집이 자유 텍스트를 한 엔트리로 욱여넣은 것. (L552-555)
- 드리프트면 `.bak.<ts>`로 스냅샷 저장 후 경로 반환 → 호출자가 mutation 거부(데이터 무손실 보장, issue #26045). (L562-568, `_drift_error` [L83-110](../../_reference/hermes-agent/tools/memory_tool.py#L83-L110))

**Step 2 — replace 경로** ([memory_tool.py:347](../../_reference/hermes-agent/tools/memory_tool.py#L347)):
- `old_text`/`new_content` 검증 + 새 content 위협 스캔. (L349-359)
- 락→재로딩→드리프트 검사. (L361-364)
- `old_text`를 **부분문자열로 포함**하는 엔트리들 수집. 0개 → 에러; 2개 이상이고 서로 다르면 → "더 구체적으로" + 미리보기 반환; 전부 동일하면 첫 번째만 처리. (L367-382)
- 예산 검사 후 `entries[idx] = new_content` → 저장. (L385-405)

**Step 3 — remove 경로** ([memory_tool.py:407](../../_reference/hermes-agent/tools/memory_tool.py#L407)): replace와 동일한 매칭 규칙(부분문자열, 다중매칭 가드)으로 `entries.pop(idx)` → 저장. (L418-441)

**Step 4 — 외부 provider 브리지.** 내장 쓰기가 `add`/`replace`면, 외부 provider에 `on_memory_write`로 미러링합니다(외부 백엔드 동기화용). ([tool_executor.py:737-749](../../_reference/hermes-agent/agent/tool_executor.py#L737-L749))
```python
if agent._memory_manager and function_args.get("action") in {"add", "replace"}:
    agent._memory_manager.on_memory_write(action, target, content, metadata=...)
```
`on_memory_write()`는 **builtin provider는 건너뛰고**(자기가 쓰기의 출처) 외부 provider에만 전달하며, provider 시그니처를 inspect해 metadata 전달 방식(keyword/positional/legacy)을 맞춥니다. ([memory_manager.py:581-609](../../_reference/hermes-agent/agent/memory_manager.py#L581-L609))

**Step 5 — atomic write** `_write_file()` ([memory_tool.py:570-599](../../_reference/hermes-agent/tools/memory_tool.py#L570-L599)): 같은 디렉터리에 `tempfile.mkstemp` → write+flush+`fsync` → `atomic_replace(tmp, path)`. "w"+flock 방식은 락 획득 전에 truncate되어 동시 reader가 빈 파일을 보는 레이스가 있어서, atomic rename으로 교체. reader는 항상 옛 완전 파일 또는 새 완전 파일만 봅니다(그래서 `_read_file`은 락 불필요).

---

### 흐름 IV — 턴 후: 외부 provider 동기화 & 다음 턴 예약

진입점: `run_agent.py` 턴 종료 훅 ([run_agent.py:2547-2565](../../_reference/hermes-agent/run_agent.py#L2547-L2565))

**Step 0 — early return 가드.** `interrupted`거나, manager/응답/입력 중 하나라도 없으면 그냥 반환(메모리 백엔드가 사용자 응답 표시를 막지 않게). (L2547-2550)

**Step 1 — 완료된 턴 동기화.** `sync_all(user, assistant, session_id=..., messages=...)` — provider별 `sync_turn`이 `messages` 인자를 받는지 inspect로 판별해 호출. ([memory_manager.py:383-411](../../_reference/hermes-agent/agent/memory_manager.py#L383-L411), 호출 [run_agent.py:2555](../../_reference/hermes-agent/run_agent.py#L2555))

**Step 2 — 다음 턴 회상 예약.** `queue_prefetch_all(user_msg, session_id=...)` — 백그라운드 회상을 미리 돌려 다음 턴 `prefetch_all`이 캐시된 결과를 즉시 쓰게 함. ([memory_manager.py:358-367](../../_reference/hermes-agent/agent/memory_manager.py#L358-L367), 호출 [run_agent.py:2560](../../_reference/hermes-agent/run_agent.py#L2560))

> 참고: **내장 메모리는 III-B에서 도구 호출 즉시 디스크에 영속화**되므로 흐름 IV의 sync가 필요 없습니다. IV는 **외부 provider 전용** 동기화입니다. 내장은 "모델이 능동적으로 memory 도구 호출", 외부는 "턴 끝에 자동 sync"라는 두 갈래입니다.

---

## 5. 상태 전이 다이어그램

```
[세션 시작]
   load_from_disk()
      ├─ _read_file(MEMORY.md/USER.md) ─ split("\n§\n") ─ dedup
      ├─ sanitize_entries_for_snapshot()   ← scan_for_threats(strict)
      │     hit ─▶ 스냅샷만 [BLOCKED:...] 치환 (라이브는 원문 유지)
      └─ _system_prompt_snapshot 확정 (frozen, 세션 내내 불변)
                         │
                         ▼
[시스템 프롬프트 빌드]  build_system_prompt_parts()
      ├─ format_for_system_prompt("memory"|"user")  → 스냅샷 블록
      └─ memory_manager.build_system_prompt()        → 외부 provider 정적 블록
                         │
                         ▼
┌─────────────────────── 매 사용자 턴 ───────────────────────┐
│ on_turn_start(turn_count)                                  │
│        │                                                   │
│ prefetch_all(query) ─▶ _ext_prefetch_cache (턴당 1회)      │
│        │                                                   │
│ ┌── 도구 루프 (api_call_count < max_iterations) ──┐        │
│ │  api_msg = user_msg + build_memory_context_block│        │
│ │            (<memory-context> 펜스, 복사본에만)   │        │
│ │                                                  │        │
│ │  모델이 memory 도구 호출?                        │        │
│ │    ├─ add ──▶ strip→scan→LOCK→reload(drift?)     │        │
│ │    │          →dup?→budget?→append→atomic write  │        │
│ │    │          → on_memory_write(외부 미러)        │        │
│ │    ├─ replace ─▶ 부분문자열 매칭(다중?)→budget→저장│       │
│ │    └─ remove ──▶ 부분문자열 매칭(다중?)→pop→저장  │        │
│ │       drift 감지 ─▶ .bak.<ts> 백업 + mutation 거부│       │
│ └──────────────────────────────────────────────────┘       │
│        │ (턴 종료)                                          │
│ sync_all(user, assistant) ─▶ 외부 provider sync_turn       │
│ queue_prefetch_all(user)  ─▶ 다음 턴 회상 예약             │
└────────────────────────────────────────────────────────────┘
                         │
                         ▼
[세션 종료]  on_session_end(messages) ─▶ provider 말미 추출

* 내장 메모리: 도구 호출 즉시 디스크 영속 (sync_all 불필요)
* 외부 메모리: 턴 후 sync_all / queue_prefetch_all 로 비동기 동기화
```

---

## 6. 외부 서브시스템 경계

진입점이 위임하지만 이 분석의 깊이 밖인 지점들(잘라내지 않고 명시):

| 경계 | 위치 | 무엇을 하나 |
|------|------|-------------|
| 위협 패턴 스캔 | [tools/threat_patterns.py:187](../../_reference/hermes-agent/tools/threat_patterns.py#L187) `scan_for_threats`, [:227](../../_reference/hermes-agent/tools/threat_patterns.py#L227) `first_threat_message` | 인젝션/exfil/invisible-unicode 패턴 매칭. memory는 `scope="strict"`(최광 패턴) 사용 |
| atomic 파일 교체 | [utils.py](../../_reference/hermes-agent/utils.py) `atomic_replace` | temp→target 원자적 rename(crash-safe) |
| 프로파일 홈 해석 | [hermes_constants.py](../../_reference/hermes-agent/hermes_constants.py) `get_hermes_home` | HERMES_HOME 동적 해석 → `memories/` 경로 |
| 도구 레지스트리 | [tools/registry.py](../../_reference/hermes-agent/tools/registry.py) `registry.register`, `tool_error` | `memory` 도구를 toolset에 등록 ([memory_tool.py:707-719](../../_reference/hermes-agent/tools/memory_tool.py#L707-L719)) |
| 외부 provider 구현체 | [plugins/memory/\<name\>/](../../_reference/hermes-agent/plugins/memory/) | honcho/hindsight/mem0/holographic(SQLite+FTS5+HRR)/byterover/supermemory/openviking/retaindb. `MemoryProvider` ABC 구현 |
| provider 설정 마법사 | [hermes_cli/memory_setup.py](../../_reference/hermes-agent/hermes_cli/memory_setup.py) | `get_config_schema()` 기반 대화형 설정 |
| 압축 전 추출 | [agent/conversation_compression.py:431](../../_reference/hermes-agent/agent/conversation_compression.py#L431) `on_pre_compress` | 컨텍스트 압축으로 버려질 메시지에서 인사이트 추출 |
| 컨텍스트 펜스 주입 | [agent/conversation_loop.py:952](../../_reference/hermes-agent/agent/conversation_loop.py#L952) | prefetch 결과를 API 메시지에 펜싱해 주입 |

---

## 7. 검증 매트릭스

3·4절에서 인용한 라인을 `grep -n`/Read로 원본과 재확인한 결과입니다.

| 분석 항목 | 원본 위치 | 상태 |
|-----------|-----------|------|
| `MemoryStore` 이중 상태 정의 | memory_tool.py:113-130 | ✅ |
| 글자 예산 기본값 2200/1375 | memory_tool.py:124 | ✅ |
| `get_memory_dir` 동적 해석 | memory_tool.py:55-57 | ✅ |
| `ENTRY_DELIMITER = "\n§\n"` | memory_tool.py:59 | ✅ |
| `load_from_disk` 로드→dedup→sanitize→snapshot | memory_tool.py:132-170 | ✅ |
| sanitize: 스냅샷만 BLOCKED, 라이브는 원문 | memory_tool.py:172-206 | ✅ |
| `_file_lock` fcntl/msvcrt/no-op | memory_tool.py:208-243 | ✅ |
| `add` strip→scan→lock→reload→dup→budget→save | memory_tool.py:297-345 | ✅ |
| `_scan_memory_content`(쓰기 시점 방어) | memory_tool.py:78-80, 304 | ✅ |
| `replace` 부분문자열 매칭 + 다중 가드 | memory_tool.py:347-405 | ✅ |
| `remove` pop | memory_tool.py:407-441 | ✅ |
| `_detect_external_drift` 2신호 + .bak | memory_tool.py:515-568 | ✅ |
| `_drift_error` 메시지 | memory_tool.py:83-110 | ✅ |
| `format_for_system_prompt` 스냅샷 반환 | memory_tool.py:443-454 | ✅ |
| `_write_file` mkstemp+fsync+atomic_replace | memory_tool.py:570-599 | ✅ |
| `memory_tool` 디스패처 가드/분기 | memory_tool.py:602-640 | ✅ |
| `MEMORY_SCHEMA`(action/target/content/old_text) | memory_tool.py:652-701 | ✅ |
| 레지스트리 등록 | memory_tool.py:707-719 | ✅ |
| `build_memory_context_block` 펜스 | memory_manager.py:227-241 | ✅ |
| `StreamingContextScrubber` 상태기계 | memory_manager.py:62-224 | ✅ |
| `add_provider` 1-외부-provider 강제 | memory_manager.py:258-302 | ✅ |
| `build_system_prompt` 격리 합성 | memory_manager.py:318-335 | ✅ |
| `prefetch_all` non-fatal 합성 | memory_manager.py:339-356 | ✅ |
| `sync_all` messages inspect 분기 | memory_manager.py:383-411 | ✅ |
| `on_memory_write` builtin skip + metadata mode | memory_manager.py:581-609 | ✅ |
| `MemoryProvider` ABC 추상/선택 메서드 | memory_provider.py:42-296 | ✅ |
| 시스템 프롬프트 주입(내장+외부) | system_prompt.py:305-321 | ✅ |
| 턴별 on_turn_start→prefetch_all | conversation_loop.py:762-779 | ✅ |
| 펜스 주입(복사본만, 원본 불변) | conversation_loop.py:952-967 | ✅ |
| 도구 실행 + on_memory_write 브리지 | tool_executor.py:721-752 | ✅ |
| 턴 후 sync_all + queue_prefetch_all | run_agent.py:2547-2565 | ✅ |
| 압축 전 on_pre_compress | conversation_compression.py:431 | ✅ |
| threat 시그니처 | threat_patterns.py:187, 227 | ✅ |

> ⚠️ 표기가 필요한 단순화 한 곳: 4절 흐름 IV에서 "내장은 sync 불필요"라고 정리했는데, 이는 내장 메모리가 III-B의 도구 호출 시점([memory_tool.py:343](../../_reference/hermes-agent/tools/memory_tool.py#L343))에 이미 `save_to_disk`로 영속화되기 때문입니다. `sync_all`/`queue_prefetch_all`([run_agent.py:2555-2560](../../_reference/hermes-agent/run_agent.py#L2555-L2560))은 builtin이 아닌 외부 provider만 의미 있게 처리하며, 코드 자체는 모든 provider를 순회하되 builtin은 해당 훅을 no-op으로 둡니다.

---

## 8. 한 줄 요약

Hermes 메모리는 **"로드 시 frozen 스냅샷(캐시 안정) + 라이브 디스크 상태(즉시 영속) + 글자 예산(고밀도 강제) + 이중 위협 스캔(로드·쓰기) + 외부 드리프트 가드(무손실)"** 를 조합해, *세션을 넘는 안전한 큐레이션 메모리*를 단일 `memory` 도구와 plug-in `MemoryProvider`로 동시에 제공합니다.
