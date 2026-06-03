# Subagent Delegation — Hermes Agent 동작 흐름 분석

> 원본: `_reference/hermes-agent` (Nous Research, Hermes Agent) · **read-only로 분석**
> 진입점: `tools/delegate_tool.py:delegate_task` (1918), 보조 기능: `tools/mixture_of_agents_tool.py:mixture_of_agents_tool` (236)
> 이 문서는 원본을 보지 않고도 위임 메커니즘의 제어 흐름과 입출력 인터페이스를 이해할 수 있도록 자기완결적으로 작성되었다.

---

## 1. 개요

Hermes Agent의 **서브에이전트 위임(subagent delegation)** 은 부모 에이전트(`AIAgent`)가 큰 작업을 잘게 쪼개,
각 조각을 **부모의 대화 기록을 전혀 모르는 깨끗한 자식 `AIAgent`** 에게 맡기고, (필요하면 병렬로) 실행한 뒤
자식의 **최종 답변만** 구조화된 tool result로 회수하는 메커니즘이다. LLM이 `delegate_task` 도구를 호출하면
시작되며, 단일(`goal`) 모드와 배치(`tasks=[...]`) 모드를 지원한다.

핵심 설계 원리는 네 가지다.

1. **격리(Isolation)** — 자식은 부모 history를 볼 수 없다. 부모는 자식 생성 시 `ephemeral_system_prompt`로
   목표만 주입하고 `skip_context_files=True` / `skip_memory=True` / 새 iteration 예산을 부여한다.
   (`delegate_tool.py:1121,1124,1125,1136`)
2. **스폰 + 권한 축소(Spawn)** — 자식 toolset은 항상 부모 toolset과의 **교집합**이며, 위임/메모리 등
   위험 toolset은 제거된다. 자식은 부모에게 없는 도구를 절대 얻지 못한다. (`delegate_tool.py:945-968`)
3. **수집(Collect)** — 자식 실행 결과 dict에서 `final_response`를 `summary`로 뽑아 `SubagentResult`로 환원한다.
   (`delegate_tool.py:1620-1718`)
4. **병렬 fan-out + 깊이 가드** — 배치는 `ThreadPoolExecutor`로 병렬 실행하고, 재위임은 `max_spawn_depth`로 묶는다.
   (`delegate_tool.py:2101-2165`, `:1960-1972`)

이와 별개로 같은 파일군의 **MoA(Mixture-of-Agents)** 도구는 "위임을 부채꼴로 펼친 뒤 한 번 더 합성하는" 변형 패턴이다
(7장 참조).

---

## 2. 입출력 인터페이스

### 2.1 진입점 시그니처

```python
# tools/delegate_tool.py:1918
def delegate_task(
    goal: Optional[str] = None,            # 단일 모드: 자식 1명의 목표
    context: Optional[str] = None,         # 인계용 배경 텍스트
    toolsets: Optional[List[str]] = None,  # 자식 toolset 요청 (부모와 교집합)
    tasks: Optional[List[Dict]] = None,    # 배치 모드: [{goal, context, toolsets, role}, ...]
    max_iterations: Optional[int] = None,  # 무시됨 — config 값이 권위 (1982-1988)
    acp_command / acp_args = None,         # ACP transport override
    role: Optional[str] = None,            # 'leaf'(기본) | 'orchestrator'
    parent_agent=None,                     # 필수 — 부모 AIAgent 컨텍스트
) -> str                                   # JSON 문자열
```

### 2.2 입력 모드 (둘 중 하나, 배타적)

| 모드 | 조건 | 결과 |
| --- | --- | --- |
| 단일 | `goal`이 비어있지 않은 str | `task_list = [{goal, context, toolsets, role}]` (2018-2021) |
| 배치 | `tasks`가 list | 각 원소가 한 자식. `len > max_concurrent_children`이면 거부 (2008-2016) |
| 오류 | 둘 다 없음 | `tool_error("Provide either 'goal' ... or 'tasks' ...")` (2022-2023) |

### 2.3 출력: `{"results": [...], "total_duration_seconds": float}` (JSON, 2303-2309)

`results` 배열의 각 원소(자식 1명) = **SubagentResult**. 입력 순서로 정렬됨(`task_index` 기준, 2212-2213).

| 필드 | 타입 | 의미 | 출처 |
| --- | --- | --- | --- |
| `task_index` | int | 배치 내 위치 (입력 순서 복원용) | 1685 |
| `status` | str | `completed` / `failed` / `interrupted` / `timeout` / `error` | 1625-1633 |
| `summary` | str\|None | 자식의 `final_response` — **부모에게 돌아가는 답** | 1620,1687 |
| `exit_reason` | str | `completed` / `max_iterations` / `interrupted` / `timeout` / `error` | 1671-1677 |
| `api_calls` | int | 자식이 수행한 LLM 호출 수 | 1688 |
| `duration_seconds` | float | 자식 실행 시간 | 1689 |
| `model` | str\|None | 자식이 쓴 모델 | 1690 |
| `tokens` | dict | `{input, output}` | 1692-1699 |
| `tool_trace` | list | 자식의 도구 호출 요약 (tool명/바이트/ok·error) | 1637-1669 |
| `error` | str | 실패/타임아웃 사유 (해당 시) | 1719-1720, 1595-1605 |

> 내부 전용 키 `_child_role`, `_child_cost_usd`는 부모 스레드에서 hook 호출·비용 합산에 쓰인 뒤
> 모델에 직렬화되기 전에 `pop`된다 (`delegate_task` 2258-2259).

---

## 3. 핵심 소스 파일 매핑

| 파일 | 역할 |
| --- | --- |
| `tools/delegate_tool.py` | 위임의 전부 — 진입점, 자식 빌드, 실행/수집, 병렬/타임아웃/깊이 가드 |
| `tools/mixture_of_agents_tool.py` | MoA(2-레이어 합성) 도구 — 위임의 변형 패턴 |
| `run_agent.py` | **외부 경계.** `AIAgent` 클래스(자식 인스턴스), `_dispatch_delegate_task` 호출 지점, `run_conversation` 실행 루프 |

### `delegate_tool.py` 내부 핵심 심볼

| 심볼 | 라인 | 역할 |
| --- | --- | --- |
| `delegate_task` | 1918 | 진입점. 정규화 → 빌드 → 실행 → 수집 → JSON |
| `_build_child_agent` | 870 | 자식 `AIAgent` 생성 (격리·toolset 교집합·role 강등) |
| `_build_child_system_prompt` | 569 | 자식 시스템 프롬프트 (goal+context만 = 격리 경계) |
| `_strip_blocked_tools` | 672 | 금지 toolset 제거 (`delegation/clarify/memory/code_execution`) |
| `_run_single_child` | 1321 | 자식 1명 실행 + 타임아웃 래핑 + 결과 distill + 정리 |
| `_normalize_role` | 312 | role 정규화 (`leaf`/`orchestrator`, 그 외 → leaf) |
| `_get_max_spawn_depth` | 394 | 깊이 한도 (config, [1,3] clamp) |
| `_get_max_concurrent_children` | 329 | 병렬 폭 (config, 기본 3) |
| `_get_child_timeout` | 367 | 자식 하드 타임아웃 (config, 기본 600s) |

### 기본 상수 (`delegate_tool.py`)

```python
_DEFAULT_MAX_CONCURRENT_CHILDREN = 3   # :132
MAX_DEPTH = 1                          # :133  ← 기본은 "평면" (부모0→자식1, 손자 거부)
_MIN_SPAWN_DEPTH, _MAX_SPAWN_DEPTH_CAP = 1, 3   # :136-137
DEFAULT_MAX_ITERATIONS = 50            # :512
DEFAULT_CHILD_TIMEOUT  = 600           # :513 (초)
```

---

## 4. step별 동작 흐름 — `delegate_task`

### step 0 — 진입 가드 (1943-1972)

- **0a. 부모 컨텍스트 필수**: `parent_agent is None` → `tool_error(...)` 반환. (1943-1944)
- **0b. 스폰 일시정지 킬스위치**: `is_spawn_paused()` 참이면 거부. TUI/`delegation.pause` RPC로 폭주 트리를
  멈출 수 있게 한다. (1949-1953)
- **0c. role 정규화**: `top_role = _normalize_role(role)` → `leaf` 또는 `orchestrator`. (1956)
- **0d. 깊이 가드(비정상 종료)**: `depth = parent._delegate_depth`(없으면 0). `depth >= max_spawn_depth`면
  `{"error": "Delegation depth limit reached ..."}` JSON 반환. **재위임 트리의 무한 증식을 막는 핵심.** (1960-1972)

### step 1 — 설정·자격증명·작업목록 준비 (1974-2026)

- **1a. config 로드**: `effective_max_iter = config.max_iterations`(기본 50). 모델이 넘긴 `max_iterations`는
  **무시**되고 로그만 남긴다(예측 가능한 예산 보장). (1974-1988)
- **1b. 위임 자격증명 해석**: `_resolve_delegation_credentials(cfg, parent)` → provider/model/base_url/api_key 등.
  `ValueError`면 `tool_error`. (1995-1998)
- **1c. tasks JSON 복구**: 모델이 `tasks`를 문자열로 보냈을 때 `_recover_tasks_from_json_string`으로 복원. (2002-2006)
- **1d. 작업목록 정규화**: 배치면 크기 검증 후 `task_list = tasks` (2008-2017), 단일이면 한 원소 리스트 구성 (2018-2021),
  둘 다 없으면 오류 (2022-2023).
- **1e. 각 task 검증**: dict 여부 + `goal` 존재 확인. 누락 시 오류. (2029-2035)

### step 2 — 모든 자식 에이전트를 메인 스레드에서 빌드 (2044-2089)

> 스레드 안전을 위해 **실행 전에** 자식들을 전부 만든다.

- **2a. 부모 tool 이름 백업**: 자식 생성이 `model_tools._last_resolved_tool_names` 전역을 덮어쓰므로
  먼저 저장. (2044-2049)
- **2b. 자식 빌드 루프**: 각 task에 대해 `effective_role = _normalize_role(task.role or top_role)`(per-task가 우선,
  2060) → `_build_child_agent(...)` 호출(2061-2083). 결과를 `(i, t, child)`로 모은다.
- **2c. finally 전역 복원**: 빌드 중 예외가 나도 `_last_resolved_tool_names`를 부모 값으로 되돌린다. (2087-2089)

#### step 2′ — `_build_child_agent` 내부 (870)

| 하위 step | 내용 | 라인 |
| --- | --- | --- |
| role 강등 | `child_depth = parent.depth+1`. `orchestrator_ok = kill_switch_on AND child_depth < max_spawn`. 둘 중 하나라도 불충족이면 `effective_role = "leaf"`. **role이 leaf로 강등되는 유일한 지점.** | 904-913 |
| 정체성 | `subagent_id = f"sa-{i}-{uuid8}"` — progress·registry·event가 공유하는 키 | 920 |
| 부모 toolset 도출 | `enabled_toolsets` 또는 로드된 tool 이름에서 역산, 없으면 `DEFAULT_TOOLSETS` | 930-943 |
| **toolset 교집합** | `toolsets` 지정 시 `[t for t in toolsets if t in expanded_parent]` → 부모에 없는 도구 차단. MCP toolset 보존 후 `_strip_blocked_tools`. 미지정 시 부모 상속 후 strip. | 945-961 |
| orchestrator 재부여 | `effective_role == "orchestrator"`면 strip된 `delegation` toolset을 다시 append (권한은 role로 부여) | 967-968 |
| 자식 프롬프트 | `_build_child_system_prompt(goal, context, ...)` — **부모 history 없음** | 970-978 |
| **자식 AIAgent 생성** | `ephemeral_system_prompt=child_prompt`, `skip_context_files=True`, `skip_memory=True`, `enabled_toolsets=child_toolsets`, `iteration_budget=None`(새 예산), `quiet_mode=True` | 1106-1137 |
| 메타 stash | `child._delegate_depth/_delegate_role/_subagent_id/_subagent_goal` 저장 | 1140-1148 |
| 등록·announce | 부모 `_active_children`에 등록(인터럽트 전파용) + `subagent.spawn_requested` 이벤트 | 1157-1172 |

### step 3 — 실행: 단일 vs 배치 분기 (2091-2213)

- **3a. 단일(`n_tasks == 1`)**: 스레드풀 없이 `_run_single_child(0, goal, child, parent)` 직접 호출. (2091-2095)
- **3b. 배치**: `ThreadPoolExecutor(max_workers=max_children)`에 각 자식을 `submit`. (2101-2111)
  - **인터럽트-aware 폴링 루프**: `as_completed()`(전부 끝날 때까지 블록) 대신
    `wait(pending, timeout=0.5, FIRST_COMPLETED)`로 0.5초마다 깨어난다. (2161-2165)
  - 매 사이클 시작에 `parent._interrupt_requested` 확인. 참이면 끝난 future는 결과 수집,
    미완은 `status="interrupted"` 엔트리로 채우고 **break**(부모가 영원히 안 막히도록). (2122-2159)
  - 완료된 future는 `result()` 수집(예외 시 `status="error"` 엔트리). 진행 라인을 스피너 위에 출력. (2166-2210)
- **3c. 정렬**: `results.sort(key=task_index)` — 입력 순서 복원. (2212-2213)

#### step 3′ — `_run_single_child` 내부 (1321)

| 하위 step | 내용 | 라인 |
| --- | --- | --- |
| 자격증명 lease | credential pool에서 lease 획득 후 자식에 바인딩 | 1345-1355 |
| **하트비트 스레드** | 30s마다 부모 `_touch_activity` 호출 → 게이트웨이 inactivity 타임아웃 방지. 정체(stale) 감지: idle 15사이클(450s)/in-tool 40사이클(1200s) 무진행이면 하트비트 중단 | 1361-1438 |
| registry 등록 | TUI가 kill/pause/status로 타깃할 수 있게 `_register_subagent` | 1444-1466 |
| **타임아웃 실행** | `ThreadPoolExecutor(max_workers=1)`에 `child.run_conversation(user_message=goal, task_id=...)` submit → `future.result(timeout=child_timeout)` | 1491-1514 |
| 타임아웃/예외 경로 | `child.interrupt()` 신호 → `status="timeout"`(또는 `error`) 엔트리 반환. 0-API-call 타임아웃은 진단 덤프 | 1515-1609 |
| **결과 distill** | `summary = result["final_response"]`; `completed`/`interrupted` 플래그로 status·exit_reason 결정 | 1618-1677 |
| tool_trace 구성 | conversation messages에서 assistant tool_calls ↔ tool result를 `tool_call_id`로 페어링 | 1637-1669 |
| entry 빌드 | SubagentResult dict 구성 (tokens/model/cost 포함) | 1684-1718 |
| file-state 알림 | 자식이 부모가 읽었던 파일을 수정했으면 summary에 "re-read before editing" 노트 추가 | 1728-1754 |
| **finally 정리** | 하트비트 stop·join, registry 해제, lease 반납, 전역 tool 이름 복원, `_active_children`에서 제거, **`child.close()`** (터미널/브라우저/프로세스 자원 회수) | 1842-1892 |

### step 4 — 후처리 (정상 종료) (2215-2309)

- **4a. 메모리 통지**: 부모에 `_memory_manager`가 있으면 각 결과로 `on_delegation(task, result, child_session_id)`. (2216-2238)
- **4b. subagent_stop hook + 비용 합산**: 각 엔트리의 `_child_role`/`_child_cost_usd`를 `pop`하여
  worker 스레드 밖(부모 스레드)에서 직렬화 hook 호출, 자식 비용을 부모 세션 비용에 fold. (2256-2299)
- **4c. JSON 반환**: `{"results": results, "total_duration_seconds": round(...)}`. (2301-2309)

---

## 5. 상태 전이 다이어그램

```
delegate_task(goal | tasks, role, parent_agent)
        │
        ▼
 [step 0] parent None? ──yes──▶ tool_error                     (비정상)
        │ no
        ▼
        spawn paused? ──yes──▶ tool_error                       (비정상)
        │ no
        ▼
        depth >= max_spawn_depth? ──yes──▶ {"error": depth limit} (비정상)
        │ no
        ▼
 [step 1] config / creds / task_list 정규화 ──invalid──▶ tool_error (비정상)
        │ ok
        ▼
 [step 2] 각 task ─▶ _build_child_agent
              ├─ child_depth = parent+1
              ├─ role: orchestrator AND depth<max AND kill_switch  ─false─▶ leaf 강등
              ├─ toolset = (요청 ∩ 부모) − 금지셋   (orchestrator면 delegation 재부여)
              └─ AIAgent(ephemeral_prompt=goal, skip_ctx, skip_mem, fresh budget)  ← 격리 경계
        │
        ▼
 [step 3] n_tasks == 1 ?
        ├─ yes ─▶ _run_single_child (직접)
        └─ no  ─▶ ThreadPoolExecutor(max_concurrent_children)
                       │  wait(timeout=0.5, FIRST_COMPLETED) 루프
                       ├─ parent interrupted? ─yes─▶ 미완 = "interrupted", break
                       └─ future 완료 ─▶ 결과 수집 (예외 = "error")
                  ─▶ sort by task_index
        │
        ▼  (각 자식: _run_single_child)
        future.result(timeout=child_timeout)
        ├─ 타임아웃 ─▶ status="timeout"  (child.interrupt())
        ├─ 예외     ─▶ status="error"
        └─ 정상 ─▶ summary = final_response
                   ├─ interrupted ─▶ "interrupted"
                   ├─ summary 有  ─▶ "completed"
                   └─ summary 無  ─▶ "failed"
                   finally: heartbeat stop · unregister · child.close()
        │
        ▼
 [step 4] memory.on_delegation · subagent_stop hook · cost rollup
        │
        ▼
   JSON {"results":[SubagentResult...], "total_duration_seconds":…}  ──▶ 부모에게 반환 (정상)
```

---

## 6. 외부 서브시스템 경계

위임이 **자기 책임 밖으로 위임하는** 지점들. 깊이 들어가지 않되 무엇을 하는지 명시한다.

| 경계 | 위치 | 무엇을 하는가 |
| --- | --- | --- |
| 도구 디스패치 | `run_agent.py:_dispatch_delegate_task` (4509-4526) | LLM의 `delegate_task` tool_call → `delegate_task(...)` 단일 호출 지점. 모든 호출 경로(concurrent/sequential/inline)가 여기를 지난다. |
| 다중 호출 캡 | `run_agent.py:_cap_delegate_task_calls` (2821) | 모델이 한 턴에 `delegate_task`를 여러 번 emit하면 `max_concurrent_children`으로 잘라낸다. |
| 자식 인스턴스 | `run_agent.py:AIAgent.__init__` (`ephemeral_system_prompt`:335, `skip_context_files`:371, `skip_memory`:373) | 격리된 자식 에이전트 본체. delegate_tool이 격리 플래그를 여기에 주입한다. |
| 자식 실행 루프 | `run_agent.py:AIAgent.run_conversation` (4575) | 자식의 실제 LLM 대화 루프. `_run_single_child`가 타임아웃 래퍼 안에서 호출(`delegate_tool.py:1507-1510`). `{"final_response","completed","interrupted","api_calls","messages"}` 반환. |
| 자격증명 해석 | `delegate_tool.py:_resolve_delegation_credentials` (2345) | provider:model 자격증명 번들을 런타임 provider 시스템에서 해석. |
| 자식 정리 | `child.close()` (`delegate_tool.py:1888-1890`) | 터미널 샌드박스·브라우저 데몬·백그라운드 프로세스·httpx 클라이언트 회수. |
| MCP/registry | `delegate_tool.py:_is_mcp_toolset_name` (455) → `tools.registry.registry` | composite/MCP toolset 별칭 해석. |

---

## 7. MoA (Mixture-of-Agents) — `mixture_of_agents_tool` (236)

위임의 변형: **같은 질문을 N개 참조 모델에 병렬로 던지고, 한 집계 모델이 합성**하는 2-레이어 구조.
delegate_task와 달리 `AIAgent` 자식이 아니라 OpenRouter chat completion을 직접 호출한다(`asyncio` 기반).

### 입출력

- 입력: `user_prompt: str`, `reference_models: Optional[List[str]]`, `aggregator_model: Optional[str]` (236-239)
- 출력: JSON 문자열 `{"success", "response", "models_used":{reference_models, aggregator_model}}` (364-383),
  실패 시 `success=False` + `error` (385-409)

### 기본 구성 (64-80)

```python
REFERENCE_MODELS = ["anthropic/claude-opus-4.6", "google/gemini-2.5-pro",
                    "openai/gpt-5.4-pro", "deepseek/deepseek-v3.2"]   # :64
AGGREGATOR_MODEL = "anthropic/claude-opus-4.6"   # :73
REFERENCE_TEMPERATURE = 0.6   # :76  (다양성)
AGGREGATOR_TEMPERATURE = 0.4  # :77  (집중 합성)
MIN_SUCCESSFUL_REFERENCES = 1 # :80  (최소 성공 수)
```

### step별 흐름

- **step 0**: `OPENROUTER_API_KEY` 검증, 없으면 `ValueError`. (303-304)
- **step 1 (Layer 1, 병렬)**: `asyncio.gather(*[_run_reference_model_safe(m, prompt) for m in ref_models])`.
  각 참조 모델은 최대 6회 지수 백오프 재시도, 실패는 graceful 처리. (312-317, 105-178)
- **step 2 (분류·게이트)**: 성공/실패 분리 → 성공 수 `< MIN_SUCCESSFUL_REFERENCES`면 `ValueError`로 중단(일부 실패 허용). (319-339)
- **step 3 (Layer 2, 합성)**: `_construct_aggregator_prompt(AGGREGATOR_SYSTEM_PROMPT, successful_responses)`로
  논문 프롬프트 + 번호 매긴 응답들(`f"{i+1}. {response}"`, 101)을 조립 → `_run_aggregator_model`로 단일 답 합성. (345-356)
- **step 4**: 결과 JSON 직렬화. (364-383)

`AGGREGATOR_SYSTEM_PROMPT`(83-85)는 MoA 논문 원문 그대로("synthesize these responses into a single,
high-quality response ... critically evaluate ...").

```
user_prompt ─┬─ ref_model_1 ┐
             ├─ ref_model_2 ┼─(asyncio.gather, 병렬)─▶ 성공 응답들 ─▶ aggregator(논문 프롬프트 + 번호매김) ─▶ 합성 답
             └─ ref_model_N ┘                          (성공<MIN 이면 ValueError)
```

---

## 8. 검증 매트릭스

3장의 모든 라인 인용을 `Read`/`grep -n`으로 원본과 대조한 결과.

| 단계 / 주장 | 원본 위치 | 결과 |
| --- | --- | --- |
| 진입점 `delegate_task` | `delegate_tool.py:1918` | ✅ |
| parent None 가드 | `delegate_tool.py:1943-1944` | ✅ |
| spawn paused 킬스위치 | `delegate_tool.py:1949-1953` | ✅ |
| 깊이 가드 (비정상 종료) | `delegate_tool.py:1960-1972` | ✅ |
| caller max_iterations 무시 | `delegate_tool.py:1982-1988` | ✅ |
| 단일/배치 task_list 정규화 | `delegate_tool.py:2008-2023` | ✅ |
| 자식 빌드 루프 + per-task role | `delegate_tool.py:2056-2086` | ✅ |
| 단일 직접 실행 | `delegate_tool.py:2091-2095` | ✅ |
| 배치 ThreadPoolExecutor | `delegate_tool.py:2101-2111` | ✅ |
| 인터럽트 폴링 + interrupted 엔트리 | `delegate_tool.py:2122-2159` | ✅ |
| `wait(timeout=0.5, FIRST_COMPLETED)` | `delegate_tool.py:2161-2165` | ✅ |
| task_index 정렬 | `delegate_tool.py:2212-2213` | ✅ |
| memory.on_delegation | `delegate_tool.py:2216-2238` | ✅ |
| subagent_stop hook + 비용 fold | `delegate_tool.py:2256-2299` | ✅ |
| JSON 반환 | `delegate_tool.py:2301-2309` | ✅ |
| role 강등 (유일 지점) | `delegate_tool.py:904-913` | ✅ |
| toolset 교집합 + strip | `delegate_tool.py:945-961` | ✅ |
| orchestrator delegation 재부여 | `delegate_tool.py:967-968` | ✅ |
| 격리 플래그 (ephemeral/skip_ctx/skip_mem/fresh budget) | `delegate_tool.py:1121,1124,1125,1136` | ✅ |
| `_build_child_system_prompt` (goal+context만) | `delegate_tool.py:569-642` | ✅ |
| `_strip_blocked_tools` 금지셋 | `delegate_tool.py:672-680` | ✅ |
| `_normalize_role` | `delegate_tool.py:312-326` | ✅ |
| 하트비트 + stale 감지 | `delegate_tool.py:1361-1438` | ✅ |
| 타임아웃 실행 (`future.result(timeout=...)`) | `delegate_tool.py:1491-1514` | ✅ |
| 자식 실행 `run_conversation` 호출 | `delegate_tool.py:1505-1510` | ✅ |
| 결과 distill (final_response→summary, status) | `delegate_tool.py:1618-1677` | ✅ |
| tool_trace 페어링 | `delegate_tool.py:1637-1669` | ✅ |
| finally 정리 + `child.close()` | `delegate_tool.py:1842-1892` | ✅ |
| 기본 상수 (`MAX_DEPTH=1`, 동시성3, 타임아웃600) | `delegate_tool.py:132-137,512-513` | ✅ |
| 외부 경계 `_dispatch_delegate_task` | `run_agent.py:4509-4526` | ✅ |
| 외부 경계 `run_conversation` | `run_agent.py:4575` | ✅ |
| 외부 경계 `AIAgent.__init__` 격리 파라미터 | `run_agent.py:335,371,373` | ✅ |
| MoA 진입점 | `mixture_of_agents_tool.py:236` | ✅ |
| MoA Layer 1 `asyncio.gather` | `mixture_of_agents_tool.py:312-317` | ✅ |
| MoA 최소 성공 게이트 | `mixture_of_agents_tool.py:338-339` | ✅ |
| MoA Layer 2 합성 | `mixture_of_agents_tool.py:345-356` | ✅ |
| MoA 집계 프롬프트(논문 원문) | `mixture_of_agents_tool.py:83-85,90-102` | ✅ |
| MoA 기본 모델/온도/최소수 상수 | `mixture_of_agents_tool.py:64-80` | ✅ |

### ⚠️ 주의 (기존 미러 README와의 차이)

| 항목 | 원본 사실 | 비고 |
| --- | --- | --- |
| `max_spawn_depth` 기본값 | **`MAX_DEPTH = 1`** (`delegate_tool.py:133`) — 기본은 "평면"(부모0→자식1, 손자 거부) | ⚠️ 기존 `README.md`는 "기본 2"라고 서술하나 실제 상수는 1. 단 `delegate_task` 내부 주석(:1959 "default 2 for parity")도 상수와 어긋나 있어, **상수값 1이 권위**다. |
| MoA `MIN_SUCCESSFUL_REFERENCES` | **1** (`mixture_of_agents_tool.py:80`) — 참조 1개만 성공해도 합성 진행 | ⚠️ 강건성 관점에서 "다수 성공 필요"로 오해하기 쉬우나 실제 하한은 1. |

---

## 9. 한 줄 요약

> Hermes의 위임은 **부모 history를 차단한 깨끗한 자식 `AIAgent`를, 부모 toolset의 교집합 권한으로,
> 타임아웃·깊이·동시성 가드 안에서 (병렬) 실행하고, 자식의 `final_response`만 구조화해 회수**하는
> 메커니즘이다. MoA는 이 위임을 부채꼴로 펼친 뒤 한 집계 모델로 한 번 더 합성하는 변형이다.
