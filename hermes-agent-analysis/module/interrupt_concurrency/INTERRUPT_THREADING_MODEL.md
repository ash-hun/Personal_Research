# Hermes Agent — 인터럽트 & 멀티스레드 협조적 취소(cooperative cancellation) 모델

> `conversation_loop`의 **턴 상태 리셋**에서 등장하는 `_interrupt_requested`가
> "바깥 스레드에서 True로 세팅된다"는 말의 실체를, Hermes 원본 코드로 교차검증해 정리한 문서입니다.
>
> - 원본: `../../_reference/hermes-agent/`
>   - `tools/interrupt.py` — per-thread 인터럽트 신호 서브시스템 (98 LOC)
>   - `run_agent.py` — `AIAgent.interrupt()` / `clear_interrupt()`
>   - `agent/conversation_loop.py` — 실행 스레드 바인딩 + 루프 인터럽트 체크
>   - `cli.py` — 에이전트를 백그라운드 데몬 스레드로 구동
> - 관련 모듈: [`../conversation_loop/`](../conversation_loop/) (이 인터럽트 플래그를 **읽는** 주체)
> - 검증일: 2026-06-07

---

## 왜 별도 모듈인가

인터럽트 신호는 한 군데가 아니라 **루프·도구 실행·서브에이전트를 가로지르는** cross-cutting 관심사다.
Hermes 원본도 이를 `tools/interrupt.py`라는 **독립 파일**로 분리하고 그 docstring에서 목적을 명시한다:

> "Per-thread interrupt signaling for all tools. Provides thread-scoped interrupt tracking so that
> interrupting one agent session does not kill tools running in other sessions. This is critical in
> the gateway where multiple agents run concurrently in the same process."

즉 `conversation_loop`는 이 신호의 **소비자(consumer)** 중 하나일 뿐이고, 인터럽트 자체는 별도 서브시스템이다.

---

## 핵심 질문: `_interrupt_requested`는 누가 True로 만드는가

`conversation_loop` 미러의 턴 상태 리셋에는 다음이 있다:

```python
agent._interrupt_requested = False   # 사용자 stop 신호. 루프가 읽기만 함
```

루프는 이 값을 **읽기만** 한다 — 매 바퀴 맨 앞에서 검사해 True면 모델 호출 전에 즉시 break.
그렇다면 **True로 쓰는 주체는 누구인가?** 답은 "루프를 돌리는 스레드가 아닌 다른 스레드"다.

---

## 문제 상황: 루프는 바빠서 스스로 입력을 못 본다

`run_conversation`은 한 번 호출되면 `모델 호출 → 도구 실행 → 재호출`을 수십 초~수 분간 한 흐름으로 붙잡고 돈다.
그 와중에 사용자가 Ctrl-C를 누르거나 새 메시지를 보내면 멈춰야 하는데, 루프는 모델 응답을 기다리거나
도구를 실행하느라 **바빠서** 자기가 "사용자가 방금 입력했나?"를 들여다볼 여유가 없다.
한 함수는 한 번에 한 줄씩만 실행되기 때문이다.

해결책: **입력을 듣는 일과 루프를 도는 일을 서로 다른 스레드로 분리**한다.

---

## 스레드 구성 (원본 검증)

### 1. 에이전트는 백그라운드 데몬 스레드에서 돈다 — `cli.py:12295`

```python
agent_thread = threading.Thread(target=run_agent, daemon=True)
agent_thread.start()
# 이후 메인 스레드는 dedicated interrupt 큐를 감시한다
```

- **Worker 스레드** = `run_agent`(→ `run_conversation`)를 돌리는 데몬 스레드. 모델·도구로 바쁨.
- **Main 스레드** = 에이전트가 도는 동안 **별도 interrupt 큐를 감시**(주석: "Monitor the dedicated
  interrupt queue while the agent runs"). 한가하게 사용자 입력만 들음.
- `daemon=True` — 사용자가 터미널 탭을 닫으면(SIGHUP) 메인 스레드가 종료되고 데몬 Worker는 자동 회수된다.

### 2. Worker는 시작 즉시 자기 스레드 ID를 등록한다 — `agent/conversation_loop.py:746`

```python
agent._execution_thread_id = threading.current_thread().ident
```

`run_conversation` 진입 시 "**내가 이 스레드 위에서 돌고 있다**"를 `agent` 객체에 박아둔다.
(미러의 Step 0 턴 상태 리셋과 같은 위치대다.) 이 ID가 있어야 인터럽트를 **정확히 이 Worker만** 겨냥할 수 있다.

### 3. Main 스레드가 Worker를 향해 인터럽트를 "쏜다" — `run_agent.py:1962, 1991-1992`

```python
def interrupt(self, message: str = None) -> None:
    self._interrupt_requested = True                      # ① 루프가 읽는 boolean 플래그
    self._interrupt_message = message
    if self._execution_thread_id is not None:
        _set_interrupt(True, self._execution_thread_id)   # ② 실행 중인 도구가 읽는 스레드별 신호
```

`interrupt()`를 호출하는 주체가 곧 Main 스레드(또는 메시징 게이트웨이)다.
→ 이것이 "`_interrupt_requested`가 **바깥 스레드에서 True로 세팅된다**"의 글자 그대로의 실체다.

`_execution_thread_id`가 아직 None이면(인터럽트가 바인딩보다 먼저 도착) `_interrupt_thread_signal_pending = True`로
지연 처리해, 엉뚱한 호출자 스레드를 인터럽트하는 사고를 막는다(`run_agent.py:1995-1999`).

---

## 두 개의 인터럽트 채널

인터럽트는 한 가지 신호가 아니라 **두 채널**로 동시에 전파된다. 둘 다 필요한 이유가 다르다.

| 채널 | 무엇 | 누가 읽나 | 왜 필요한가 |
|------|------|-----------|-------------|
| ① `agent._interrupt_requested = True` | 단순 boolean 플래그 | `run_conversation` 루프 | 루프가 바퀴 맨 앞에서 확인 → **다음 모델 호출 전** 안전하게 break |
| ② `set_interrupt(True, tid)` | 스레드별 신호 집합 | 실행 중인 **도구**(`is_interrupted()`) | 네트워크 I/O에 멈춘 도구가 **timeout 안 기다리고** 즉시 빠져나오게 |

### 채널 ②의 구현 — `tools/interrupt.py`

```python
_interrupted_threads: set[int] = set()   # 인터럽트된 스레드 ident 집합
_lock = threading.Lock()

def set_interrupt(active: bool, thread_id: int | None = None) -> None:
    tid = thread_id if thread_id is not None else threading.current_thread().ident
    with _lock:
        _interrupted_threads.add(tid) if active else _interrupted_threads.discard(tid)

def is_interrupted() -> bool:                       # 인자 없음 — 현재 스레드만 확인
    return threading.current_thread().ident in _interrupted_threads
```

도구는 자기 코드 안에서 `if is_interrupted(): return {"output": "[interrupted]", "returncode": 130}` 식으로
**자발적으로** 확인하고 빠져나온다. 강제 종료가 아니다.

---

## 왜 강제 종료가 아니라 "플래그"인가 — 협조적 취소(cooperative cancellation)

Main 스레드가 Worker를 **강제로 죽이지 않는** 것이 핵심이다. 모델 호출/도구 실행 중간에 강제 종료하면:

- 도구가 반쯤 실행되어 파일·상태가 깨지고,
- `tool_call`에 대응하는 결과 메시지가 빠져 대화 기록(messages)의 1:1 정합성이 망가진다.

대신 **"멈춰달라"는 깃발만 꽂아두고**, Worker가 안전한 지점에서 스스로 확인하고 정리하며 빠져나온다.
그래서 `conversation_loop`에 인터럽트 체크가 **두 군데**다:

1. **루프 맨 앞** — 다음 모델 호출을 시작하기 전에 확인 → 낭비 없이 break
2. **도구 배치 도중** — 남은 도구를 `"cancelled"` 결과로 채워 모든 tool_call의 1:1 대응을 보존

> 비유: 주방장(Worker)이 요리에 집중 중이고 홀 직원(Main)이 손님 응대를 한다. 손님이 "취소요" 하면
> 홀 직원은 칼을 빼앗지(강제 종료) 않고 **주문판에 '취소' 메모**(`= True`)를 붙인다. 주방장은 한 단계
> 끝낼 때마다 주문판을 흘끔 보고, 메모가 있으면 다음 요리를 시작하지 않고 정리한다(다음 바퀴에서 break).

---

## 단순 2-스레드를 넘어서 — 멀티 에이전트 격리 & 재귀 전파

Hermes는 한 프로세스에서 여러 에이전트가 동시에 도는 gateway 환경을 전제하므로,
인터럽트를 **스레드 단위로 격리**하고 관련 스레드에 **재귀 전파**한다.

| 스레드 | 역할 | 근거 |
|--------|------|------|
| Main | 입력/인터럽트 큐 감시, `interrupt()` 호출 | `cli.py:12296~` |
| **Worker** | `run_conversation` 실행 | `cli.py:12295` |
| 도구 워커 풀 | 병렬 도구를 `ThreadPoolExecutor`로 실행 | `_tool_worker_threads` (`run_agent.py:2007~`) |
| 서브에이전트 | delegation 시 자식 에이전트 각자 별도 스레드 | `_active_children` (`run_agent.py:2024~`) |
| 백그라운드 리뷰 | memory/skill 큐레이션 | `run_agent.py:1372` (`name="bg-review"`) |

`interrupt()`는 한 번 불리면:

1. Worker의 boolean 플래그(`_interrupt_requested`)를 세우고,
2. Worker 스레드 tid에 `set_interrupt(True, tid)`,
3. **도구 워커 풀의 각 tid**에도 fan-out (`run_agent.py:2007~`) — 이미 돌고 있는 병렬 도구도 즉시 인지,
4. **자식 에이전트들에게 재귀 호출**(`child.interrupt(message)`, `run_agent.py:2024~2032`).

격리가 핵심인 이유: 한 세션의 인터럽트가 **같은 프로세스의 다른 세션 도구를 죽이면 안 되기** 때문.
`is_interrupted()`가 현재 스레드 ident만 보므로, 내 세션 신호는 내 스레드 계열에만 닿는다.

`clear_interrupt()`(`run_agent.py:2029~`)는 턴 경계에서 플래그와 모든 워커 tid 비트를 청소해,
재활용된 워커 스레드가 다음 무관한 도구 호출에서 stale 인터럽트로 오발하는 것을 막는다.

---

## 미러(conversation_loop)와의 관계 — 무엇이 보존/생략되었나

`module/conversation_loop/`의 stdlib-only 미러는 이 동시성 인프라를 **갖고 있지 않다**:

- 미러의 `_interrupt_requested`는 그냥 boolean이고, 데모에서 True로 세팅하는 바깥 스레드가 없다.
- 미러에서 `threading.Lock`을 쓰는 곳은 `IterationBudget`뿐이다.
- 미러는 "**루프가 플래그를 읽고 협조적으로 멈춘다**"는 *제어 흐름*만 보존하고
  (`conversation_loop.py:592` 루프 앞 체크, `:428` 도구 배치 중 체크),
  실제 스레드 생성·시그널·per-thread 격리·재귀 전파는 `[EXTERNAL SUBSYSTEM]` 성격으로 단순화했다.

| 항목 | 원본 위치 | 미러 |
|------|-----------|------|
| 실행 스레드 바인딩 | `agent/conversation_loop.py:746` | 생략 (단일 스레드 가정) |
| 루프 인터럽트 체크 (모델 호출 전) | `conversation_loop.py:801-806` | ✅ 보존 (`:592-596`) |
| 도구 배치 중 인터럽트 → cancelled | `tool_executor` | ✅ 보존 (`:428-436`) |
| boolean 플래그 세팅 | `run_agent.py:1991` `interrupt()` | boolean만 있고 setter 없음 |
| per-thread 신호 `set_interrupt/is_interrupted` | `tools/interrupt.py` | 생략 |
| 도구풀/서브에이전트 재귀 전파 | `run_agent.py:2007~2032` | 생략 |

---

## 한 줄 요약

`_interrupt_requested`가 "바깥 스레드에서 True로 세팅"된다 = **에이전트는 백그라운드 Worker 스레드에서
돌고, 한가한 Main 스레드가 사용자 입력을 듣다가 `agent.interrupt()`로 플래그를 세운다.**
Worker는 강제로 죽지 않고 안전한 지점에서 그 플래그를 읽어 스스로 정리하며 멈춘다(협조적 취소).
실제 Hermes는 여기에 더해 인터럽트를 스레드 단위로 격리하고 도구풀·서브에이전트에 재귀 전파한다.
