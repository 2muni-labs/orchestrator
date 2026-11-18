# 구현 기능과 CLI

[English](features.md) | 한국어

기준: `1c191614`, 2026-11-18. 코드는 [src/orch](../../src/orch), 명령의 권위 있는
목록은 [cli.py](../../src/orch/cli.py)의 `_parser()`다. 아래는 소스 구현 상태이며
현재 머신에 설치·활성화된 기능의 보증이 아니다.

## 실행 파이프라인

| 기능 | 구현과 동작 | 주요 소스 |
| --- | --- | --- |
| 대상 등록 | repository, default branch, setup/test/lint, 공유 경로 등록 | config.py |
| 지시서 파싱 | TOML 필수 값·경로·레인·모델 검증, Markdown 본문 보존 | taskfile.py |
| 준비 | 기본 브랜치에서 worktree 생성, setup 실행, 공유 경로/테스트 수정 범위 거부 | lifecycle.py |
| 예약 | 선행 task 완료, glob 충돌, 실행 수, pause 검사; 예약 TTL | scheduler.py |
| 실행 | 레인별 argv/env, 실행 로그, 프로세스 수명·timeout, 결과 수집 | runner.py, lanes/ |
| 결과 | JSON 결과 형식 검사, 누락/잘못된 보고서 실패 처리 | results.py |
| 검증 | G1 범위 및 고정 테스트 보호, G2 테스트, G3 lint, 통과 후 commit | gate.py |
| 리뷰 | 결과 검증·저장, triage, Claude 구현의 Codex 리뷰 | review.py, cli.py |
| PR | 기본 브랜치 반영, 검증, push/PR, 충돌·테스트 실패 상태 | prflow.py, github.py |
| 머지 대기 | PR·CI·리뷰 상태를 관찰하고 후속 행동을 반환 | waiter.py |
| 정리/복구 | close/discard/retry/resume/kill, 남은 프로세스·worktree 관리 | lifecycle.py, gc.py |

작업 위험도 L3/L3R 또는 명시적 review는 리뷰를 요구한다. 안전 경로 관련
운영 정책과 코드의 게이트 조건은 별개로 확인해야 한다. PR 생성은 승인이나
머지를 뜻하지 않는다. 사람이 또는 사용자 계정의 지정된 검증 절차가 머지를 맡는다.

## 제공되는 명령

| 명령 | 용도 |
| --- | --- |
| `orch --version`, `orch --help` | 패키지 버전·도움말 |
| `orch target add PATH --name ... --github ... --setup ... --test ... --lint ...` | 대상 등록 |
| `orch open NAME [--no-attach]` | 주입 정책이 있는 메인 tmux 세션과 패널 |
| `orch prepare SPEC` | 지시서와 worktree 준비 |
| `orch dispatch ID`, `orch next` | 한 작업 예약 / queued 작업 재검사 |
| `orch run ID`, `orch gate ID` | 에이전트 실행 / 기계적 게이트 |
| `orch review ID --by codex` | Codex 교차 리뷰 실행 |
| `orch review save ID [--triage FILE]` | 표준 입력의 리뷰 JSON 저장 |
| `orch pr ID [--resolution TEXT]`, `orch update ID` | PR 게시·수정 / 기본 브랜치 갱신 반영 |
| `orch wait ID --merged [--interval N] [--max-wait N]` | 머지·검사·리뷰 결과 대기 |
| `orch close ID`, `orch discard ID` | 완료 확인과 정리 / 폐기 |
| `orch retry ID`, `orch resume ID [--keep]`, `orch kill ID` | 실패 재시도 / 차단 복구 / 중단 |
| `orch status [--human]`, `orch watch [--once] [--panels]` | 상태 출력과 패널 갱신 |
| `orch doctor`, `orch gc [--dry-run]`, `orch notify test` | 건강 점검·보존 정리·알림 확인 |
| `orch service install/uninstall/restore` | launchd 서비스 등록·삭제·세션 복구 |
| `orch sandbox run --dir PATH -- COMMAND ...` | 게이트 프로필로 검증 실행 |
| `orch track import/bind/accept/status/sync` | 대응표 import, task 연결, 인수 확인, 추적 조회·동기화 |
| `orch hook NAME` | 설정에 주입되는 내부 hook 진입점 |

대부분 명령은 JSON으로 결과를 보고한다. 상태 변경 명령은 현재 상태에 따라 거부될
수 있다. `resume`/`kill` 등은 메인 정책의 승인 경계를 따른다. 도움말에 없는 설계
명령을 사용 가능한 CLI로 가정하지 않는다.

## 부분 구현: GitHub 작업 추적

[tracking.py](../../src/orch/tracking.py)는 대응표 검증·DB import, project/record/task/
alias 관계와 sync 상태를 관리한다. [tracksync.py](../../src/orch/tracksync.py)는
동기화 잠금, 원하는 상태 계산, dry-run, GitHub 쓰기와 검증·실패 기록을 구현한다.
[github.py](../../src/orch/github.py)에 issue/Projects 연산이 있다.

- task 소유: prepared→Todo, blocked/conflict/orphaned→Blocked,
  closed→Done, discarded→Cancelled, 그 외→In Progress.
- harness 소유: PR OPEN/MERGED/CLOSED로 상태를 계산한다.
- aggregate/manual 소유: 개별 task 완료만으로 닫지 않으며 acceptance가 필요하다.
- GitHub의 상태 편집이 로컬 task를 dispatch/merge시키지 않는다.

기준 시점에 `orch track import/bind/accept/status/sync` CLI가 구현됐다.
`sync`는 `--dry-run`, `--all`, `--target`을 지원하며 나머지는 `--target`으로 대상을 선택한다.
service에는 아직 `orch.track`이 없다. CLI 구현만으로 상시 동기화가 가동 중이라고
설명하지 않는다. #496–498의 서비스·출시·절차 연결과 설치 버전을 확인한다.

## 미구현 또는 설계 단계

| 항목 | 기준 시점의 경계 |
| --- | --- |
| 작업 우선도 | 메인 정책·Markdown 계획으로 기록; scheduler/DB의 priority 없음 |
| milestone / replaces | 템플릿에 향후 필드 설명은 있으나 현재 task parser가 지원하지 않음 |
| events/show/roadmap/versions/selfcheck/explore | 설계·일부 스키마가 있어도 현재 CLI에는 없음 |
| 웹 작업공간 | 서버/PWA/웹 터미널/공유 레이아웃/기기 입력 소유권 구현 없음 |
| 전용 tmux socket·네트워크 allowlist proxy | 향후 작업공간/강화 설계와 현재 adapter를 구분 |
| 자동 버전 전환 | pins는 측정 기준 자료; 자동 검사·설치 완료의 증거 아님 |

이 문서는 설계를 축소하지 않는다. 계획 중인 기능은
[구축 절차](../design/구축%20절차.md)와 관련 이슈에서 추적한다.
