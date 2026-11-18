# orchestrator

[English](README.md) | 한국어

`orch`는 Claude Code 메인 에이전트가 작업을 분해하고, Codex/Claude를 독립 Git
worktree에서 실행하여 테스트·검증·리뷰를 거쳐 GitHub PR로 전달하는 전역 CLI다.
이 저장소는 여러 대상 저장소를 관리하는 hub이며 Python 런타임 의존성은 표준
라이브러리뿐이다. 에이전트는 PR을 머지하지 않는다.

## 무엇을 제공하는가

- TOML front matter와 Markdown 본문으로 작성하는 작업 지시서.
- task 의존성, 파일 범위 잠금, 실행 슬롯과 예약 만료를 관리하는 scheduler.
- 실행별 로그·결과 보고, timeout/사용량 제한 감지, 재시도·차단·복구 절차.
- 변경 범위(G1), 테스트(G2), lint(G3), 교차 리뷰와 PR 갱신·머지 대기.
- SQLite 실행 기록, 자동 생성 TODO, tmux 메인 세션·상태 패널.
- macOS sandbox 검증, 훅·명령 가드, 민감 정보 제거, doctor/launchd 운영.

**구현 기준:** `1c191614`(2026-11-18), `pyproject.toml` 버전 `0.15.0`.
GitHub tracking 저장소·동기화 엔진·CLI는 구현됐으며 서비스 연결은 남아 있다.
웹 작업공간, 우선도 기반 자동 스케줄링은 아직 구현되지 않았다.
현재 설치된 orch와 이 소스 기준은 별도로 확인해야 한다.

## 문서 탐색

[전체 문서 목차](docs/README.ko.md)에서 설계 원본과 ADR까지 연결한다.

| 알고 싶은 내용 | 문서 |
| --- | --- |
| 실제 구현 기능과 명령 | [기능·CLI](docs/reference/features.ko.md) |
| 모듈 구성과 실행 흐름 | [아키텍처](docs/reference/architecture.ko.md) |
| 설치 전제·설정·DB·서비스·복구 | [백엔드 환경과 운영](docs/operations/runtime.ko.md) |
| 먼저 할 프로젝트와 의존관계 관리 | [우선도 운영안](docs/operations/work-priority.ko.md) |
| 개발 순서와 선행 조건 | [구축 절차](docs/design/구축%20절차.md) |
| 요구사항과 설계 결정 | [요건 정의](docs/design/요건%20정의.md), [ADR](docs/decisions/README.md) |

## 시작하기

운영 환경은 macOS, Python 3.14+, uv, Git, GitHub CLI, tmux와 인증된 에이전트
바이너리를 전제로 한다. GitHub는 지정된 App bot을 사용한다.
[환경·설정 절차](docs/operations/runtime.ko.md)를 먼저 읽고 대상 저장소와 바이너리
경로를 등록한다. 승인된 버전을 설치한 뒤 셸에서 `ORCH_HUB`를 hub의 절대 경로로
설정하고 `orch open <target>`으로 메인 세션을 시작한다.

작업은 [계획 템플릿](templates/work-plan.md)과 [태스크 템플릿](templates/task.md)을
사용한다. 메인이 선행 조건과 우선도를 검토하고 승인된 작은 묶음을 실행한다.
`orch --help`와 `orch --version`은 명령·설치 버전 확인용이다.

## 개발과 검증

`uv sync --frozen`으로 개발 환경을 준비한다. 테스트·ruff는 개발 의존성이다.
[CLAUDE.md](CLAUDE.md)의 harness 검증과 [메인 절차](config/main.md)를 따른다.
전체 테스트와 lint는 설치된 `orch sandbox run`으로 실행하며 실제 sandbox를
시작해야 하는 지정 테스트 두 파일은 별도 절차로 검증한다.

`src/orch/`는 실행 코드, `tests/`는 검증, `schemas/`는 출력 계약,
`config/`는 주입 정책, `templates/`는 지시서 형식이다.
런타임 DB·로그·비밀은 저장소에 커밋하지 않는다. 변경된 정책은 머지된 hub에서
새 메인 세션을 열어야 적용되며, 코드 변경은 승인된 설치가 필요하다.
