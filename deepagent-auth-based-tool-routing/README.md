# DeepAgent + auth.md 기반 Tool 접근 권한 실험 보고서

## 개요

`auth.md` 프로토콜(WorkOS, 2026)의 scope 시스템을 활용하여 `deepagents` 라이브러리(`create_deep_agent`)에서 **사용자 권한에 따라 다른 tool을 사용하게 할 수 있는지** 검증한다. 단일 tool 레벨 권한 제어(Exp 1)에서 출발하여, skill 레이어와 tool 레이어를 분리한 두 레이어 아키텍처(Exp 2)로 확장한다.

---

## 기술 스택

| 라이브러리 | 역할 |
|---|---|
| `deepagents >= 0.6.3` | LangGraph 기반 agent harness (`create_deep_agent`) |
| `langchain-anthropic >= 1.4.3` | LLM 연동 |
| `langgraph >= 1.2.1` | 상태 그래프 기반 agent 실행 |
| `langchain-community >= 0.4.2` | LangChain 커뮤니티 도구 |
| Model | `anthropic:claude-haiku-4-5-20251001` |

### auth.md 프로토콜

```
401 응답
  └─> WWW-Authenticate 헤더 (resource_metadata URL)
       └─> /.well-known/oauth-protected-resource
            └─> /.well-known/oauth-authorization-server
                 └─> agent_auth 블록 (register_uri, scopes 등)
```

YAML frontmatter + Markdown 본문 형식으로 서비스 루트에 배포되며,
커스텀 scope(`analytics:read`, `billing:read`, `model:invoke` 등)를 frontmatter에 추가하여 확장한다.

---

## 실험 1: Tool 레벨 권한 라우팅 (`run.ipynb`)

### 실험 설계

**대상**: 8개 tool, 5개 역할

| Tool | 분류 | Required Scope |
|---|---|---|
| `query_internal_db` | 내부 | `read:internal` |
| `write_internal_db` | 내부 | `write:internal` |
| `read_internal_file` | 내부 | `read:internal` |
| `web_search` | 외부 | `read:external` |
| `send_notification` | 외부 | `write:external` |
| `admin_system_config` | 관리자 | `admin:all` |
| `get_analytics_report` | 커스텀 | `analytics:read` |
| `invoke_ai_model` | 커스텀 | `model:invoke` |

**역할별 scope 설계**:

| 역할 | 보유 scope |
|---|---|
| admin | 전체 8개 (커스텀 포함) |
| developer | `read:internal`, `write:internal`, `read:external`, `model:invoke` |
| analyst | `read:internal`, `read:external`, `analytics:read` |
| viewer | `read:internal`, `read:external` |
| guest | 없음 |

### 두 가지 접근법 비교

#### Approach A: 정적 권한 필터링 (Static)

agent 초기화 시 1회 scope를 검사하여 허용된 tool만 `create_deep_agent`에 주입한다.

```
사용자 role → auth.md roles[role] → scope 추출
  → ALL_TOOLS 필터링 → create_deep_agent(tools=allowed_only)
  → LLM은 허용된 tool만 인식 (미허용 tool 정보 노출 차단)
```

#### Approach B: 동적 권한 미들웨어 (Dynamic)

전체 tool을 클로저로 래핑하여 실행 시점에 scope를 확인한다.

```
원본 @tool.func 추출
  → @wraps 클로저에 auth 체크 주입
  → StructuredTool.from_function(args_schema=원본 유지)
  → create_deep_agent(tools=all_wrapped)
  → 호출 시 has_scope() → 통과/AUTH DENIED
```

> **구현 이슈**: `BaseTool` 서브클래싱 시 deepagents + Pydantic v2 제네릭 재귀 문제 발생.
> `StructuredTool.from_function` + `@wraps` 클로저로 회피.

#### 비교표

| 항목 | Static | Dynamic |
|---|---|---|
| 필터 시점 | Agent init 1회 | Tool 호출마다 |
| LLM tool 노출 | 허용 tool만 **(보안 강함)** | 전체 tool 노출 |
| Scope 업그레이드 | 재초기화 필요 | 즉시 반영 **(강점)** |
| Token 효율 | 높음 **(강점)** | 낮음 |
| 구현 복잡도 | 낮음 | 중간 |
| auth.md 플로우 | Agent Verified | User Claimed **(강점)** |
| 커스텀 scope | 가능 | 가능 |
| 추천 상황 | 역할 고정, 배치, 보안 강조 | SaaS, 실시간 권한 변경 |

### Scope 업그레이드 (User Claimed 플로우)

Dynamic Auth의 핵심 시나리오 — 동일 agent 인스턴스에서 OTP 인증 후 즉시 반영:

```
viewer로 시작 → send_notification 호출 → AUTH DENIED
  → POST /agent/auth/claim         (OTP 이메일 발송)
  → POST /agent/auth/claim/complete (OTP 검증)
  → auth_ctx.upgrade_scope("write:external")
  → 동일 agent 재시도 → 성공
```

Static Auth는 이 시나리오를 지원할 수 없다.

### Pass Rate 결과 (계산 기반)

테스트 매트릭스: 5개 역할 × 8개 쿼리

| 역할 | Tool 가용률 | Query 통과율 |
|---|---|---|
| admin | 100% (8/8) | 100% (8/8) |
| developer | 50% (4/8) | 50% (4/8) |
| analyst | 38% (3/8) | 38% (3/8) |
| viewer | 25% (2/8) | 25% (2/8) |
| guest | 0% (0/8) | 0% (0/8) |

---

## 실험 2: Two-Layer 권한 아키텍처 (`skills_auth_routing.ipynb`)

### 아키텍처

```
사용자
  ↓
auth.md (단일 권한 소스)
  ├── skill:* 스코프 → skills.md 접근 게이팅 (Layer 1)
  └── read:*/write:* 스코프 → 스킬 내부 Tool 실행 게이팅 (Layer 2)
  ↓
선택적 skills.md 접근 (Layer 1 통과한 스킬만 로드)
  ↓
Skill Layer (복합 워크플로우)
  ↓
Tool Layer (원자적 API/DB 접근)
```

skills.md는 독립적인 권한 시스템이 아니라 auth.md scope에 의존하는 **레지스트리**다.

### 스킬 레지스트리 (skills.md)

| 스킬 | Layer 1 게이트 (auth.md) | Layer 2 필요 tool scope |
|---|---|---|
| `research_skill` | `skill:research` | `read:external` |
| `data_analysis_skill` | `skill:data_analysis` | `read:internal`, `read:external` |
| `notification_skill` | `skill:notification` | `write:external` |
| `reporting_skill` | `skill:reporting` | `read:internal`, `read:external`, `write:external` |
| `code_review_skill` | `skill:code_review` | `read:internal` |

### 역할 설계 (핵심: analyst_restricted)

| 역할 | skill 스코프 | tool 스코프 |
|---|---|---|
| admin | 전체 5개 | 전체 |
| analyst | research, data_analysis, reporting | `read:internal`, `read:external` |
| **analyst_restricted** | research, data_analysis, reporting | `read:external` **(read:internal 없음)** |
| developer | research, notification, code_review | `read:internal`, `read:external`, `write:external` |
| viewer | research | `read:external` |
| guest | 없음 | 없음 |

### 핵심 발견: 부분 실패(Partial Fail)

`analyst_restricted`는 두 레이어 구조에서만 관찰 가능한 **부분 실패** 케이스를 만들어낸다.

```
data_analysis_skill 실행 요청
  → Layer 1: skill:data_analysis 보유 → skills.md 접근 허용
  → Layer 2: read:internal 없음 → DB 조회 tool 차단
  → 결과: 스킬은 로드되지만 실행 중 부분 실패
```

단일 레이어(Tool 실험)에서는 완전 성공(1) 또는 완전 실패(0)만 존재한다.
두 레이어 구조에서는 `0 < 결과 < 1`인 부분 실패 케이스가 등장한다.

### Pass Rate 결과 (계산 기반)

테스트 매트릭스: 6개 역할 × 5개 쿼리

| 역할 | L1 통과율(skill) | L2 통과율(tool) | E2E 통과율 | 부분실패율 |
|---|---|---|---|---|
| admin | 100% (5/5) | 100% (5/5) | 100% (5/5) | 0% |
| analyst | 60% (3/5) | 100% (3/3) | 60% (3/5) | 0% |
| **analyst_restricted** | **60% (3/5)** | **33% (1/3)** | **20% (1/5)** | **40%** |
| developer | 60% (3/5) | 100% (3/3) | 60% (3/5) | 0% |
| viewer | 20% (1/5) | 100% (1/1) | 20% (1/5) | 0% |
| guest | 0% (0/5) | N/A | 0% (0/5) | 0% |

### 시각화

`pass_rate_analysis.png` — 3개 Figure:
- **Fig 1**: 역할별 L1 / E2E / 부분실패율 Grouped Bar
- **Fig 2**: 역할별 쿼리 분류 Stacked Bar (E2E 성공 / 부분 실패 / L1 차단)
- **Fig 3**: Static(계산) vs Dynamic(실행) E2E 비교

---

## 종합 결론

### 검증 결과

1. **두 접근법 모두 `create_deep_agent`에서 정상 동작** — auth.md scope 체계를 DeepAgents에 직접 적용 가능하다.
2. **커스텀 scope**(`analytics:read`, `model:invoke` 등)도 동일 패턴으로 확장된다.
3. **`analyst_restricted` 케이스가 두 레이어 구조의 가치를 증명**한다 — 단일 레이어에서는 관찰 불가능한 "부분 실패율"이라는 지표가 생긴다. 이 값이 0이 아니면 auth.md role 설계 검토가 필요하다는 신호다.

### 권장 아키텍처

| 상황 | 권장 |
|---|---|
| 역할 고정 + 보안 민감 | **Static** (미허용 tool 자체를 LLM에서 숨김) |
| 세션 중 scope 변경 필요 | **Dynamic** (User Claimed OTP 플로우 연동) |
| 일반 SaaS / Multi-tenant | **Hybrid** — 기본 tool Static + 업그레이드 가능 tool Dynamic |
| 복합 워크플로우 시스템 | **Two-Layer** — skill 레이어 + tool 레이어 분리 |

### 실제 서비스 연동 패턴

```python
# 원격 auth.md fetch + agent 생성
auth_md  = fetch_auth_md_sync("https://my-service.com")
auth_ctx = AuthContext(user_id, role, auth_md=auth_md)
agent    = create_static_agent(auth_ctx)   # 또는 dynamic / two-layer
```

### 추가 탐색 주제

- **audit log**: `(user_id, tool_name, scope, timestamp)` 기록으로 권한 감사 추적
- **DeepAgents 내장 tool에 auth 적용**: `write_file`, `execute` 등에 scope 추가
- **MCP tool 통합**: MCP 서버 tool에도 `AuthWrappedTool` 패턴 적용
- **Hybrid 구현**: 기본 scope Static 필터 + 업그레이드 scope Dynamic 래핑 혼용

---

## 파일 구조

```
deepagent-auth-based-tool-routing/
├── run.ipynb                    # 실험 1: Tool 레벨 권한 라우팅 (Static/Dynamic)
├── skills_auth_routing.ipynb    # 실험 2: Two-Layer 권한 아키텍처 (Skill + Tool)
├── pass_rate_analysis.png       # 시각화 결과 (Three Figure)
├── script/
│   ├── gen_notebook.py          # run.ipynb 생성 스크립트
│   └── gen_skills_notebook.py   # skills_auth_routing.ipynb 생성 스크립트
└── pyproject.toml               # uv 의존성 관리
```


## 레퍼런스
- https://workos.com/blog/agent-registration-with-auth-md
- https://www.marktechpost.com/2026/05/25/workos-releases-auth-md-an-open-agent-registration-protocol-built-on-oauth-standards/
- https://digitalbourgeois.tistory.com/m/3131
