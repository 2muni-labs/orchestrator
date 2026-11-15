# orch-deck 기획 초안

> **초안입니다.** 이 문서는 논의 중인 내용이며 설계의 근거가 아닙니다. 확정되면 해당 내용을
> `docs/design/`(허브 쪽)과 새 저장소의 설계 문서로 옮기고, 이 파일은 지우거나 결정 기록으로 줄입니다.
> 이름 `orch-deck`은 가칭입니다.

- 작성: 2026-11-15, Phase 4 완료(PR #134) 직후
- 배경: Phase 5(운영 전환)의 첫 실제 대상으로, orch를 관찰하고 조작하는 작업공간을 만듭니다. Tern(Stencil, 클로즈드 베타)의 기능 일부와 Hyprland의 조작감을 가져오고, orch 특성상 필요한 화면을 더합니다.

## 1. 정할 항목

| # | 항목 | 권장안 | 다른 안 | 상태 |
| --- | --- | --- | --- | --- |
| D1 | 클라이언트 형태 | 맥에서 서버, 브라우저 PWA (Windows·iPhone에 설치 없음) | 네이티브(Tauri), TUI | 미정 |
| D2 | 기술 스택 | TypeScript 단일 (Node 서버 + `node-pty`, xterm.js) | Python 서버 + TS 화면, Rust | 미정 |
| D3 | v1 범위 | 보기 전용 + 터미널 연결 | orch 명령 실행 포함 | 미정 |
| D4 | 저장소 이름·공개 | public (Free 조직 private은 ruleset 불가) | private | 미정 |
| D5 | 저장소 구성 | 7장 참고: 저장소 분리 + 버전 붙은 출력 계약 | 허브 통합, 서브모듈 | 미정 |
| D6 | 측정 먼저 | 4장 측정을 허브 작업보다 먼저 | 작업과 병행 | 미정 |
| D7 | 진행도 데이터 | 대상별 `docs/roadmap.toml` + 지시서 `milestone` 필드 | 설계 문서 표 파싱 | 미정 |

아래 2·3장은 D1~D4의 권장안을 전제로 씁니다.

## 2. 원칙

- 화면은 오케스트레이션 로직을 갖지 않습니다. 상태는 orch가 내는 JSON으로만 읽고, 동작은 orch 명령으로만 합니다.
- state.db 스키마, 실행 로그 형식, 설계 문서의 문구에 직접 의존하지 않습니다.
- 머지와 force push는 어떤 범위에서도 화면에 두지 않습니다(허브의 Merge policy).
- 업그레이드는 알리기만 하고 실행하지 않습니다. 고정 버전은 허브 PR과 사용자 승인으로만 바뀝니다.

## 3. 기능 요건

v1은 1주일 운용 범위, v2는 그 뒤입니다.

### A. 작업공간 (Tern 채택)

| ID | 요건 | 출처 | 범위 |
| --- | --- | --- | --- |
| WS-1 | 맥에서 서버 하나를 띄우고 Windows·iPhone 브라우저가 Tailscale로 접속. 홈 화면 설치(PWA) | Tern `web serve`, `remote serve` | v1 |
| WS-2 | 기존 tmux 세션 `orch-<target>`의 창을 xterm.js 터미널 타일로 연결. 세션 유지는 tmux와 `orch service restore`에 맡김 | Tern daemon | v1 |
| WS-3 | 여러 기기가 같은 세션을 동시에 보고, 한쪽 입력이 다른 쪽에도 보임 | Tern 다중 클라이언트 | v1 |
| WS-4 | Hyprland식 타일링: dwindle/master, 간격, 둥근 모서리, 활성 테두리 그라데이션, 베지어 애니메이션 | Tern 레이아웃 + Hyprland | v1 |
| WS-5 | 분할·탭·플로팅, 대상마다 workspace(Super+숫자), 스크래치패드(special workspace) | Tern | v1 |
| WS-6 | 키보드 위주 조작, 단축키 설정 파일 | Hyprland | v1 |
| WS-7 | 기기별 레이아웃 저장과 재접속 시 복원 | Tern | v1 |
| WS-8 | 휴대폰에서는 한 번에 한 패널, 스와이프 전환 | Tern iPhone | v1 |

### B. 블록 (Tern 채택, orch용으로 변경)

| ID | 요건 | 출처 | 범위 |
| --- | --- | --- | --- |
| BL-1 | 작업 보드: 상태별 열, 대기 사유(`waiting_for`), 막힌 의존 사슬, 새 배분 중단 사유 | Tern TODO.md 칸반 | v1 |
| BL-2 | 실행 로그 패널: 실행 시작 시 그 회차 로그 타일을 자동으로 열고 끝나면 표시 (`orch watch --panels` 대체) | Tern 패널 이벤트 | v1 |
| BL-3 | 작업 상세: 지시서, 결과 보고, G1~G3, 리뷰 판정(P0~P3)과 triage, 회차 기록 | Tern 파일 미리보기 | v1 |
| BL-4 | diff 보기(읽기 전용): 작업 diff, 리뷰 diff | Tern Git 블록 | v1 |
| BL-5 | 프로세스: 실행 중인 서브의 pid, 경과 시간, 시간 제한까지 남은 시간, CPU | Tern 작업 관리자 | v1 |
| BL-6 | 실행 기록 조회: runs와 게이트 결과 필터 (orch 출력으로) | Tern SQLite 브라우저 | v2 |

채택하지 않는 Tern 기능: 내장 에이전트(omp), 브라우저 블록, Jupyter, 화면 공유, 음성. 에이전트는 orch의 레인이 맡습니다.

### C. orch 전용 화면

| ID | 요건 | 범위 |
| --- | --- | --- |
| OR-1 | 병합 표(PR, Head, Base, Tests, Lint, Gates, Review, Merge by). user check와 `review-overridden` 강조. 머지 버튼 없음, GitHub 링크만 | v1 |
| OR-2 | 입력 대기: 메인이 권한 승인을 기다리면 스크래치패드에 알리고 메인 창으로 이동. 승인은 메인 터미널에서 | v1 |
| OR-3 | 운용 상태: doctor 항목과 수준, 새 배분 중단, 서비스 세션 생존 | v1 |
| OR-4 | 실시간 갱신: 상태 전이 이벤트 스트림, 끊기면 놓친 이벤트부터 다시 받음 | v1 |
| OR-5 | orch 명령 실행: `next`, `wait`부터. `kill`·`resume`·`discard`는 두 번 확인. merge·force push 영구 제외 | v2 |

### D. 에이전트 버전과 사용량

| ID | 요건 | 범위 |
| --- | --- | --- |
| AG-1 | Claude Code, Codex, orch의 설치·고정·최신 버전과 차이 | v1 |
| AG-2 | 새 버전 알림과 올리기 전에 다시 측정할 항목 목록. 자동 업그레이드·버튼 없음 | v1 |
| AG-3 | 서브 실행별 입력·캐시·출력·추론 토큰을 대상·레인·모델·기간별로 집계 | v1(Codex), C 레인은 P5-M2 뒤 |
| AG-4 | Codex 5시간·주간 사용률과 리셋 시각(마지막 실행 시점 값임을 표시). Claude 한도는 P5-M1 결과에 따름 | v1 / 미정 |
| AG-5 | 메인 세션 토큰 | P5-M5 뒤 |

### E. 진행도

| ID | 요건 | 범위 |
| --- | --- | --- |
| PG-1 | 대상별 페이즈 파이프라인, 현재 페이즈와 종료 조건 | v1 |
| PG-2 | 마일스톤 노드별 작업 상태 색과 진척률. 폐기 후 새 id로 낸 작업은 한 줄로 묶음 | v1 |
| PG-3 | 시간축: 실행 구간, 게이트, PR 생성·머지 | v2 |

### F. 비기능 요건

| ID | 요건 |
| --- | --- |
| NF-1 | Tailscale 인터페이스에만 바인드, Tailscale 신원 또는 토큰 인증, WebSocket Origin 검사. 터미널 연결은 원격 셸과 같은 위험으로 다룸 |
| NF-2 | 서버는 `~/.orch/config` 전체, `~/.orch/secrets`, 토큰을 읽지 않음. orch 데이터는 orch가 `redact`를 거쳐 낸 출력만 씀 |
| NF-3 | 실행 호스트는 Intel 맥(x86_64). 가벼운 서버, launchd 시작·재기동 |
| NF-4 | orch 출력 계약 버전 확인. 모르는 버전이면 해당 화면만 끄고 경고 |
| NF-5 | 의존성 설치는 준비 단계에서만 네트워크 사용, 테스트·lint는 네트워크 없이 (orch 게이트 조건) |

## 4. 측정 (코드 변경 없음, 허브 작업보다 먼저)

외부 도구의 동작은 고정 버전에서 잰 결과로만 지시서에 씁니다.

| ID | 확인할 것 | 영향 |
| --- | --- | --- |
| P5-M1 | Claude 구독 한도(5시간·주간)를 비대화형으로 읽을 수 있는지 (Claude Code 2.1.319) | AG-4 |
| P5-M2 | `claude -p` JSON 출력의 인자, 종료 코드, usage 필드 | AG-3, `m-p5-lane-c-usage` |
| P5-M3 | Claude Code·Codex 최신 버전 확인 위치와 인증 필요 여부 | AG-1, `m-p5-versions` |
| P5-M4 | 권한 승인 대기가 끝났음을 알 수 있는 훅 이벤트 | OR-2, `m-p5-input-wait` |
| P5-M5 | 메인 세션 기록의 usage 필드 | AG-5 |

확인된 사실(2026-11-15): B 레인 실행 로그의 `turn.completed`에 `usage`(입력, 캐시 입력, 캐시 쓰기, 출력, 추론 토큰)가 있습니다. C 레인 로그는 일반 텍스트라 usage가 없습니다. runs 테이블에는 토큰 열이 없습니다. Notification 훅의 `input_wait`는 ntfy로만 보내고 기록을 남기지 않습니다. Claude Code 고정 버전은 `launcher.py`의 상수, Codex 버전은 `~/.orch/config`의 절대 경로에 들어 있습니다.

## 5. 허브 작업

### 하네스 PR

| ID | 내용 | user check |
| --- | --- | --- |
| `h-docs-p5` | Phase 5 계획, orch-deck과 허브의 경계, 출력 계약 규칙, 로드맵 파일 형식, `milestone` 필드, Tern 항목 변경("보류" → "자체 작업공간으로 대체"), 측정 결과 | — |
| `h-pins` | 고정 버전과 버전별 재측정 항목을 한 파일(예: `config/pins.toml`)로, `templates/task.md`에 `milestone` | ✅ |
| `h-roadmap-hub` | 허브 `docs/roadmap.toml`, Phase 0~5 이관 | — |

### orch 작업

| ID | 내용 | 등급 | 수정 범위 | 의존 | 묶음 |
| --- | --- | --- | --- | --- | --- |
| `m-p5-schema6` | 스키마 v6: runs 토큰 열, tasks `milestone`, 지시서 검증 | L3 | `store.py`, `models.py`, 지시서 파서 | `h-pins` | 1 |
| `m-p5-usage-b` | B 레인 `turn.completed` usage를 runs에 기록 | L3 | `runner.py`, `lanes/codex.py`(안전) | schema6 | 1 |
| `m-p5-events` | `orch events --follow [--since <id>] [--target]`, JSON 한 줄씩 | L3 | `cli.py`, `store.py` | — | 1 |
| `m-p5-show` | `orch show <id>`: 지시서, 결과, 게이트, 리뷰, PR, 로그 경로 | L2 | `cli.py`, `render.py` | schema6 | 1 |
| `m-p5-contract` | 화면이 읽는 출력에 `schema_version`과 계약 테스트 | L2 | `cli.py` | 위 명령 | 1 |
| `m-p5-lane-c-usage` | C 레인 JSON 출력과 usage 기록 | L3 | `lanes/claude.py`(안전) | schema6, P5-M2 | 2 |
| `m-p5-input-wait` | 입력 대기 시작·끝 기록과 events 출력 | L3 | `hooks.py`(안전) | P5-M4 | 2 |
| `m-p5-usage` | `orch usage`: 버전, 토큰 집계, 한도 | L3 | 새 `usage.py`, `cli.py` | usage-b, `h-pins`, P5-M3 | 2 |
| `m-p5-versions` | doctor `agent_versions`(하루 한 번, 경고만), `launcher`가 `pins.toml`에서 고정 버전을 읽음 | L3 | `doctor.py`, `launcher.py`(안전) | `h-pins`, P5-M3 | 2 |
| `m-p5-roadmap` | `orch roadmap [--target]`: 대상 기본 브랜치의 로드맵 검증과 진척률 | L2 | 새 `roadmap.py`, `cli.py` | schema6, `h-docs-p5` | 2 |

묶음 1이 끝나면 orch-deck v1을 시작할 수 있습니다. 각 묶음 뒤에 버전 PR, 태그, 승인 후 설치를 거칩니다. 새로 생기는 `usage.py`, `roadmap.py`를 안전 장치 경로에 넣을지는 `h-docs-p5`에서 정합니다.

### orch-deck 저장소 준비

사용자가 저장소를 만들고 App 설치와 ruleset을 적용합니다. 메인이 기본 구조를 첫 커밋으로 넣고, 등록 전에 test·lint와 `git status --porcelain`을 확인한 뒤 `orch target add`로 등록합니다.

## 6. 미해결 위험

- 터미널 연결은 원격 셸입니다. NF-1이 무너지면 Tailscale 안의 누구든 메인 세션에 입력할 수 있습니다.
- 출력 계약을 정해도 orch 버전과 화면 버전이 어긋나는 기간이 생깁니다(NF-4).
- 묶음 1만 해도 안전 장치 경로가 하나(`lanes/codex.py`) 들어갑니다.

## 7. 저장소 구성 (D5)

화면은 orch에 의존하고, orch는 화면에 의존하지 않습니다. 의존이 한 방향이므로 그 경계를 어디에 두느냐의 문제입니다.

| 안 | 내용 | 장점 | 단점 |
| --- | --- | --- | --- |
| A. 허브 통합 | 허브에 `deck/` 디렉터리를 두고 한 저장소, 한 버전 | 계약 변경과 화면 변경이 한 PR. 버전이 항상 맞음 | 허브 런타임은 표준 라이브러리만 쓰는 Python인데 Node 도구 체인이 들어옴. 허브 전체 테스트가 이미 약 14분 반(2026-11-15 측정)인데, G2가 두 언어의 전체 테스트를 모두 돌리게 됨. 하네스와 안전 장치 경로가 있는 저장소에 화면 코드가 섞여 리뷰 부담이 커짐. Phase 5의 "실제 대상 운용"이 다시 자기 적용이 됨 |
| B. 분리 + 서브모듈 | 화면 저장소가 허브를 서브모듈로 포함(또는 반대) | 특정 허브 커밋에 고정 | `orch prepare`·워크트리·게이트 스냅샷이 서브모듈을 다루지 않음(새 기능 필요). 준비 단계에서 서브모듈 초기화에 네트워크와 App 권한이 더 필요. 화면은 허브 코드가 아니라 출력 계약만 필요하므로 얻는 것이 적음 |
| **C. 분리 + 버전 붙은 출력 계약 (권장)** | 허브가 화면용 출력의 JSON Schema를 `schemas/`에 버전별로 두고 태그로 배포. 화면 저장소는 지원할 계약 버전의 스키마 사본을 고정해 넣고 계약 테스트로 확인. 실행 중에는 `orch --version`과 `schema_version`으로 맞는지 확인 | 의존 방향과 저장소 경계가 같음. 각자 자기 언어·게이트·시간 제한. 허브의 안전 장치 경로가 그대로. Phase 5가 진짜 외부 대상 운용이 됨 | 계약을 바꿀 때 PR이 둘(허브 먼저, 화면 나중). 어긋나는 기간을 NF-4로 다뤄야 함 |

C를 쓰더라도 사용자에게는 하나의 서비스처럼 보이게 할 수 있습니다.

- 설치: orch-deck은 자체 launchd 에이전트를 갖되, 설치 안내는 한 문서로 둡니다.
- 감시: `orch doctor`에 orch-deck 생존 점검 항목을 추가할 수 있습니다(선택, 경고만).
- 진입점: `orch open` 출력에 orch-deck 주소를 함께 냅니다(선택).
- 호환성: orch-deck이 지원하는 orch 버전 범위를 선언하고, 허브의 버전 PR 체크리스트에 "orch-deck 계약 테스트 통과"를 넣습니다.
