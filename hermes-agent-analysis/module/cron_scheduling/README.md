# cron_scheduling — Hermes Cron Scheduler 미러

> Hermes Agent(Nous Research)의 내장 cron 스케줄러를 **stdlib만으로** 재현한 학습용 미러입니다.
> 외부 의존성 없이 한 파일로 읽고, 돌려보고, 마음껏 뜯어볼 수 있게 만들었어요.

---

## 1. 기능 개요 — unattended 자동화

사람이 옆에 붙어 있지 않아도 에이전트가 **스스로 정해진 시각에 깨어나** 일을 하고,
결과를 사용자에게 전달하는 기능입니다.

- "매일 아침 9시에 일일 리포트 올려줘"
- "30분마다 안 읽은 메일 요약해줘"
- "45분 뒤에 스트레칭 알림 줘"

이런 자연어 요청이 곧바로 **cron job**이 되어 저장되고, 스케줄러가 주기적으로
"지금 실행할 게 있나?"를 확인합니다. due(실행 시점 도래)한 job이 있으면 에이전트를
unattended 모드로 한 번 돌리고, 그 결과를 Telegram·Discord 같은 **사용자가 있던 플랫폼**으로
배달(delivery)합니다. 사람의 개입(unattended)이 전혀 없다는 게 핵심이에요.

---

## 2. Hermes 실제 구현 방식

실제 Hermes는 네 단계로 동작합니다. 이 미러는 그 골격을 그대로 따릅니다.

### (1) Job 모델 — 스케줄을 어떻게 표현하나
`create_job()`이 자연어성 입력을 받아 job 레코드(dict)를 만듭니다. 핵심은 `schedule`
필드인데, `parse_schedule()`이 문자열을 구조화된 스펙으로 바꿉니다.

| 입력 예시 | kind | 의미 |
|---|---|---|
| `"30m"`, `"2h"`, `"1d"` | `once` | 지금부터 그만큼 뒤 한 번 |
| `"every 30m"` | `interval` | 그 간격으로 반복 |
| `"0 9 * * *"` | `cron` | 5필드 cron 표현식 |
| `"2026-06-03T14:00"` | `once` | 그 시각에 한 번 |

one-shot은 자동으로 `repeat.times = 1`이 되고, delivery 기본값은 origin이 있으면
`origin`, 없으면 `local`입니다.

### (2) due 판정 — 무엇이 실행 시점인가
`get_due_jobs()`(미러의 `Scheduler.due_jobs(now)`)가 각 job의 `next_run_at`을 현재 시각과
비교합니다. 단순 비교에 더해 두 가지 안전장치가 있어요.

- **one-shot grace window**: 요청 분(minute)보다 몇 초 늦게 만들어진 job도 다음 tick에
  무사히 한 번 실행되도록 약간의 유예를 줍니다.
- **stale fast-forward**: 게이트웨이가 한참 꺼져 있다 켜졌을 때, 놓친 반복 실행이
  한꺼번에 우르르 터지지 않도록 grace(스케줄 주기의 절반, 120초~2시간으로 clamp)를
  넘긴 반복 job은 실행 대신 `next_run_at`만 다음 미래 시각으로 건너뜁니다.

### (3) 트리거 — due한 job을 어떻게 돌리나
`tick()`이 due job들을 모읍니다. 실행 **전에** 먼저 `advance_next_run()`으로 반복 job의
`next_run_at`을 미리 한 칸 당겨 놓습니다(at-most-once). 실행 도중 크래시가 나도 다음
재시작 때 같은 job이 다시 터지지 않게 하기 위함이죠. 그다음 실제 에이전트 실행
`run_job()`이 `(success, output, final_response, error)`를 돌려줍니다.

### (4) delivery — 결과를 어디로 보내나
`_deliver_result()`가 `deliver` 값을 실제 타깃으로 해석합니다.

- `local` → 배달 안 함(파일에만 저장)
- `origin` → job이 만들어진 그 채팅(`origin.platform`, `origin.chat_id`)
- `telegram` / `telegram:<chat_id>` → 특정 플랫폼/채널
- 에이전트가 `[SILENT]`를 응답하면 배달을 건너뜀(보낼 내용 없음)

실패한 job은 에러 알림 형태로 배달되고, delivery 실패는 agent 실패와 **별도로**
`last_delivery_error`에 기록됩니다.

---

## 3. 핵심 소스 파일 매핑

레퍼런스: `_reference/hermes-agent/`

| 이 미러 | Hermes 원본 | 역할 |
|---|---|---|
| `parse_duration` / `parse_schedule` | `cron/jobs.py` | 스케줄 문자열 → 구조화 스펙 |
| `_CronExpr` | (croniter 대체) | stdlib 5필드 cron 평가기 |
| `compute_next_run` / `_recoverable_oneshot_run_at` / `_compute_grace_seconds` | `cron/jobs.py` | 다음 실행 시각·grace 계산 |
| `CronJob` / `create_job` | `cron/jobs.py::create_job` | job 모델 |
| `JobStore` | `cron/jobs.py` (`load_jobs`/`save_jobs`) | JSON 영속화 |
| `Scheduler.due_jobs` | `cron/jobs.py::_get_due_jobs_locked` | due 판정 |
| `Scheduler.advance_next_run` / `mark_job_run` | `cron/jobs.py` | 실행 전후 bookkeeping |
| `Scheduler.tick` | `cron/scheduler.py::tick` | tick 루프 |
| `run_agent` (주입) | `cron/scheduler.py::run_job` | 에이전트 실행 |
| `deliver` (주입) | `cron/scheduler.py::_deliver_result` | 플랫폼 배달 |
| `cronjob_tool` | `tools/cronjob_tools.py::cronjob` | 에이전트용 create/list/remove 툴 |

> 참고: 실제 Hermes는 5필드 cron에 외부 패키지 `croniter`를 씁니다. stdlib-only 제약을
> 지키려고 이 미러는 `*`, `,`, `a-b`, `*/step`를 지원하는 작은 `_CronExpr`로 대체했어요.
> `"0 9 * * *"` 같은 일반적인 표현식은 그대로 동작합니다.

---

## 4. I/O 인터페이스

### CronJob 정의
```python
@dataclass
class CronJob:
    id: str
    name: str
    prompt: str                       # unattended로 돌릴 자연어 task
    schedule: Dict[str, Any]          # parse_schedule 결과
    schedule_display: str
    deliver: str = "local"            # local | origin | telegram | telegram:<chat>
    origin: Optional[Dict[str, Any]] = None
    repeat: Repeat = Repeat()         # times=None 이면 무한
    enabled: bool = True
    state: str = "scheduled"          # scheduled | completed | paused | error
    next_run_at: Optional[str] = None # 스케줄러가 now와 비교하는 ISO 시각
    last_run_at / last_status / last_error / last_delivery_error: ...
```

### Scheduler / tool 시그니처
```python
# 주입형 콜러블 (Hermes가 게이트웨이 기동 시 실제 agent/adapter를 꽂는 자리)
RunAgentFn = Callable[[CronJob, datetime], AgentRunResult]
DeliverFn  = Callable[[CronJob, str], DeliveryResult]

scheduler = Scheduler(store, run_agent=..., deliver=...)
scheduler.due_jobs(now: datetime) -> List[CronJob]
scheduler.tick(now: datetime, *, verbose=False) -> TickReport

# 에이전트가 호출하는 툴 (JSON 문자열 반환)
cronjob_tool("create", store, prompt=..., schedule=..., deliver=..., now=...)
cronjob_tool("list",   store, include_disabled=False)
cronjob_tool("remove", store, job_id=...)
```

`AgentRunResult(success, final_response, output, error)`,
`DeliveryResult(delivered, target, error)` — 둘 다 `@dataclass`이고 타입 어노테이션이
붙어 있어요. 원본의 `(success, output, final_response, error)` 튜플 / `_deliver_result`의
"None이면 성공" 규약을 그대로 옮긴 것입니다.

---

## 5. 데이터 흐름

```
자연어 요청
   │
   ▼
cronjob_tool("create")  ──►  create_job()  ──►  parse_schedule()  ──►  JobStore.add()
                                                       │
                                              next_run_at 계산
                                                       │
   ┌───────────────────────────────────────────────────┘
   ▼
Scheduler.tick(now)                       ← now를 명시적으로 주입 (실시간 sleep 없음)
   │
   ├─ due_jobs(now)        : next_run_at <= now ? (grace / stale fast-forward 포함)
   ├─ advance_next_run()   : 반복 job의 next_run_at 선행 갱신 (at-most-once)
   ├─ run_agent(job, now)  : AgentRunResult 생성
   ├─ [SILENT] 체크 후 deliver(job, content)  : 플랫폼으로 배달
   └─ mark_job_run()       : last_* 기록, repeat 카운트, one-shot 완료/자동삭제
```

`demo.py`는 08:00~09:30을 15분 간격 가상 시각으로 진행시켜 interval(30분), cron(09:00),
one-shot(08:45 후 자동삭제)이 각각 어떻게 발화하는지 보여줍니다. **실시간 sleep은
전혀 없고**, 모든 시각은 `now` 인자로 주입됩니다.

```bash
python3 demo.py
```

---

## 6. 커스터마이징 · 응용 포인트

### 새 스케줄 스펙 추가
`parse_schedule()`에 분기를 추가하고, `compute_next_run()` / `due_jobs()`가 새 `kind`를
처리하도록 확장하면 됩니다. 예: `"weekday 9am"`, `"last day of month"` 같은 표현.
cron 표현식을 더 풍부하게(요일 이름, `L`/`#` 등) 쓰고 싶으면 `_CronExpr`를 손보거나
원본처럼 `croniter`로 교체하세요.

### 새 delivery 채널 추가
`deliver` 콜러블만 갈아끼우면 끝입니다. `DeliverFn` 시그니처
(`(CronJob, str) -> DeliveryResult`)만 지키면 Slack·이메일·웹훅 등 무엇이든 연결할 수
있어요. `deliver` 문자열 포맷(`"slack:#alerts"` 등)도 자유롭게 정의하면 됩니다.

### 실제 에이전트 연결
`run_agent` 콜러블을 LangChain/LangGraph 에이전트 호출로 바꾸면 됩니다.
`job.prompt`를 입력으로 받아 `AgentRunResult`를 반환하도록만 맞추세요. 응답에서
delivery를 스킵하고 싶을 땐 `final_response`에 `[SILENT]`를 넣으면 됩니다.

### 영속화 백엔드 교체
`JobStore`는 지금 JSON 파일(또는 in-memory)입니다. `add/all/get/resolve_ref/remove/update`
인터페이스만 유지하면 SQLite·Redis·DB로 바꿔도 스케줄러는 그대로 동작합니다.

### 운영 시 주의 (원본 교훈)
- 반복 job은 `next_run_at`을 못 구해도 **절대 조용히 비활성화하지 않습니다** —
  `state="error"`로 남겨 사용자가 알아채게 합니다.
- 실행 전에 `next_run_at`을 미리 당겨 두는 at-most-once 패턴은 크래시 루프에서
  같은 job이 수십 번 터지는 사고를 막아 줍니다. 이 순서를 함부로 바꾸지 마세요.
```
