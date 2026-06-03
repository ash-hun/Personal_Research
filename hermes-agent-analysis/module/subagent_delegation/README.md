# subagent_delegation — Hermes 서브에이전트 위임 미러

Hermes Agent(Nous Research)가 **격리된 서브에이전트를 띄워 병렬 작업을 처리하는 방식**(`delegate_task` 도구)과
**Mixture-of-Agents(MoA) 패턴**을 stdlib만으로 재현한 학습용 미러입니다. 실제 LLM 호출 부분만 `run_agent`라는
교체 가능한 콜러블 뒤로 숨겨서, 외부 의존성 없이 `python3 demo.py` 한 줄로 전체 흐름이 돌아갑니다.

> 목표는 "그대로 가져다 쓰는 라이브러리"가 아니라, **연구자가 한 파일만 읽고도 Hermes의 위임 메커니즘을 이해하고
> 자기 식대로 커스터마이즈**할 수 있게 만드는 것입니다. 함수/필드 이름은 원본과 최대한 똑같이 맞췄습니다.

---

## 기능 개요

부모 에이전트가 큰 작업을 잘게 쪼개서, **각각을 깨끗한(=부모 히스토리를 모르는) 자식 에이전트에게 위임**하고,
자식들을 (필요하면 병렬로) 돌린 뒤, **자식의 최종 답변만 다시 tool result로 회수**하는 패턴입니다.

핵심은 네 가지입니다.

1. **격리(Isolation)** — 자식은 부모의 대화 기록을 절대 보지 못합니다. 오직 위임 브리프(목표 + 컨텍스트)만 봅니다.
2. **스폰(Spawn)** — 부모의 toolset과 교집합을 취하고 금지 도구를 떼어낸 새 자식을 만듭니다.
3. **수집(Collect)** — 자식의 `final_response`를 `summary`로 뽑아 구조화된 결과로 되돌립니다.
4. **MoA** — 같은 질문을 여러 참조 에이전트에게 병렬로 던지고, 그 답들을 한 명의 집계자(aggregator)가 합성합니다.

---

## Hermes 실제 구현 방식

### 격리 (Context Isolation)

자식은 부모와 **완전히 별개의 `AIAgent` 인스턴스**입니다. Hermes는 자식을 만들 때
`ephemeral_system_prompt=child_prompt`, `skip_context_files=True`, `skip_memory=True`, 그리고 **새 iteration 예산**을
줍니다. 부모의 conversation history를 자식에게 넘기는 경로 자체가 없습니다. 자식이 받는 건 `goal`(YOUR TASK)과
선택적 `context`(CONTEXT)뿐입니다.

> `delegate_tool.py:1106-1137` (`AIAgent(...)` 생성), `delegate_tool.py:569-642` (브리프 프롬프트 작성)

### 스폰 + allowed-tools 제한

자식의 toolset은 다음 순서로 정해집니다.

1. 호출자가 `toolsets`를 지정하면 **부모의 toolset과 교집합** — 자식은 부모에게 없는 도구를 절대 얻지 못합니다.
2. 지정하지 않으면 부모 toolset을 상속.
3. `delegation / clarify / memory / code_execution` 같은 **금지 toolset 제거**(`strip_blocked_tools`).
4. role이 `orchestrator`면 `delegation`을 다시 붙여서 자기 워커를 스폰할 수 있게 함.

`leaf`(기본값)는 더 위임할 수 없고, `orchestrator`만 재위임이 가능하되 `max_spawn_depth`(기본 2)로 깊이가 묶입니다.

> `delegate_tool.py:945-968` (교집합 + role 재부여), `delegate_tool.py:672-680` (금지 toolset), `:312-329` (role 정규화)

### 수집 (Result Collection)

자식은 `child.run_conversation(user_message=goal, task_id=...)`로 돌고, Hermes는 결과 dict에서
`final_response`를 꺼내 `summary`로 삼습니다. `completed / interrupted` 플래그로 status와 exit_reason을 정하고,
메시지에서 가벼운 tool trace를 뽑습니다. 자식이 멈춰도 부모가 영원히 막히지 않도록 **하드 타임아웃**(별도 스레드 +
`future.result(timeout=...)`)을 씌웁니다.

> `delegate_tool.py:1507-1510` (자식 실행), `:1620-1700` (결과 distill), `:1492-1514` (타임아웃)

### 병렬 fan-out

단일 작업은 스레드풀 오버헤드 없이 바로 실행하고, **배치는 `ThreadPoolExecutor(max_workers=max_concurrent_children)`**
로 병렬 실행합니다. 결과는 `task_index`로 정렬해 입력 순서를 복원한 뒤, `{"results": [...], "total_duration_seconds": ...}`
형태의 JSON tool result로 부모에게 반환됩니다.

> `delegate_tool.py:2091-2213` (단일/배치 분기, 병렬 루프), `:2303-2309` (JSON 직렬화)

### MoA (Mixture-of-Agents)

2-레이어 구조입니다. **Layer 1**은 N개의 참조 모델에게 같은 프롬프트를 병렬로(`asyncio.gather`) 던져 다양한 답을
받습니다(일부 실패는 허용). **Layer 2**는 집계 모델이 논문의 합성 프롬프트(`AGGREGATOR_SYSTEM_PROMPT`)에 모든 참조
답변을 번호 매겨 붙인 뒤 하나의 고품질 답으로 합성합니다. 즉 MoA는 "위임을 부채꼴로 펼친 뒤 한 번 더 합성하는 것"입니다.

> `mixture_of_agents_tool.py:236-409` (메인 흐름), `:312-356` (2-레이어), `:90-102` (집계 프롬프트 구성), `:83-85` (논문 프롬프트)

---

## 핵심 소스 파일 매핑

| 이 미러의 심볼 | Hermes 원본 위치 |
| --- | --- |
| `build_child_system_prompt()` | `tools/delegate_tool.py:_build_child_system_prompt` (569) |
| `build_child_agent()` | `tools/delegate_tool.py:_build_child_agent` (870) |
| `run_single_child()` | `tools/delegate_tool.py:_run_single_child` (1321) |
| `delegate_task()` | `tools/delegate_tool.py:delegate_task` (1918) |
| `strip_blocked_tools()` | `tools/delegate_tool.py:_strip_blocked_tools` (672) |
| `resolve_child_toolsets()` | `tools/delegate_tool.py:_build_child_agent` 교집합 로직 (945-968) |
| `normalize_role()` / `Role` | `tools/delegate_tool.py:_normalize_role` (312) |
| `mixture_of_agents()` | `tools/mixture_of_agents_tool.py:mixture_of_agents_tool` (236) |
| `construct_aggregator_prompt()` | `tools/mixture_of_agents_tool.py:_construct_aggregator_prompt` (90) |
| `AGGREGATOR_SYSTEM_PROMPT` | `tools/mixture_of_agents_tool.py:83-85` (논문 그대로) |
| `RunAgent` 콜러블 계약 | `run_agent.py:AIAgent.run_conversation` (4575) / `_dispatch_delegate_task` (4509) |

---

## I/O 인터페이스 (DelegateSpec → SubagentResult)

### 입력: `DelegateSpec`

자식이 보는 **유일한 것**입니다. 부모 히스토리는 여기에 들어오지 않습니다 — 이게 격리 경계입니다.

| 필드 | 타입 | 의미 |
| --- | --- | --- |
| `goal` | `str` | 자식이 달성할 목표 (프롬프트의 YOUR TASK) |
| `context` | `Optional[str]` | 인계용 배경 텍스트 (CONTEXT 블록) |
| `toolsets` | `Optional[List[str]]` | 요청 toolset (부모와 교집합 후 금지 제거). None이면 부모 상속 |
| `role` | `Role` | `leaf`(기본) 또는 `orchestrator` (재위임 가능 여부) |
| `max_iterations` | `int` | 자식 전용 반복 예산 (부모와 공유 안 함) |
| `workspace_path` | `Optional[str]` | 프롬프트에 주입할 작업 디렉토리 힌트 |

### 출력: `SubagentResult`

부모가 회수하는 구조화된 결과. `summary`가 곧 tool result로 흐르는 자식의 최종 답변입니다.

| 필드 | 타입 | 의미 |
| --- | --- | --- |
| `task_index` | `int` | 배치 내 위치 (입력 순서 복원용) |
| `status` | `str` | `completed`/`failed`/`timeout`/`error`/`interrupted` |
| `summary` | `Optional[str]` | 자식의 `final_response` — 부모에게 돌아가는 답 |
| `exit_reason` | `str` | `completed`/`max_iterations`/`timeout`/`error` |
| `api_calls`, `duration_seconds`, `model`, `role`, `tool_trace`, `error` | — | 부가 메타 |

---

## 데이터 흐름

```
부모 AIAgent (history = 비밀 포함)
   │  delegate_task(goal=... 또는 tasks=[...])
   ▼
[정규화] goal/tasks → List[DelegateSpec]      (깊이 가드: depth >= max_spawn_depth 면 거부)
   │
   ▼
[빌드] 각 spec → build_child_agent()
   │      ├─ toolset = (요청 ∩ 부모) − 금지셋   (orchestrator면 delegation 재부여)
   │      ├─ system_prompt = goal + context 만으로 구성   ← 격리 경계
   │      └─ depth = 부모 depth + 1, role 강등 판단
   ▼
[실행] run_single_child()  (타임아웃 래핑)
   │      단일 → 직접 실행
   │      배치 → ThreadPoolExecutor(max_concurrent_children) 병렬
   │      child.run_agent(user_message=goal, ...) → final_response
   ▼
[수집] SubagentResult(summary=final_response, status, ...)
   │      task_index 로 정렬 (입력 순서 복원)
   ▼
JSON tool result  {"results":[...], "total_duration_seconds":...}  → 부모에게 반환


MoA:  user_prompt ─┬─ ref_agent_1 ┐
                   ├─ ref_agent_2 ┼─(병렬)─ 응답들 → aggregator(AGGREGATOR_SYSTEM_PROMPT + 번호매김) → 합성 답
                   └─ ref_agent_N ┘
```

---

## 커스터마이징 · 응용 포인트

- **실제 LLM 연결**: `fake_run_agent`를 `RunAgent` 계약(`user_message / system_prompt / toolsets / task_id` →
  `{"final_response", "completed", "api_calls", ...}`)을 만족하는 진짜 모델 호출로 바꾸면 됩니다. `ParentAgent.run_agent`
  에 넣어두면 자식들이 자동으로 그걸 씁니다.

- **병렬화 튜닝**: `MAX_CONCURRENT_CHILDREN`(스레드풀 너비)와 `delegate_task(..., timeout_seconds=...)`로 동시성·타임아웃을
  조절합니다. CPU 바운드 자식이면 스레드 대신 프로세스풀로 바꾸는 것도 고려하세요.

- **allowed-tools 제한**: `resolve_child_toolsets`의 교집합 규칙이 보안 핵심입니다. 자식에게 절대 주면 안 되는 도구는
  `BLOCKED_TOOLSET_NAMES`에 추가하면 자동으로 떨어집니다. "민감 작업은 read-only toolset만 위임" 같은 정책을 여기서 강제하세요.

- **재위임 깊이**: `MAX_SPAWN_DEPTH`로 orchestrator→worker 트리가 무한 증식하는 걸 막습니다. `leaf`만 허용하면 단층
  위임만, `orchestrator`를 허용하면 다층 분해가 가능합니다.

- **MoA 확장**: `reference_run_agents`에 서로 다른 모델/프롬프트 스타일을 넣어 다양성을 키우고, `aggregator_run_agent`를
  가장 강한 모델로 두면 논문 그대로의 효과를 냅니다. `min_successful_references`로 일부 실패를 견디는 강건성도 조절합니다.

---

## 실행

```bash
python3 demo.py
```

데모는 (1) 단일 위임 + 격리/toolset 제한 시연, (2) 3개 배치 병렬 fan-out, (3) MoA 합성, (4) 깊이 가드를 차례로 보여줍니다.
모두 가짜 자식 brain으로 오프라인 실행됩니다.
