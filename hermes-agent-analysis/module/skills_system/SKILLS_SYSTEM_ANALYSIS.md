# Skills System — Hermes Agent 정밀 분석

> 원본: `_reference/hermes-agent/` (Nous Research, **read-only**)
> 분석 대상: 에이전트가 `SKILL.md`를 **발견 → 파싱 → (프롬프트로) 노출 → 슬래시 커맨드로 호출 → 본문 전처리/주입 → 스스로 생성**하는 전체 제어 흐름.

이 문서 하나만 읽어도 "Hermes의 스킬 기능이 어떤 순서로, 어떤 조건에서 동작하고, 입력 타입 → 출력 타입이 무엇인지"를 알 수 있도록 자기완결적으로 작성했다. 모든 주장은 원본 `파일:라인`으로 뒷받침한다.

---

## 1. 개요

**스킬(skill)** 은 디스크의 `SKILL.md` 한 파일이다. 상단에 YAML frontmatter(`name`, `description`, `platforms`, `triggers`, `metadata.hermes.*`), 그 아래 마크다운 본문(절차/지식)이 온다. Hermes는 이 파일들을 **재사용 가능한 절차적 메모리(procedural memory)** 로 다룬다.

핵심은 **루프가 닫혀 있다**는 점이다:

1. **발견·노출** — 시작 시 모든 스킬 디렉터리를 walk 해 `name: description` 인덱스를 만들고, 이를 시스템 프롬프트 `## Skills (mandatory)` 블록에 넣는다. 모델은 관련 스킬을 `skill_view(name)`으로 **스스로 로드**한다 (`agent/prompt_builder.py:1040`, `1236`).
2. **호출·주입** — 사용자가 `/skill-name`을 입력하거나 모델이 스킬을 부르면, 본문을 전처리(템플릿 치환·인라인 셸)해 활성화 안내문과 함께 메시지로 끼워 넣는다 (`agent/skill_commands.py:428`, `160`).
3. **생성(self-improving)** — 작업을 끝낸 에이전트가 `skill_manage(action="create")`로 새 `SKILL.md`를 작성·검증·기록한다. 백그라운드 self-improvement 리뷰가 만든 것이면 `agent_created`로 표시되어 Curator의 관리 대상이 된다 (`tools/skill_manager_tool.py:816`, `883`).

진입점은 셋으로 나뉜다:

| 흐름 | 진입점 |
|---|---|
| 발견 → 프롬프트 노출 | `build_skills_system_prompt()` `agent/prompt_builder.py:1040` |
| 슬래시 커맨드 호출 → 주입 | `build_skill_invocation_message()` `agent/skill_commands.py:428` |
| 생성(self-improving) | `skill_manage()` `tools/skill_manager_tool.py:816` |

세 흐름 모두 동일한 저수준 유틸(`agent/skill_utils.py`: 발견·파싱·플랫폼 게이팅)을 공유한다.

---

## 2. 입출력 인터페이스

### 2.1 입력 — `SKILL.md` 포맷 (agentskills.io 호환)

```markdown
---
name: pdf-extract                         # 필수
description: Extract text/tables from PDFs # 필수
platforms: [linux, macos, windows]        # 생략 시 모든 OS
triggers: [pdf, parse pdf, pdf table]     # (선택) 자동 활성화 힌트
metadata:
  hermes:
    requires_tools: [terminal]            # (선택) 조건부 노출
    config:                               # (선택) 스킬이 요구하는 config.yaml 키
      - key: wiki.path
        description: Wiki directory
        default: "~/wiki"
---

# PDF Extraction
스킬 폴더 경로: ${HERMES_SKILL_DIR}        # 주입 시 절대경로로 치환
오늘 날짜: !`date +%Y-%m-%d`               # (opt-in) 인라인 셸 확장
```

- 디스크 구조: `<skills_dir>/<category?>/<skill-name>/SKILL.md`
- 스킬 디렉터리: 로컬 `~/.hermes/skills/` 먼저, 그다음 `skills.external_dirs` (config 순서). `get_all_skills_dirs()` `agent/skill_utils.py:327`.

### 2.2 주요 함수 시그니처 (입력 타입 → 출력 타입)

| 함수 | 입력 | 출력 | 위치 |
|---|---|---|---|
| `parse_frontmatter(content)` | `str` (파일 전체) | `(dict 메타, str 본문)` | `skill_utils.py:88` |
| `skill_matches_platform(fm)` | `dict` frontmatter | `bool` (현재 OS 호환?) | `skill_utils.py:128` |
| `iter_skill_index_files(dir, name)` | `Path, str` | `Iterator[Path]` (정렬·가지치기됨) | `skill_utils.py:532` |
| `build_skills_system_prompt(tools, toolsets)` | `set\|None, set\|None` | `str` (프롬프트 블록) | `prompt_builder.py:1040` |
| `scan_skill_commands()` | — | `Dict["/slug", info]` | `skill_commands.py:263` |
| `resolve_skill_command_key(cmd)` | `str` (`git_bisect`) | `"/git-bisect" \| None` | `skill_commands.py:409` |
| `build_skill_invocation_message(key, instr, …)` | `str, str` | `str` (주입 메시지) `\| None` | `skill_commands.py:428` |
| `skill_manage(action, name, content, …)` | `str, str, str` | `str` (JSON 결과) | `skill_manager_tool.py:816` |

### 2.3 부수효과(side effects)

- **읽기**: 모든 흐름이 `SKILL.md`/`config.yaml`/`DESCRIPTION.md`를 디스크에서 읽는다.
- **쓰기**: 생성 경로만 디스크에 쓴다 — `SKILL.md` 원자적 기록(`_atomic_write_text` `skill_manager_tool.py:510`), 스냅샷 캐시 `~/.hermes/.skills_prompt_snapshot.json` (`prompt_builder.py:937`), usage 텔레메트리(`tools/skill_usage.py`).
- **네트워크/프로세스**: 인라인 셸이 켜졌을 때만 `bash -c`로 서브프로세스 실행(`skill_preprocessing.py:70`).

---

## 3. 핵심 소스 파일 매핑

| 파일 | 역할 |
|---|---|
| `agent/skill_utils.py` | 저수준 공유 유틸: 발견(walk+가지치기), frontmatter 파싱, 플랫폼/disabled 게이팅, 외부 디렉터리·config 변수 해석 |
| `agent/skill_preprocessing.py` | 본문 전처리: `${HERMES_*}` 템플릿 치환, `` !`cmd` `` 인라인 셸 확장(+타임아웃·출력 캡) |
| `agent/prompt_builder.py` | 발견 결과를 시스템 프롬프트 `## Skills` 인덱스로 조립(2단 캐시), self-improving 지시문 |
| `agent/skill_commands.py` | 슬래시 커맨드 스캔/정규화/해석, 호출 시 본문을 활성화 메시지로 빌드 |
| `tools/skill_manager_tool.py` | `skill_manage` 툴: create/edit/patch/delete/write_file, frontmatter 검증, 원자적 기록, 보안 스캔, provenance 표시 |
| `tools/skill_usage.py` | usage 텔레메트리: `bump_use`/`bump_patch`/`mark_agent_created`/`forget` (Curator 라이프사이클용) |
| `tools/skill_provenance.py` | `is_background_review()` — 생성이 사용자 지시인지 백그라운드 자기개선인지 구분 |

---

## 4. step별 동작 흐름

### 흐름 A — 발견 → 시스템 프롬프트 노출

진입점: `build_skills_system_prompt()` `agent/prompt_builder.py:1040`.

**Step A0. 스킬 디렉터리 결정**
로컬 `get_skills_dir()`를 0번, 외부 디렉터리들을 그 뒤로. `external_dirs = get_all_skills_dirs()[1:]` (`prompt_builder.py:1099` 부근, `skill_utils.py:327`).

**Step A1. 캐시 조회 (2단)**
① in-process LRU (skills_dir, tools, toolsets) 키 → ② 디스크 스냅샷 `.skills_prompt_snapshot.json`. 스냅샷은 `(mtime_ns, size)` manifest로 검증 — 파일이 하나라도 바뀌면 무효화(`prompt_builder.py:919`, `932`). 둘 다 적중하면 전체 walk를 건너뛴다.

**Step A2. (캐시 미스) 풀 스캔 — cold path** `prompt_builder.py:1121`
`iter_skill_index_files(skills_dir, "SKILL.md")`로 모든 `SKILL.md`를 순회. 각 파일에 대해:
- `_parse_skill_file()` `prompt_builder.py:990`: 한 번 읽어 `(호환여부, frontmatter, description)` 반환. **에러 시 `(True, {}, "")` → 보여주는 쪽으로 fail-safe** (`:1007`).
- `skill_matches_platform()`로 OS 비호환이면 제외(`:1003`).
- `frontmatter_name` 또는 `skill_name`이 `disabled`에 있으면 제외(`:1130`).
- `_skill_should_show()` 조건부 게이팅(`:1132`): `fallback_for_*`(주 도구가 있으면 숨김) / `requires_*`(필수 도구가 없으면 숨김) (`prompt_builder.py:1024`).
- 통과분을 `skills_by_category[category]`에 `(name, desc)`로 누적(`:1138`).
- 카테고리별 `DESCRIPTION.md`도 읽어 `category_descriptions`에 채움(`:1143`).
- 스캔 결과를 스냅샷으로 기록(`_write_skills_snapshot` `:1156`) → 다음 호출 가속.

**Step A2'. (캐시 적중) warm path** `prompt_builder.py:1100`
스냅샷의 `skills` 엔트리를 같은 게이팅(platform/disabled/should_show)으로 필터해 `skills_by_category` 재구성.

**Step A3. 외부 디렉터리 병합** `prompt_builder.py:1167`
로컬에서 이미 본 이름을 `seen_skill_names`에 모으고, 외부 디렉터리를 스캔하되 **이름 충돌 시 로컬 우선**(외부는 skip) (`:1183`). 외부는 스냅샷 캐싱 안 함(읽기전용·소규모).

**Step A4. 인덱스 텍스트 조립 + 반환** `prompt_builder.py:1214`
- 스킬이 하나도 없으면 `result = ""` (`:1215`).
- 카테고리 정렬 → 카테고리별로 스킬을 `name`순 정렬·중복 제거 → 줄 생성:
  - `    - {name}: {desc}` 또는 desc 없으면 `    - {name}` (`:1231`, `:1233`).
- 헤더 `## Skills (mandatory)` + "관련되면 반드시 `skill_view(name)`으로 로드하라"는 강제 지시문을 붙여 반환(`:1236`).

> **귀결**: 출력은 "카테고리 → name: description" **인덱스 문자열**일 뿐, 본문은 들어가지 않는다. 실제 활성화는 **모델이 `skill_view`를 호출**해야 일어난다. (= "매칭"의 실체는 substring 알고리즘이 아니라 **LLM 라우팅**이다. ⚠️ 미러의 `match_skills` substring은 학습용 단순화.)

---

### 흐름 B — 슬래시 커맨드 호출 → 본문 주입

**Step B0. 커맨드 맵 스캔** `scan_skill_commands()` `agent/skill_commands.py:263`
모든 `SKILL.md`를 순회하며 `/slug → {name, description, skill_md_path, skill_dir}` 맵 구축:
- `.git/.github/.hub/.archive` 경로 추가 가지치기(`:286`).
- `skill_matches_platform` 비호환 / `disabled` / 이미 본 `name`이면 skip(`:292`, `:295`, `:298`).
- description 없으면 본문 첫 비주석 줄을 80자로 잘라 대체(`:302`).
- **슬러그 정규화**(`:311`): 소문자화 → 공백·`_`를 `-`로 → `[^a-z0-9-]` 제거 → 다중 하이픈 축약 → 양끝 `-` strip. 빈 슬러그면 skip(`:314`). 결과를 `_skill_commands["/slug"]`에 저장(`:316`).

`get_skill_commands()` `:329`는 캐시가 비었거나 **플랫폼 스코프가 바뀌면** 재스캔(텔레그램/디스코드 동시 서빙 시 platform_disabled 뷰 분리, `:336`).

**Step B1. 사용자 입력 → 커맨드 키 해석** `resolve_skill_command_key()` `:409`
`/git_bisect` 입력이 와도 `_`→`-`로 바꿔 `/git-bisect` 키를 찾는다(텔레그램은 하이픈 불가라 언더스코어로 등록되어 되돌아오는 경우 호환) (`:424`). 없으면 `None`.

**Step B2. 페이로드 로드** `build_skill_invocation_message()` `:428` → `_load_skill_payload()` `:53`
- 커맨드 맵에서 `skill_info` 조회, 없으면 `None` 반환(early return, `:445`).
- `_load_skill_payload`가 `skill_view(...preprocess=False)`로 원본을 JSON 로드. **절대경로는 trusted root(`SKILLS_DIR`+외부) 기준 lexical 상대화를 먼저** 시도 — symlink resolve를 먼저 하면 신뢰경로가 임의경로로 바뀌어 거부되기 때문(`:78`). 실패 시 `None`.

**Step B3. usage 기록** `:455`
`bump_use(skill_name)` — Curator 라이프사이클용. **best-effort**, 실패해도 호출 진행(`:459`).

**Step B4. 활성화 메시지 빌드** `_build_skill_message()` `:160`
순서가 중요하다:
1. **전처리 먼저**(`:176`): config의 `template_vars`(기본 **on**)이면 `${HERMES_SKILL_DIR/SESSION_ID}` 치환, `inline_shell`(기본 **off**)이면 `` !`cmd` `` 확장. 다운스트림 블록이 확장된 본문을 보도록 가장 먼저 한다.
2. `parts = [activation_note, "", content]` (`:183`).
3. `skill_dir` 있으면 `[Skill directory: ...]` + 상대경로 해석 안내(`:187`).
4. `_inject_skill_config()` — frontmatter가 요구한 config 값을 `[Skill config: ...]`로 주입(`:197`, `:121`).
5. setup note(`setup_skipped`/`gateway_setup_hint`/`setup_note`) 분기 추가(`:199`).
6. supporting files: `linked_files` 또는 `references/templates/scripts/assets/`를 훑어 목록화(`:221`).
7. `user_instruction`/`runtime_note` 있으면 덧붙임(`:252`).
8. `"\n".join(parts)` 반환(`:260`).

활성화 안내문(`:461`): `[IMPORTANT: The user has invoked the "<name>" skill … full skill content is loaded below.]`

> **귀결**: 출력은 모델이 곧바로 따를 수 있는 **완결된 메시지 문자열**(본문+경로+config+supporting). 실패 경로는 모두 `None`으로 귀결되어 호출자가 "스킬 없음"을 처리한다.

---

### 흐름 C — 생성(self-improving)

진입점: `skill_manage(action, name, content, …)` `tools/skill_manager_tool.py:816`. JSON 문자열 반환.

**Step C0. 액션 디스패치** `:833`
`create / edit / patch / delete / write_file / remove_file`로 분기. 알 수 없는 액션은 에러 dict(`:865`). `create`는 `content` 필수(`:834`).

**Step C1. `_create_skill()` 검증 체인** `:476`
순서대로 검증, 하나라도 실패하면 `{success:False, error}` early return:
1. `_validate_name` (`:479`)
2. `_validate_category` (`:483`)
3. `_validate_frontmatter` (`:488` → `:217`): `---`로 시작·닫힘, YAML 매핑, `name`·`description` 필수, description 길이, 본문 존재 검사.
4. `_validate_content_size` (`:492`)
5. **이름 충돌 검사** — 전 디렉터리에서 동명 스킬 있으면 거부(`:497`).

**Step C2. 디스크 기록 + 보안 스캔(롤백)** `:504`
- `skill_dir.mkdir(parents=True, exist_ok=True)` (`:506`).
- `_atomic_write_text(skill_dir/"SKILL.md", content)` — 원자적 기록(`:510`).
- `_security_scan_skill()`(`:512`): 위험 발견 시 **`shutil.rmtree`로 방금 만든 디렉터리 롤백** 후 에러 반환(`:514`). 정상이면 `{success, path, skill_md, hint}` 반환(`:518`).

**Step C3. 성공 후처리** `skill_manage` `:868`
`result.success`일 때만:
- `clear_skills_system_prompt_cache(clear_snapshot=True)` — 인덱스 캐시·스냅샷 무효화 → 다음 프롬프트 빌드에서 새 스킬 반영(`:870`).
- **provenance 분기**(`:883`):
  - `action == "create"` **그리고** `is_background_review()`가 참일 때만 `mark_agent_created(name)` → `created_by="agent"`로 표시, Curator 관리 대상이 됨(`skill_usage.py:592`).
    - ⚠️ **포그라운드 사용자 지시 create는 표시하지 않는다** — 사용자 소유 스킬을 Curator가 건드리지 못하게(`:876` 주석).
  - `patch/edit/write_file/remove_file` → `bump_patch(name)` (`:886`).
  - `delete` → `forget(name)` (`:888`).
- 모든 텔레메트리는 best-effort `try/except`(`:890`).

**Step C4. 루프 종료 → 재발견**
새 `SKILL.md`는 캐시 무효화 덕에 **다음 `build_skills_system_prompt()`에서 일반 스킬로 재인덱싱**되고, 다음 요청부터 모델이 `skill_view`로 다시 로드할 수 있다. self-improving 루프가 닫힌다.

> self-improving을 촉발하는 지시문은 시스템 프롬프트에 박혀 있다(`prompt_builder.py:151`):
> *"If you've discovered a new way to do something … save it as a skill with the skill tool."*

---

## 5. 상태 전이 다이어그램

```
                    ┌──────────────────────────────────────────────┐
                    │            DISK: <skills_dir>/**/SKILL.md     │
                    └──────────────────────────────────────────────┘
                         │ iter_skill_index_files (walk + prune)
                         │ skill_utils.py:532 / EXCLUDED_DIRS:27
        ┌────────────────┼─────────────────────────────┐
        ▼ (A) 발견·노출                                  ▼ (B) 슬래시 호출
  build_skills_system_prompt:1040                 user types /git_bisect
        │ cache hit? ──yes──▶ warm path:1100              │
        │ no                                              ▼ resolve_skill_command_key:409
        ▼ cold scan:1121                            ('_'→'-')  ──not found──▶ None
   _parse_skill_file:990 (err→show, fail-safe)           │ found "/git-bisect"
        │ platform/disabled/should_show gating            ▼ build_skill_invocation_message:428
        ▼ write snapshot:1156                       _load_skill_payload:53 ──fail──▶ None
   merge external (local wins):1167                       │ ok
        ▼ assemble index:1214                        bump_use:455 (best-effort)
   "## Skills (mandatory)\n  cat:\n    - name: desc"      ▼ _build_skill_message:160
        │  (:1236)                                   preprocess(template/shell):176
        ▼                                                 │ + dir + config + setup + files
   SYSTEM PROMPT ──▶ 모델이 skill_view(name) 자율 로드 ◀───┘ activation message 문자열
        │
        │ 작업 수행 중 "새 절차 발견"  (prompt_builder.py:151)
        ▼ (C) 생성
   skill_manage(action="create"):816
        │ dispatch:833 ──unknown──▶ {error}
        ▼ _create_skill:476
   validate(name/category/frontmatter/size):479-492 ──fail──▶ {success:False}
        │ name collision:497 ──exists──▶ {success:False}
        ▼ mkdir + atomic write:506-510
   _security_scan_skill:512 ──block──▶ rmtree 롤백 + {error}   (:514)
        │ ok → {success, path}
        ▼ success 후처리:868
   clear cache(snapshot):870 ──▶ 다음 (A)에서 재발견 ──┐
        │ is_background_review()? ──yes──▶ mark_agent_created:885 → Curator 관리
        │ no (user create) → 표시 안 함 (사용자 소유)     │
        └────────────────────────────────────────────────┘  루프 닫힘
```

---

## 6. 외부 서브시스템 경계

진입점이 위임하지만 이 분석에서 깊이 들어가지 않은 곳(잘라내지 않고 명시):

| 경계 | 무엇을 하는가 | 위치 |
|---|---|---|
| `skill_view()` | 슬래시 호출 시 스킬을 trusted root 안에서 안전하게 읽어 JSON 페이로드로 반환(경로 traversal 방어 포함) | `tools/skills_tool.py` (호출: `skill_commands.py:60`, `93`) |
| `_security_scan_skill` → `scan_skill` | 생성된 스킬 디렉터리를 정적 스캔해 위험 패턴이면 차단(기본 off, `skills.guard_agent_created`로 opt-in) | `tools/skills_guard.py` (호출: `skill_manager_tool.py:78`, `512`) |
| `skill_usage` 텔레메트리 | `bump_use/bump_patch/mark_agent_created/forget` — JSON 레코드에 use_count·last_used·created_by 기록 | `tools/skill_usage.py:569`, `581`, `592`, `624` |
| `is_background_review` | 현재 실행이 백그라운드 self-improvement 리뷰 fork인지 판정 → provenance 게이팅 | `tools/skill_provenance.py:75` |
| **Curator** | `agent_created` 스킬들을 주기적으로 통합/아카이브(좁은 스킬 → 큰 스킬). 라이프사이클 관리 | `agent/curator.py` (1843 lines) |
| `hermes_cli.config` / `hermes_constants` | `config.yaml` 로드, `get_skills_dir()`·`get_config_path()` 등 경로 해석 | `skill_preprocessing.py:27`, `skill_utils.py:15` |
| 인라인 셸 실행 | `bash -c`로 `` !`cmd` `` 실행(타임아웃·4000자 출력 캡, 실패는 마커로 격리) | `skill_preprocessing.py:63`–`98` |

---

## 7. 검증 매트릭스

3·4절에서 인용한 라인을 원본과 재대조한 결과. ✅ = 정확히 일치, ⚠️ = 단순화/주의.

| 단계 | 주장 | 원본 위치 | 결과 |
|---|---|---|---|
| A2 | 풀 스캔 cold path 진입 | `prompt_builder.py:1121` | ✅ |
| A2 | `_parse_skill_file` 에러 시 fail-safe(show) | `prompt_builder.py:990`, `:1007` | ✅ |
| A2 | 조건부 노출 게이팅 `_skill_should_show` | `prompt_builder.py:1024` | ✅ |
| A3 | 외부 디렉터리 이름 충돌 시 로컬 우선 | `prompt_builder.py:1167`, `:1183` | ✅ |
| A4 | 인덱스 줄 `    - name: desc` | `prompt_builder.py:1231`, `:1233` | ✅ |
| A4 | 헤더 `## Skills (mandatory)` | `prompt_builder.py:1236` | ✅ |
| A4 | 활성화는 모델의 `skill_view` 호출(=LLM 라우팅) | `prompt_builder.py:1236`–`1244` | ⚠️ 미러 `match_skills` substring은 학습용 단순화 |
| B0 | walk + `.git/.hub` 등 추가 가지치기 | `skill_commands.py:263`, `:286` | ✅ |
| B0 | 슬러그 정규화(소문자·`_`→`-`·invalid 제거) | `skill_commands.py:311`–`313` | ✅ |
| B1 | `_`↔`-` 호환 해석 | `skill_commands.py:409`, `:424` | ✅ |
| B2 | trusted-root lexical 상대화 우선(symlink) | `skill_commands.py:53`, `:78` | ✅ |
| B3 | `bump_use` best-effort | `skill_commands.py:455`–`459` | ✅ |
| B4 | 전처리를 가장 먼저(template on/shell off 기본값) | `skill_commands.py:176`–`181` / `skill_preprocessing.py:134`,`136` | ✅ |
| B4 | 활성화 안내문 문구 | `skill_commands.py:461` | ✅ |
| 발견 공통 | EXCLUDED_SKILL_DIRS 가지치기 | `skill_utils.py:27`–`44`, `:540` | ✅ |
| 발견 공통 | `parse_frontmatter` `---` 경계 + YAML/폴백 | `skill_utils.py:88`–`122` | ✅ |
| 발견 공통 | `skill_matches_platform`(Termux 특례 포함) | `skill_utils.py:128`–`169` | ✅ |
| C0 | 액션 디스패치/unknown 처리 | `skill_manager_tool.py:833`, `:865` | ✅ |
| C1 | 검증 체인 + 이름 충돌 거부 | `skill_manager_tool.py:476`–`502` | ✅ |
| C1 | `_validate_frontmatter` 필수 필드 검사 | `skill_manager_tool.py:217`–`253` | ✅ |
| C2 | 원자적 기록 + 보안스캔 rmtree 롤백 | `skill_manager_tool.py:510`–`516` | ✅ |
| C3 | 성공 시 캐시·스냅샷 무효화 | `skill_manager_tool.py:870`–`871` | ✅ |
| C3 | `mark_agent_created`는 background review에서만 | `skill_manager_tool.py:883`–`885` | ✅ ⚠️ README는 "create 시 항상 표시"로 단순화 |
| C 트리거 | "save it as a skill" 지시문 | `prompt_builder.py:151` | ✅ (README의 `:150`은 1줄 오차) |

---

### 분석 요약 한 줄

> Hermes의 스킬 시스템은 **(A) 시스템 프롬프트 인덱스로 노출 → 모델이 `skill_view`로 자율 로드, (B) `/slug` 슬래시 커맨드로 본문을 전처리·주입, (C) `skill_manage(create)`로 스스로 생성**하는 세 흐름이 동일한 발견·파싱·게이팅 유틸을 공유하며, **캐시 무효화 → 재발견**으로 self-improving 루프를 닫는 구조다.
