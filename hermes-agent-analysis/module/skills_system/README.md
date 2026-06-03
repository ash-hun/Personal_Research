# skills_system — Hermes Agent의 스킬 시스템 미러

Nous Research의 **Hermes Agent**가 가진 스킬(skills) 기능을, 외부 의존성 없이
순수 표준 라이브러리만으로 한 파일에 압축해 재현한 학습용 미러입니다.
`agentskills.io` 표준의 `SKILL.md` 포맷과 호환됩니다.

원본 코드를 직접 읽지 않고도 "스킬이 어떻게 발견·파싱·주입되고, 슬래시 커맨드로
노출되며, **에이전트가 스스로 새 스킬을 만들어내는가**"를 한눈에 이해하고
커스터마이징할 수 있도록 만들었습니다.

---

## 기능 개요 — 왜 self-improving이 핵심인가

대부분의 에이전트 프레임워크에서 "스킬/툴"은 사람이 미리 짜 넣는 정적인 자산입니다.
Hermes의 스킬 시스템이 특별한 이유는 **루프가 닫혀 있다**는 점입니다.

```
   사용자 요청 ──▶ 트리거 매칭 ──▶ 스킬 주입 ──▶ 작업 수행
                                                  │
        새 스킬이 다음 요청부터 매칭됨 ◀── SKILL.md 생성 ◀── "방금 배운 것" 정리
                                                  ▲
                                       (백그라운드 Curator가 주기적으로 통합/정리)
```

에이전트가 어려운 작업을 끝내면, 시스템 프롬프트가 이렇게 지시합니다
(`agent/prompt_builder.py:150`):

> "If you've discovered a new way to do something, solved a problem that could be
> necessary later, **save it as a skill** with the skill tool."

즉 작업 경험(transcript)이 곧바로 재사용 가능한 절차(SKILL.md)로 결정화되고,
그 스킬은 **다음 요청부터 곧장 매칭되어 다시 쓰입니다**. 시간이 지나면 백그라운드
Curator가 자잘한 스킬들을 클래스 단위의 큰 스킬로 통합합니다. 이것이
self-improving 에이전트의 실체입니다.

---

## hermes 실제 구현 방식

### 1) 발견(discovery)
`~/.hermes/skills/` 와 설정된 외부 디렉터리들을 `os.walk`로 순회하면서 `SKILL.md`
파일을 찾습니다. `.git`, `node_modules`, `.venv`, `__pycache__` 등은 가지치기해서
의존성 트리가 스킬을 몰래 등록하지 못하게 합니다. 로컬 디렉터리를 먼저 보고,
이름이 같으면 먼저 본 쪽이 이깁니다.

### 2) 파싱(parsing)
각 `SKILL.md`는 상단에 `---` 로 감싼 YAML frontmatter(`name`, `description`,
`triggers`, `platforms` …)와 그 아래 마크다운 본문으로 구성됩니다. frontmatter를
파싱해 메타데이터를 뽑고, 현재 OS와 `platforms`가 맞지 않으면 제외합니다.

### 3) 주입(injection / preprocessing)
스킬이 활성화되면 본문에 대해 전처리를 합니다. `${HERMES_SKILL_DIR}` 같은 템플릿
토큰을 치환하고(기본 on), 설정에 따라 `` !`명령` `` 형태의 인라인 셸 스니펫을 실제
출력으로 확장(기본 off, 안전상 opt-in)합니다. 그런 다음 활성화 안내문 + 본문 +
`[Skill directory: ...]` 힌트를 묶어 하나의 프롬프트 블록으로 메시지에 끼워 넣습니다.

### 4) 슬래시 커맨드(slash-command)
스킬 이름을 `/slug`로 정규화해 명령으로 노출합니다. 공백·언더스코어는 하이픈으로
바뀌고, 사용자가 `/git_bisect`라고 쳐도 `/git-bisect`로 해석됩니다(텔레그램 봇
명령 호환). 번들(`skill_bundles.py`)은 여러 스킬을 한 슬래시 커맨드로 묶는 별도
레이어이며, 충돌 시 번들이 우선합니다.

### 5) 생성(creation) — self-improving
`skill_manage(action='create')` 툴이 에이전트가 작성한 SKILL.md 텍스트를 검증
(`_validate_frontmatter`)하고 디스크에 기록한 뒤, 그 스킬을 "에이전트가 만든 것"
으로 표시합니다(`skill_usage.mark_agent_created`). 이 표시가 백그라운드
**Curator**(`agent/curator.py`)의 통합·아카이브 대상 선정 기준이 됩니다.

---

## 핵심 소스 파일 매핑

| 미러(이 폴더)의 요소 | Hermes 원본 |
|---|---|
| `parse_frontmatter`, `skill_matches_platform`, `EXCLUDED_SKILL_DIRS`, `iter_skill_index_files`, `extract_skill_description` | `agent/skill_utils.py` |
| `SkillPreprocessor` (`substitute_template_vars` / `expand_inline_shell` / `preprocess`) | `agent/skill_preprocessing.py` |
| `build_skill_message`, `SkillCommandRegistry` (`scan` / `resolve` / `build_invocation_message`) | `agent/skill_commands.py` |
| (번들 개념 — 본 미러는 단일 스킬에 집중) | `agent/skill_bundles.py` |
| `validate_frontmatter`, `SkillCreator.create_from_transcript` | `tools/skill_manager_tool.py` (`_validate_frontmatter`, `_create_skill`) |
| `Skill.agent_created`, `agent_created_names` | `tools/skill_usage.py` (`mark_agent_created`, `is_agent_created`) |
| self-improving 트리거 문구 / 통합 오케스트레이션 | `agent/prompt_builder.py:150`, `agent/curator.py` |

---

## I/O 인터페이스

### SKILL.md 포맷 (agentskills.io 호환)

```markdown
---
name: pdf-extract
description: "Extract text and tables from PDF files reliably."
triggers: [pdf, extract pdf, parse pdf, pdf table]
platforms: [linux, macos, windows]   # 생략 시 모든 OS
---

# PDF Extraction
## Procedure
1. ...
스킬 폴더 경로: ${HERMES_SKILL_DIR}
```

- 디스크 구조: `<skills_dir>/<category?>/<skill-name>/SKILL.md`
- `name`, `description` 필수. `triggers`는 자동 활성화용 짧은 문구 리스트.

### 주요 시그니처

```python
# 발견·파싱
loader = SkillLoader(skills_dirs=[Path("~/.hermes/skills")], agent_created_names=set())
skills: list[Skill] = loader.load()

# 매칭·주입
matches: list[SkillMatch] = match_skills(query: str, skills)
prompt_block: str = build_injection_prompt(query, skills, SkillPreprocessor())

# 슬래시 커맨드
reg = SkillCommandRegistry(skills, SkillPreprocessor())
reg.resolve("git_bisect")                 # -> "/git-bisect" | None
reg.build_invocation_message("/pdf-extract", user_instruction="invoice.pdf")

# 생성 (self-improving)
creator = SkillCreator(skills_dir, llm=my_llm_callable)   # llm: Callable[[str], str]
created: CreatedSkill = creator.create_from_transcript(
    TaskTranscript(task=..., steps=[...], outcome=..., tags=[...]),
    category="data-science",
)
```

데이터클래스: `Skill`, `SkillMatch`, `TaskTranscript`, `CreatedSkill` — 모두
타입 어노테이션된 `@dataclass`입니다.

---

## 데이터 흐름

```
디렉터리 트리
   │  os.walk + 제외 디렉터리 가지치기
   ▼
SKILL.md ── parse_frontmatter ──▶ Skill(name, description, triggers, body, ...)
   │
   ├─[사용자 쿼리]─ match_skills ──▶ 트리거 점수순 SkillMatch
   │                                   │
   │                                   ▼
   │                        SkillPreprocessor.preprocess (템플릿/인라인셸)
   │                                   │
   │                                   ▼
   │                        build_skill_message ──▶ 프롬프트에 주입
   │
   └─[/slug 입력]─ SkillCommandRegistry.resolve ──▶ build_invocation_message

[작업 완료 transcript]
   │  SkillCreator.build_synthesis_prompt
   ▼
LLMCallable(prompt) ──▶ SKILL.md 텍스트 ── validate_frontmatter ──▶ 디스크 기록
   │                                                                    │
   └────────────────────── agent_created 표시 ◀──────────────────────────┘
                                  │  (다음 load()에서 일반 스킬로 재발견)
                                  ▼
                         다시 매칭·주입 가능 → 루프 종료
```

---

## 커스터마이징·응용 포인트

- **매칭 전략 교체**: `match_skills`는 단순 substring 트리거 매칭입니다. 임베딩
  유사도나 LLM 라우팅으로 바꾸면 더 정교한 자동 활성화가 가능합니다. Hermes 원본은
  스킬 이름/설명을 모델에 노출하고 모델이 직접 고르는 방식도 병행합니다.
- **LLM 주입**: `SkillCreator(llm=...)`의 `llm`은 `Callable[[str], str]`라서
  실제 API 클라이언트(Anthropic/OpenAI 등)나 목(mock)을 그대로 끼울 수 있습니다.
  Curator의 통합 패스도 동일한 보조 모델 indirection을 씁니다.
- **인라인 셸 안전성**: `SkillPreprocessor(inline_shell=...)`는 기본 off입니다.
  신뢰되지 않은 스킬을 다룬다면 켜지 마세요. 켤 때는 타임아웃·출력 캡을 조정하세요.
- **Curator(통합 패스) 추가**: 본 미러는 생성까지만 다룹니다. `agent_created_names`
  를 기준으로 "오래 안 쓰인 좁은 스킬을 큰 스킬로 통합" 하는 주기적 잡을 얹으면
  원본의 라이프사이클 관리까지 재현할 수 있습니다.
- **번들/네임스페이스**: 여러 스킬을 한 명령으로 묶는 번들, 플러그인 스킬을 위한
  `namespace:skill-name` 분리 등은 원본 `skill_bundles.py` / `skill_utils.py`를
  참고해 확장 포인트로 삼을 수 있습니다.

---

## 실행

```bash
python3 demo.py
```

임시 디렉터리에 가짜 SKILL.md 2개를 쓰고 → 로드 → 쿼리로 매칭/주입 →
슬래시 커맨드 등록·호출 → 가짜 transcript로 새 스킬을 합성하고, 스킬 저장소가
진화하는 과정을 출력합니다. 외부 의존성 없이 동작합니다.
