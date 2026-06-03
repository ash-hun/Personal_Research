# Hermes Agent 기능 분석 — 모듈 인덱스

> [Nous Research의 **Hermes Agent**](https://github.com/NousResearch/hermes-agent)을 기능 단위로 해부해,
> 각 기능을 **표준 라이브러리만으로 돌아가는 self-contained Python 미러**로 정리한 작업물입니다.
> 원본은 Python 파일만 2,046개·`agent/` 코어만 6만 LOC라 그대로 읽기 어렵습니다.
> 여기서는 기능마다 **진짜 제어 흐름과 I/O 데이터 모델만 충실히 보존**하고 곁가지는 덜어냈습니다.
>
> **목표:** 개별 모듈 폴더 하나만 읽어도 "아, Hermes의 A 기능이 이렇게 구현됐고,
> input/output 인터페이스는 이렇구나"를 알 수 있고, 나중에 **응용·커스텀**할 수 있게 하는 것.

원본 레퍼런스: [`../_reference/hermes-agent/`](../_reference/hermes-agent/)

---

## Hermes Agent이란

Nous Research의 **self-improving AI agent**. 다른 에이전트와 구별되는 핵심은 **닫힌 학습 루프(closed learning loop)** 입니다 — 경험에서 스킬을 만들고, 사용 중 스킬을 개선하고, 지식을 저장하도록 스스로를 nudge하고, 과거 대화를 검색하고, 세션을 넘나들며 사용자 모델을 깊게 쌓습니다. $5 VPS부터 GPU 클러스터·서버리스까지 어디서든 돌고, Telegram 등 메신저에서 클라우드 VM 위 에이전트와 대화할 수 있습니다.

---

## 모듈 지도

각 폴더 = `<feature>.py`(미러) + `demo.py`(실행 시연) + `README.md`(구현방식·I/O·커스터마이징) + `__init__.py`.
모든 미러는 **stdlib only**, 모든 demo는 `python3 demo.py`로 외부 API 없이 즉시 실행됩니다.

### A. 코어 에이전트 엔진 — "에이전트를 에이전트로 만드는" 런타임

| 모듈 | 한 줄 요약 | 핵심 원본 소스 |
|------|-----------|----------------|
| [conversation_loop](conversation_loop/) | user 메시지 → assistant 턴 변환의 심장. 모델 호출→tool 실행→결과 재투입 루프 + iteration budget | `agent/conversation_loop.py` |
| [tool_system](tool_system/) | 도구 정의·등록·디스패치·실행·guardrail/approval·결과 분류 (행동 레이어) | `agent/tool_executor.py`, `toolsets.py`, `tools/approval.py` |
| [provider_adapters](provider_adapters/) | 모든 프로바이더를 하나의 정규화 인터페이스로. "use any model" 추상화 | `agent/anthropic_adapter.py`, `chat_completion_helpers.py`, `gemini_native_adapter.py` |
| [prompt_system](prompt_system/) | 시스템 프롬프트 조립 + 섹션 구성/순서 + prompt caching(캐시 경계) | `agent/system_prompt.py`, `prompt_builder.py`, `prompt_caching.py` |
| [context_compression](context_compression/) | 토큰 예산 측정→압축 트리거→오래된 턴 요약. 긴 세션을 살려두는 기능 | `agent/context_engine.py`, `context_compressor.py`, `trajectory_compressor.py` |
| [memory_system](memory_system/) | 에이전트가 직접 큐레이션하는 장기 메모리 + nudge (**learning loop의 절반**) | `agent/memory_manager.py`, `curator.py`, `tools/memory_tool.py` |

### B. 시스템 기능 — Hermes를 "플랫폼"으로 만드는 레이어

| 모듈 | 한 줄 요약 | 핵심 원본 소스 |
|------|-----------|----------------|
| [skills_system](skills_system/) | SKILL.md 발견·주입·슬래시커맨드 + 경험에서 스킬 **자동 생성** (**learning loop의 나머지 절반**) | `agent/skill_utils.py`, `skill_preprocessing.py`, `tools/skill_manager_tool.py` |
| [subagent_delegation](subagent_delegation/) | 격리된 서브에이전트 스폰으로 병렬 작업 + mixture-of-agents | `tools/delegate_tool.py`, `mixture_of_agents_tool.py` |
| [code_execution_rpc](code_execution_rpc/) | 에이전트가 짠 Python이 RPC로 도구를 호출 → 다단계 파이프라인을 1턴으로 압축 | `tools/code_execution_tool.py`, `managed_tool_gateway.py` |
| [execution_environments](execution_environments/) | local/Docker/SSH/Singularity/Modal/Daytona를 하나의 `Environment` 인터페이스로 추상화 | `tools/environments/{base,local,docker,ssh,modal}.py` |
| [cron_scheduling](cron_scheduling/) | 자연어 작업의 무인 예약 실행 + 임의 플랫폼으로 결과 delivery | `cron/scheduler.py`, `cron/jobs.py`, `tools/cronjob_tools.py` |
| [gateway_platforms](gateway_platforms/) | 단일 게이트웨이로 Telegram/Discord/Slack/… 멀티플랫폼 + 크로스플랫폼 연속성 | `gateway/{run,session,delivery,platform_registry}.py` |

---

## Loop

Hermes의 정체성인 self-improving은 두 모듈이 맞물려 돕니다:

```
경험(대화 transcript)
   │
   ├─▶ memory_system : 사용자/지식을 메모리로 저장 → curator가 주기적으로 정제 → 다음 세션에 주입
   │
   └─▶ skills_system : 복잡한 작업 완료 후 SKILL.md 자동 생성 → 사용 중 개선 → curator가 통합
```

`memory_system`은 "**누구와 무엇을**" 기억하고, `skills_system`은 "**어떻게 하는지**"를 기억합니다.
두 모듈의 README를 함께 읽으면 Hermes가 왜 "self-improving"인지 이해됩니다.