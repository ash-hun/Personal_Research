# execution_environments — Hermes Agent의 실행 백엔드 추상화

Nous Research의 **Hermes Agent**가 가진 핵심 설계 중 하나인 *pluggable execution
environments*를 stdlib만으로 재현한 학습용 미러입니다. 노트북에서 바로 읽고 돌려보며
"에이전트가 어떻게 노트북·컨테이너·원격 서버·서버리스를 똑같은 방식으로 다루는지"를
체감하는 것이 목표예요.

```bash
python3 demo.py   # 외부 의존성 없음. LocalEnvironment만 실제 subprocess 사용
```

---

## 1. 기능 개요 — 왜 백엔드 추상화가 중요한가

에이전트의 도구(shell tool, file tool)는 결국 두 가지 동작으로 수렴합니다.

- **명령을 실행한다** (`execute`)
- **파일을 읽고 쓴다** (`read_file` / `write_file`)

문제는 이 동작이 *어디서* 일어나느냐가 상황마다 다르다는 점이에요. 사용자의 노트북일
수도, 격리된 Docker 컨테이너일 수도, 원격 SSH 빌드 서버일 수도, 혹은 Modal 같은
서버리스 샌드박스일 수도 있습니다.

Hermes는 이걸 **하나의 `Environment` 인터페이스**로 통일합니다. 에이전트 도구 코드는
오직 `env.execute(...)`, `env.write_file(...)`, `env.read_file(...)`만 호출하고,
그 아래에 어떤 백엔드가 꽂혀 있는지는 전혀 신경 쓰지 않아요. 그래서 `LocalEnvironment`를
`ModalEnvironment`로 바꿔도 **도구 코드는 단 한 줄도 바뀌지 않습니다.**

원격/서버리스 백엔드는 두 가지 숨은 과제를 추가로 떠안는데, 인터페이스가 이것마저
가려줍니다.

- **File sync**: 원격 샌드박스에서는 호스트 파일시스템이 보이지 않으므로, 명령 실행
  전에 바뀐 파일을 업로드하고(종료 시 다시 받아옴) 동기화해야 합니다.
- **Serverless 지속성 (hibernate/wake)**: Modal 샌드박스는 일회성입니다. 종료 시
  파일시스템을 **스냅샷**으로 떠 두고, 다음 세션에서 그 스냅샷을 **복원**해서 마치
  계속 살아있던 머신처럼 보이게 합니다.

---

## 2. Hermes 실제 구현 방식

### Environment 인터페이스 (`base.py :: BaseEnvironment`)

추상 베이스 클래스가 **공통 실행 흐름**을 소유하고, 서브클래스는 두 군데만 구현합니다.

- 서브클래스 구현: `_run_bash(cmd)` (실제로 명령을 어디서 띄울지), `cleanup()` (자원 해제)
- 베이스 제공: `init_session()`(세션 1회 부트스트랩 → 환경변수/함수/별칭 스냅샷),
  `execute()`(통합 호출 경로), CWD 추적, 인터럽트/타임아웃 처리

`execute()`의 흐름은 백엔드와 무관하게 동일합니다.

```
execute(command, cwd)
  └─ _before_execute()        # 원격 백엔드는 여기서 file sync 플러시
  └─ _wrap_command(cmd, cwd)  # cd + 스냅샷 re-source + CWD 마커 삽입
  └─ _run_bash(wrapped)       # 백엔드별 실제 실행 (subprocess/docker exec/ssh/SDK)
```

### 각 백엔드의 차이

| 백엔드 | 실행 매체 | file sync | 지속성 | 비고 |
|--------|-----------|-----------|--------|------|
| **Local** (`local.py`) | `subprocess` (`bash -c`) | 불필요 | — | 호스트 FS가 곧 환경 |
| **Docker** (`docker.py`) | `docker exec` | **불필요** | 컨테이너 수명 | 호스트 워크스페이스를 bind-mount → 파일이 컨테이너에 그대로 보임 |
| **SSH** (`ssh.py`) | `ssh ... bash -c` | **필요** | 호스트 수명 | scp로 업로드, 종료 시 `sync_back` |
| **Modal** (`modal.py`) | Modal SDK `sandbox.exec` | **필요** | **스냅샷** | cleanup 때 `snapshot_filesystem`, 재시작 때 복원 = hibernate/wake |
| Singularity (`singularity.py`) | overlay 컨테이너 | 불필요 | overlay | bind-mount 계열 |
| Daytona (`daytona.py`) | Daytona SDK | 필요 | workspace | 원격 계열 |

여기서 가장 중요한 갈림길은 **bind-mount 계열(Docker/Singularity/Local)** 과
**원격 전송 계열(SSH/Modal/Daytona)** 의 구분입니다. 전자는 호스트 FS를 직접 보므로
file sync가 아예 없고, 후자만 `FileSyncManager`를 답니다.

### File sync (`file_sync.py :: FileSyncManager`)

원격 백엔드는 전송 콜백(`upload_fn`, `delete_fn`, `get_files_fn`)을 주입해
`FileSyncManager`를 만듭니다. 매니저는 다음을 책임집니다.

- `(mtime, size)` 지문 기반 **변경 감지** — 안 바뀐 파일은 건너뜀
- 삭제 추적 — 더 이상 호스트에 없는 파일은 원격에서도 삭제
- **rate limiting** — `sync_interval`마다 1회만 실제 전송
- **트랜잭션 롤백** — 전송 중 하나라도 실패하면 상태를 되돌려 다음 사이클에 전부 재시도
- **sync_back** — 종료 시 원격 `.hermes/`를 tar로 받아 SHA-256 비교 후 바뀐 것만 반영

---

## 3. 핵심 소스 파일 매핑

| 이 미러 | Hermes 원본 (`tools/environments/`) |
|---------|--------------------------------------|
| `Environment` (ABC) | `base.py :: BaseEnvironment` — **THE 핵심 파일** |
| `LocalEnvironment` | `local.py` (실제 subprocess) |
| `DockerEnvironment` | `docker.py` (bind-mount, sync 없음) |
| `SSHEnvironment` | `ssh.py` (원격, FileSyncManager 사용) |
| `ModalEnvironment` | `modal.py` + `managed_modal.py` (서버리스 hibernate/wake) |
| `FileSyncManager` | `file_sync.py` |
| `create_environment` | `terminal_tool._create_environment` (팩토리) |
| `_SNAPSHOT_STORE` | `modal.py :: modal_snapshots.json` (스냅샷 저장소) |

> 미러는 stdlib 전용이라 Docker/SSH/Modal 백엔드는 **충실한 페이크**입니다. 분리된
> 원격 파일시스템(`_FakeRemoteFS`)과 remote-exec / hibernate-wake 라이프사이클을
> 흉내 내, 실제 도구 없이도 데이터 흐름을 눈으로 확인할 수 있게 했어요. `LocalEnvironment`
> 만 진짜로 `subprocess`로 명령을 실행합니다.

---

## 4. I/O 인터페이스

타입 주석이 달린 `@dataclass`로 입출력을 고정했습니다 (Hermes는 dict
`{"output", "returncode"}`를 주고받음).

```python
@dataclass
class ExecResult:
    output: str
    returncode: int
    @property
    def ok(self) -> bool: ...

@dataclass
class ReadResult:
    content: str
    returncode: int = 0

@dataclass
class WriteResult:
    path: str
    bytes_written: int
    returncode: int = 0
```

핵심 시그니처:

```python
class Environment(ABC):
    needs_file_sync: bool = False   # 원격이면 True
    serverless: bool = False        # Modal이면 True (hibernate/wake)

    def execute(self, command: str, cwd: str = "", *, timeout: int | None = None) -> ExecResult: ...
    def write_file(self, path: str, content: str) -> WriteResult: ...   # heredoc 경유
    def read_file(self, path: str) -> ReadResult: ...                   # cat 경유

    # 라이프사이클
    def start(self) -> None: ...        # = init_session (세션 부트스트랩)
    def stop(self) -> None: ...         # = cleanup
    @abstractmethod
    def _run_bash(self, cmd_string: str, *, timeout: int = 120) -> ExecResult: ...
    @abstractmethod
    def cleanup(self) -> None: ...

class ModalEnvironment(Environment):
    def hibernate(self) -> str: ...     # 파일시스템 스냅샷 → 저장, snapshot_id 반환
```

`write_file` / `read_file`이 별도 전송 채널이 아니라 **`execute()`를 통해** (heredoc
쓰기 / `cat` 읽기) 구현된다는 점이 포인트입니다. 덕분에 파일 연산도 모든 백엔드에서
자동으로 동일하게 동작해요. (Hermes는 `tools/file_operations.py`에서 활성 Environment로
라우팅합니다.)

---

## 5. 데이터 흐름

```
에이전트 도구 코드
   │  env.execute("echo hi") / env.write_file(...) / env.read_file(...)
   ▼
Environment.execute()                       ← 단일 진입점, 백엔드 무관
   │
   ├─ _before_execute()                      ← [원격만] FileSyncManager.sync()
   │      └─ 변경된 호스트 파일 업로드 / 삭제 동기화
   ├─ _wrap_command(cmd, cwd)                ← cd + 스냅샷 + CWD 마커
   └─ _run_bash(wrapped) ──► 백엔드별 실제 실행
          Local : subprocess(bash -c)
          Docker: docker exec   (bind-mount → FS 공유)
          SSH   : ssh host bash -c
          Modal : sandbox.exec  (SDK)
   ▼
ExecResult(output, returncode)              ← 항상 동일한 타입으로 복귀
```

서버리스 hibernate/wake (Modal):

```
세션 A:  ModalEnvironment(task_id="job-42")
            start → 파일 작업 → cleanup
                                  └─ hibernate(): FS 스냅샷 → _SNAPSHOT_STORE["job-42"]
세션 B:  ModalEnvironment(task_id="job-42")
            __init__: 스냅샷 발견 → FS 복원 (restored_from_snapshot=True)
            → 세션 A가 쓴 파일이 그대로 보임
```

---

## 6. 커스터마이징 · 응용 포인트 — 새 백엔드 추가하기

새로운 실행 백엔드(예: Kubernetes Pod, WASM 런타임, 사내 원격 박스)를 붙이는 절차는
간단합니다.

1. **`Environment`를 상속**하고 두 메서드만 구현합니다.
   - `_run_bash(cmd_string, *, timeout)` → 해당 매체에서 명령을 띄우고 `ExecResult` 반환
   - `cleanup()` → 컨테이너/연결/인스턴스 해제
2. **원격이라면** `needs_file_sync = True`로 두고, 생성자에서 전송 콜백
   (`upload_fn` / `delete_fn` / `get_files_fn`)으로 `FileSyncManager`를 만든 뒤
   `_before_execute()`에서 `self._sync_manager.sync()`를 호출합니다.
3. **서버리스라면** `serverless = True`로 두고 `cleanup()`에서 스냅샷을 저장,
   `__init__`에서 task_id로 스냅샷을 복원하는 hibernate/wake 패턴을 따릅니다
   (`ModalEnvironment` 참고).
4. **`BACKENDS` 레지스트리에 등록**하면 `create_environment("my-backend")`로 선택됩니다.
   (Hermes 실제로는 `terminal_tool._create_environment`가 `TERMINAL_ENV` 설정을 보고 고름)

이렇게만 하면 기존 에이전트 도구 코드는 **전혀 손대지 않고** 새 백엔드 위에서 그대로
돌아갑니다. 그게 바로 이 추상화가 주는 가장 큰 선물이에요.

---

## 파일 구성

```
execution_environments/
├── execution_environments.py   # 인터페이스 + 4개 백엔드 + FileSyncManager (소스 인용 포함)
├── demo.py                      # python3 demo.py — 4개 데모 시나리오
├── __init__.py                  # 재노출
└── README.md                    # (이 문서)
```
