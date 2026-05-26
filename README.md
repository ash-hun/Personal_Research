# my-research

> Personal AI/ML research repository — experiment-per-folder, fully isolated Python environments.

## Overview

AI/ML 리서치를 실험 단위로 격리해 관리하는 개인 레포지터리입니다.  
각 실험은 독립된 `uv` 가상환경과 Jupyter 커널을 가지며, 메인 실행 주체는 Jupyter Notebook(`.ipynb`)입니다.

## Repository Structure

```
my_research/
├── _template/                  # 새 실험의 기준 템플릿 (수정 금지)
│   ├── run.ipynb               # 메인 노트북 템플릿
│   ├── dataset/                # 데이터 파일
│   ├── module/                 # 재사용 Python 모듈
│   ├── script/                 # 유틸리티 스크립트
│   └── README.md
├── <experiment-name>/          # 실험 폴더 (kebab-case)
│   ├── run.ipynb
│   ├── dataset/
│   ├── module/
│   ├── script/
│   ├── pyproject.toml          # uv 관리 의존성
│   └── README.md
└── .claude/
    └── commands/
        └── init_research.md    # 실험 초기화 커맨드
```

## Prerequisites

- [uv](https://docs.astral.sh/uv/) — Python 패키지 및 가상환경 관리
- [Jupyter Lab](https://jupyterlab.readthedocs.io/) — 노트북 실행 환경
- [Claude Code](https://claude.ai/code) — `/init_research` 커맨드 사용 시 필요

## Getting Started

### 새 실험 시작

```bash
/init_research <experiment-name>
```

커맨드 하나로 아래 과정이 자동 처리됩니다:

1. `_template/` 복사 → `<experiment-name>/` 생성
2. `uv init` 으로 Python 프로젝트 초기화
3. `uv add --dev ipykernel` 설치
4. Jupyter 커널 등록

> 폴더명 규칙: 소문자 영문, 숫자, 하이픈(`-`)만 허용. 예: `rag-chunking-001`

### 실험 실행

```bash
cd <experiment-name>

# Jupyter Lab 실행
uv run jupyter lab

# CLI 실행 (결과 저장)
uv run jupyter nbconvert --to notebook --execute run.ipynb --output run_executed.ipynb
```

### 패키지 관리

```bash
# 실험 폴더 안에서
uv add <package>          # 런타임 의존성
uv add --dev <package>    # 개발 의존성
```

## Experiments

| Folder | Description |
|--------|-------------|
| [`deepagent-builtin-tools-analysis`](deepagent-builtin-tools-analysis/) | deepagent 구조 기반 내장 툴 호출 방식 및 응답 context 분석 |

## Conventions

- 노트북은 `Kernel > Restart & Run All` 기준으로 **위에서 아래로 순서 실행 가능**해야 한다.
- 재사용 로직은 `module/*.py` 로 분리 후 노트북에서 `import`.
- API 키 등 시크릿은 `.env` + `python-dotenv` 사용 (하드코딩 금지).
- 실험 결과는 노트북 셀 출력으로 보존한다.

## License

Private — All rights reserved.
