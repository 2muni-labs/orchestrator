# 백엔드 환경과 운영

[English](runtime.md) | 한국어

기준: `b757a619`, 2026-11-18. 이 문서는 저장소가 구현한 환경을 설명한다.
현재 호스트의 설치·인증·서비스 활성화 상태를 확인한 결과는 아니다.

## 기술 구성

| 구성 요소 | 현재 역할 |
| --- | --- |
| Python >=3.14 | CLI와 비즈니스 로직; 런타임 외부 Python 의존성 없음 |
| uv / uv_build | lock 기반 환경 준비와 패키지 빌드 |
| sqlite3 | 로컬 상태 DB, WAL, migration, 트랜잭션 |
| Git / gh | worktree, 변경 검증, PR·CI·Projects API |
| Claude Code / Codex CLI | 메인 세션 및 작업/리뷰 실행 |
| tmux | 대상별 메인 세션과 관측 패널, pane 수명 확인 |
| macOS sandbox-exec | 게이트 검증 명령의 OS 격리 |
| launchd | 세션 복구, 주기 doctor와 GC |
| ntfy(선택) | 알림; 기본 설정은 none |
| pytest / ruff / jsonschema | 개발·테스트 의존성 |

실제 운영은 macOS의 sandbox-exec/launchd에 의존한다. Python 버전 조건을
만족한다고 Linux/Windows에서도 같은 운영 경로가 지원되는 것은 아니다.
`config/pins.toml`의 Claude 2.1.319, Codex 0.160.0, tmux 3.8, macOS 15.8.1은
측정 기준이다. 최신 권장 버전이나 설치된 버전을 의미하지 않는다.
외부 도구를 바꾸면 명시된 blocking/warn 항목을 다시 측정한다.

## 설정과 디렉터리

`ORCH_HUB`는 정책과 targets.toml이 있는 hub의 절대 경로다. `ORCH_TARGET`은
메인 세션의 대상이다. 기본 런타임은 `~/.orch`, 별도 `ORCH_HOME`은 config의
경로 규칙을 따른다. 실제 설정·키·알림 topic을 공개 문서에 복사하지 않는다.

| 경로 | 소유 내용 |
| --- | --- |
| hub/targets.toml | 대상 clone, GitHub 저장소, default branch, setup/test/lint, shared/safety paths |
| ~/.orch/config | terminal, limits, agents 절대 경로, 알림, doctor, gate 설정 |
| ~/.orch/state.db | 대상 전체 task/run/lock/event/tracking 상태 |
| ~/.orch/<target>/tasks | 확정 지시서 |
| results / reviews / logs | 실행 결과, 리뷰·triage·diff, 실행별 로그 |
| worktrees | task별 Git worktree |
| TODO.md / terminal.json | 생성 상태 뷰 / 소유 pane 정보 |
| plans | 메인 소유 계획과 우선도·외부 의존관계 기록 |
| github-tracking.json | 추적 대응표 import 입력; reviewed seed와 런타임 사본 구분 |
| ~/.orch/service | 서비스 로그 |

[config.py](../../src/orch/config.py)의 기본값은 total=3, lane_b=2, lane_a=2,
예약 TTL 10분, 보존 30일, disk warn 10GB/fail 2GB다. 메인 정책의 실제 동시 작업
권장값(1~2)과 코드 상한은 다르다. 기능 계획에 적힌 B 상한 3을 설치된 기본값으로
간주하지 않는다. agent 실행 경로는 절대 경로로 지정하여 cwd의 shim 영향을 피한다.

## 설치와 시작 절차

1. 승인된 release/tag와 호스트 요구조건을 확인한다. 소스의 버전 문자열만으로
   배포 완료를 판단하지 않는다.
2. `uv tool install`로 승인된 버전을 설치하고 `orch --version`, `orch --help`를 확인한다.
   이미 설치된 운영 버전의 교체는 아래 업그레이드 절차를 따른다.
3. hub의 targets.toml 또는 `orch target add`로 대상 경로와 GitHub 저장소,
   setup/test/lint 명령을 등록한다. 실제 테스트는 네트워크 없이 실행 가능해야 한다.
4. 로컬 config에 Claude/Codex 바이너리의 절대 경로와 필요한 설정을 넣는다.
   지정된 GitHub App bot 인증을 준비하며 토큰을 task/로그/문서에 넣지 않는다.
5. `ORCH_HUB`가 설정된 셸에서 `orch open <target>`을 실행한다.
   launcher는 config/main.md, settings와 agent 정의를 주입한다.
6. `orch status`, `orch doctor`로 상태와 차단 조건을 확인한다. doctor는 점검 기록,
   pause, 정리/중단을 수행할 수 있으므로 단순 읽기 명령으로 취급하지 않는다.
7. 필요할 때 `orch service install`로 launchd를 등록하고 재부팅·로그인 후 동작을
   실제로 검증한다. 등록 성공만으로 재부팅 인수 완료가 되지는 않는다.

## 서비스와 상태 관찰

현재 [service.py](../../src/orch/service.py)는 아래 세 서비스를 만든다.

| Label | 실행 시점 | 명령 |
| --- | --- | --- |
| orch.session | RunAtLoad | orch service restore |
| orch.doctor | 600초 간격 | orch doctor |
| orch.gc | 매일 04:00 | orch gc |

launchd에는 절대 실행 경로, 제한된 PATH와 ORCH_HUB를 전달한다. 현 기준에
orch.track, orch.workspace, orch.fulltest 서비스는 없다. doctor는 디스크,
주요 외부 도구/연결과 메인 pane 등의 건강을 검사하며 일부 실패에 pause를 건다.
watch/TODO는 관측 뷰이므로 이를 편집해 상태를 복구하지 않는다.

## 검증과 업그레이드

개발 환경은 `uv sync --frozen`으로 먼저 준비한다. `.venv`는 sandbox 안에서
설치하지 않는다. 허브 변경의 전체 검증은 설치된 orch로 다음을 실행한다.

```text
orch sandbox run --dir <worktree> -- .venv/bin/python -m pytest -rs
orch sandbox run --dir <worktree> -- .venv/bin/python -m ruff check .
orch sandbox run --dir <worktree> -- .venv/bin/python -m ruff format --check .
```

각 JSON의 ok와 log_path를 확인한다. 허용된 skip은
`tests/test_sandbox_real.py`, `tests/integration/test_sandbox_gate.py`의
`already inside a sandbox, where sandbox-exec cannot start`뿐이다.
그 두 파일만 같은 worktree에서 sandbox 밖으로 실행하여 0 skipped를 확인한다.
실패한 sandbox 명령을 일반 실행으로 바꿔 통과시키지 않는다.

버전 교체 전에는 실행 중인 task가 없는지 확인하고 SQLite `.backup`으로
`~/.orch/state.db`를 백업한다. WAL DB 파일 하나만 복사하면 최근 변경이 누락될
수 있다. 사용자 승인된 tag로 재설치하고 상태·명령·서비스를 확인한다.
새 DB 스키마를 예전 바이너리가 읽지 못할 수 있으므로 rollback은 바이너리뿐
아니라 호환 DB와 그 이후 실행 기록의 처리 계획까지 포함해야 한다.
정책 변경은 hub 갱신과 새 `orch open` 세션이 필요하다.

## 장애 조사와 기록

task id와 run 번호로 상태, 결과 JSON, 리뷰, 게이트 결과, 로그, PR head를 연결해
조사한다. 외부에 공유할 때는 짧은 요약과 검증된 링크만 남긴다. 인증 실패와
사용량 제한, 테스트 실패, report_missing은 구분한다. blocked/orphaned 복구는
정책의 승인 경계를 따르고 DB를 수동 갱신하지 않는다.

GitHub 상태가 뒤처지면 로컬 실행 증거를 먼저 확인한다. 추적 CLI/서비스가 아직
연결되지 않은 버전에서는 실행 가능한 명령인 것처럼 `orch track`을 안내하지 않는다.
설치된 버전에서 지원 여부를 확인하고 미반영 작업을 계획의 pending sync로 남긴다.
