# orchestrator

`orch`는 메인 에이전트(Claude Code) 하나가 사용자 요청을 작업으로 나누고, 각 작업을 Claude·Codex 서브에이전트에게 격리된 git worktree에서 맡긴 뒤, 기계적인 게이트로 결과를 검증해 PR을 여는 전역 CLI입니다. 에이전트는 머지하지 않습니다. 머지는 사용자 계정의 예약 작업이 머지 정책에 따라 하거나 사용자가 직접 합니다.

이 저장소는 허브입니다. `orch` 패키지, 메인과 서브에이전트에 주입하는 규칙, 설계 문서가 여기에 있습니다.

## 작업 흐름

1. **계획**: 메인이 작업 목록(목적, 우선도, 위험 등급, 레인, 수정 범위, 의존)을 보고하고 사용자 승인을 받습니다.
2. **지시서와 테스트**: 메인이 지시서를 쓰고, worktree에 테스트와 인터페이스 스텁을 먼저 커밋합니다.
3. **실행**: `orch run`이 지시서의 레인으로 서브에이전트를 실행합니다. 서브는 수정 범위 안에서만 구현하고 결과 보고를 남깁니다.
4. **게이트와 리뷰**: `orch gate`가 범위·테스트·검증 명령을 샌드박스에서 확인합니다. 위험한 작업은 교차 리뷰를 받습니다.
5. **PR**: `orch pr`이 기본 브랜치에 rebase하고 테스트를 다시 돌린 뒤 PR을 엽니다. 필수 검사가 통과하면 리뷰 라벨이 붙고, 예약 작업이 리뷰하고 머지합니다.

상태의 원본은 `~/.orch/state.db` 하나입니다. `TODO.md`, 그룹별 계획 파일, GitHub 이슈와 Projects는 모두 여기서 파생합니다.

## 요구 사항

- macOS (게이트 샌드박스에 `sandbox-exec` 사용)
- Python 3.14, [uv](https://docs.astral.sh/uv/)
- Claude Code, Codex CLI, tmux, git, GitHub CLI(`gh`)
- 대상 저장소에 설치된 GitHub App 봇 (push, PR, 이슈에 사용)

## 설치와 시작

```sh
# 태그로 설치. 갱신은 실행 중인 작업이 없을 때 state.db를 백업한 뒤 --reinstall
uv tool install git+https://github.com/2muni-labs/orchestrator@v0.16.0

# 허브 클론 위치를 셸 설정에 지정
export ORCH_HUB=<허브 클론의 절대 경로>

# 대상 저장소를 등록하고 메인 세션을 엶
orch target add <대상 저장소 경로> --name <대상>
orch open <대상>
```

`orch open`은 tmux 세션 `orch-<대상>`을 열고, 대상 저장소 위치에서 메인 Claude Code를 실행하면서 허브의 훅, 메인 규칙, 서브에이전트 정의를 주입합니다.

## 주요 명령

| 명령 | 하는 일 |
| --- | --- |
| `orch prepare` / `dispatch` / `run` | worktree 준비, 실행 슬롯 배분, 서브에이전트 실행 |
| `orch gate` / `review` | 게이트 검증과 교차 리뷰 |
| `orch pr` / `update` / `wait` / `close` | PR 생성과 갱신, 머지 대기, 정리 |
| `orch retry` / `resume` / `discard` / `kill` | 재시도, 재개, 폐기, 중단 |
| `orch status` / `watch` | 작업 상태 보기 |
| `orch track` | GitHub 이슈·Projects 동기화 |
| `orch doctor` / `service` / `gc` | 상시 점검, launchd 에이전트, 정리 |

명령의 기본 출력은 JSON 한 줄입니다(`orch watch`와 `--human` 출력은 사람이 읽는 형식). 전체 명령 목록은 `docs/design/실행 설계.md` 2장에, 명령별 동작은 `docs/design/기술 설계.md` 4장에 있습니다.

## 문서

| 문서 | 내용 |
| --- | --- |
| [`docs/design/`](docs/design/) | 설계의 원본: 요건 정의, 기술 설계, 실행 설계, 보안 설계, 화면 설계, 구축 절차 |
| [`docs/decisions/`](docs/decisions/) | 결정 기록(ADR), 결정 하나에 파일 하나 |
| [`config/main.md`](config/main.md) | 메인 에이전트 규칙과 머지 정책 |
| [`config/subagent.md`](config/subagent.md) | 구현 서브에이전트 규칙 |
| [`AGENTS.md`](AGENTS.md) | 이 저장소에서 일하는 모든 에이전트의 규칙 |
