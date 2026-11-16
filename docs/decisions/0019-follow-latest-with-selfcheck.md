# 0019. 고정 버전 대신 selfcheck가 검증한 버전으로, 사용자 승인을 받아 전환함

- 상태: 결정됨
- 날짜: 2026-11-16
- 관련 문서: 요건 정의 12장(원칙, AG-1·AG-2), 기술 설계 4.13·5.2·5.3, 실행 설계 6장 환경 전제, 구축 절차 4.3, `config/pins.toml`

## 맥락

지금은 Claude Code(2.1.319)와 Codex(0.160.0)의 버전을 고정합니다. 버전을 올릴 때마다 사람이 다시 측정하고, PR로 고정 버전을 바꿉니다. guard, 훅의 차단 방식, Codex 격리와 프록시 같은 안전 장치가 측정한 도구 동작을 전제로 하기 때문입니다. 그런데 새 기능(예: Claude Code 2.1.323 이상의 Code Mode)을 쓰려면 버전을 자주 올려야 합니다. 재측정을 매번 사람이 하면 그만큼 늦어집니다. 반대로 검사 없이 자동 업데이트를 켜면, 도구 동작이 조용히 바뀌어 안전 장치가 꺼질 수 있습니다. 요건 12장의 원칙은 업그레이드를 알리기만 하고, 버전은 사용자 승인으로만 바꾸게 합니다.

측정(2026-11-16, 결과는 `~/.orch/<대상>/measure/fl/`):

- **FL-M1 (Claude Code 설치):** `claude install <버전>`은 `versions/<버전>`에 내려받자마자 `~/.local/bin/claude` 링크를 새 버전으로 바꿉니다. 릴리스마다 `manifest.json`에 플랫폼별 sha256과 크기가 있습니다. 내려받은 바이너리의 sha256은 이 값과 일치했습니다. 새 바이너리는 링크를 거치지 않고 절대 경로로도 실행됐습니다.
- **FL-M2 (mise의 Codex 설치):**
  - `mise install codex@<버전>`(aqua 백엔드)은 활성 버전을 바꾸지 않고, 새 버전의 체크섬을 lock에 넣지도 않습니다.
  - `mise lock`은 설치 없이 체크섬과 주소를 lock에 기록합니다. 다만 이전 버전 항목을 지웁니다.
  - lock의 체크섬을 틀리게 바꾸면 `mise install`이 종료 코드 1로 거부합니다.
  - 작업 디렉터리가 실제 홈 아래에 있으면, HOME을 바꿔도 mise가 위쪽 디렉터리의 실제 `~/.config/mise/config.toml`을 읽습니다.
- **FL-M3 (Claude Code 서명):**
  - `manifest.json.sig`는 ASCII armor 형식의 분리 PGP 서명입니다. v4, RSA, SHA-512를 씁니다.
  - 서명 키는 `https://downloads.claude.ai/keys/claude-code.asc`에 있습니다. RSA 4096 키이고, 지문은 `31DDDE24DDFAB679F42D7BD2BAA929FF1A7ECACE`, uid는 "Anthropic Claude Code Release Signing"입니다.
  - 표준 라이브러리만으로 검증했습니다(PKCS#1 v1.5). 2.1.300, 2.1.319, 2.1.325는 통과했고, manifest를 한 바이트 바꾸면 실패했습니다.
  - manifest의 `buildDate`도 서명 범위에 들어 있습니다.
  - 설치 스크립트는 이 서명을 확인하지 않습니다.
  - 바이너리는 Anthropic의 Developer ID(팀 `Q6L2SF6YDW`)로 코드 서명되고 공증돼 있습니다.
- **FL-M4 (Codex 체크섬과 서명의 출처):**
  - mise lock의 체크섬은 GitHub이 업로드 때 계산한 자산 `digest`와 같습니다. 같은 릴리스의 `codex-package_SHA256SUMS`와도 같습니다. SHA256SUMS에는 서명이 없습니다.
  - sigstore 번들은 linux 자산에만 있습니다. GitHub 아티팩트 증명은 macOS 패키지에 없습니다(404).
  - 그래서 체크섬만으로는 발행자 서명이라는 FL-M3의 기준을 채우지 못합니다.
  - 대신 패키지의 실행 파일(`codex`, `codex-code-mode-host`, `rg`)은 OpenAI의 Developer ID(팀 `2DC432GLL2`)로 코드 서명되고 공증돼 있습니다.
  - `codesign --verify --strict -R='anchor apple generic and certificate leaf[subject.OU] = "<팀>"'`의 종료 코드는 팀이 맞으면 0, 팀이 다르면 3, 변조본이면 1이었습니다.
- **FL-M5 (orch 전용 mise 설정):**
  - `MISE_GLOBAL_CONFIG_FILE`만 바꾸면, 작업 디렉터리가 홈 아래일 때 실제 사용자 설정도 함께 읽습니다.
  - `MISE_CONFIG_DIR`(또는 `MISE_CEILING_PATHS`)을 함께 주면 홈 아래에서도 전용 설정만 읽습니다.
  - `MISE_DATA_DIR`, `MISE_CACHE_DIR`, `MISE_STATE_DIR`을 전용 디렉터리로 주면, `mise lock --global`과 `mise install`이 전용 lock과 전용 설치 디렉터리에만 씁니다. 실제 사용자 lock의 sha256, 설치본, 대화형 `mise ls` 결과는 그대로였습니다.
  - 버전 하나에 449MB를 씁니다.

## 선택지

**전환 방식**

1. 지금처럼 버전을 고정하고, 사람이 재측정한 뒤 PR로 올림
2. 도구의 자동 업데이트를 켬
3. orch가 새 버전을 옆에 설치하고, 재측정 항목을 새 바이너리로 돌려 통과하면 바로 전환함
4. 3과 같이 검증과 설치 준비까지 orch가 하고, 전환은 사용자 승인 한 번(`orch selfcheck --apply`)으로 함

**Codex 설치 위치**

- A. 사용자 개인 mise 설정과 lock의 복사본으로 준비하고, 통과하면 실제 파일을 바꿈
- B. orch 전용 mise 설정, lock, 설치 디렉터리(`~/.orch/mise/`)

## 결정

전환 방식은 4, Codex 설치 위치는 B로 정합니다.

- 1은 느립니다.
- 2는 안전 장치의 전제를 검사 없이 바꿉니다.
- 3은 요건 12장의 원칙(사용자 승인으로만 바꿈)과 충돌합니다.
- 4는 전제를 매번 기계적으로 확인합니다. 사람에게 남는 일은 승인 한 번입니다.

**후보와 보류**

- 후보는 출시된 지 `min_age_days`(기본 3일)가 지난 버전 가운데 가장 새 버전입니다. 지금 버전보다 새 버전이어야 합니다.
- 출시 시각:
  - Claude Code는 서명 범위 안의 `buildDate`를 씁니다. `latest`의 버전부터 패치 번호를 낮추며 manifest를 확인합니다.
  - Codex는 GitHub 릴리스 목록에서 사전 릴리스를 뺀 항목의 `published_at`을 씁니다.
- 사용자는 `~/.orch/config`의 `[selfcheck] hold`에 특정 버전을 적어 보류합니다. 보류한 버전은 후보가 되지 않습니다.

**출처 검증 (필수)**

출처 검증은 전환의 필수 조건입니다. 검증할 수 없으면 전환하지 않습니다.

- Claude Code:
  - 허브에 커밋한 공개 키 `config/keys/claude-code.asc`로 `manifest.json.sig`를 검증합니다. 키의 지문은 `config/pins.toml`에 고정합니다. 실행 중에 키를 내려받지 않습니다. 키가 릴리스와 같은 호스트에 있기 때문입니다.
  - 내려받은 바이너리의 sha256과 크기를 서명된 manifest의 값과 비교합니다.
  - 코드 서명 팀이 `Q6L2SF6YDW`인지 확인합니다.
  - 서명이 없거나 형식을 모르거나 검증에 실패하면 전환하지 않습니다.
- Codex:
  - 체크섬에는 발행자 서명이 없으므로, 같은 기준인 발행자 서명은 macOS 코드 서명으로 채웁니다. 패키지 안의 모든 Mach-O 실행 파일이 팀 `2DC432GLL2`의 요구 조건을 통과해야 합니다.
  - lock의 체크섬은 같은 릴리스의 SHA256SUMS와 일치해야 합니다. 이 일치는 서명이 아니라 일관성 확인입니다.

**검증과 설치 준비 (`orch selfcheck`)**

- 새 세션을 시작할 때(`orch open`)와 사용자가 직접 `orch selfcheck`를 실행할 때 돕니다. 주기 실행은 없습니다.
- 후보를 옆에 설치하고 출처를 검증합니다. 이어서 `config/pins.toml`의 재측정 항목을 새 바이너리의 절대 경로로 실행합니다.
  - 차단(`blocking`) 항목은 모두 통과해야 합니다. 안전 장치의 전제가 여기에 들어갑니다: m14-cli 훅 차단 방식, deny 규칙의 의미, `--agents`·`--append-system-prompt-file` 주입, Codex의 S-M2·S-M2b와 hooks·plugins 끄기 격리.
  - 차단 항목마다 selfcheck 안에 자동 검사가 있어야 합니다. 검사가 없는 항목이 하나라도 있으면 그 버전은 전환하지 않습니다.
  - 경고(`warn`) 항목은 결과에 경고로 냅니다.
- 통과한 후보는 `ready`로 기록합니다. 기록에는 바이너리의 sha256과 검사에 쓴 `config/pins.toml`의 해시가 들어갑니다. 그다음 알립니다.
- 실패하면 전환하지 않습니다. 결과를 기록하고 알립니다.

**전환과 되돌리기**

- 전환은 사용자가 승인한 `orch selfcheck --apply`만 합니다. 메인 에이전트가 이 명령을 실행하면 pre-bash가 사용자 확인(`ask`)을 받습니다. `--rollback`도 같습니다.
- `--apply`는 기록 뒤에 바이너리와 `config/pins.toml`이 바뀌지 않았는지 다시 확인합니다. 그다음 링크를 원자적으로 바꾸고, 이전 버전을 되돌리기 대상으로 기록합니다.
- `orch selfcheck --rollback <에이전트>`는 그 대상으로 링크를 되돌립니다. 그 버전의 설치본은 남겨 둡니다. 더 오래된 버전은 지웁니다.

**지금 쓰는 버전의 확인**

- `orch open`과 doctor는 지금 쓰는 바이너리의 버전과 sha256이 마지막으로 전환한 기록과 같은지 확인합니다.
- 다르면, 예를 들어 selfcheck 밖에서 업데이트됐으면, 새 배분을 멈추고(사유 `agent_unverified`) 알립니다.
- Claude Code의 자체 자동 업데이트는 계속 끕니다(`DISABLE_AUTOUPDATER=1`). `claude install`은 설치하자마자 전환하므로 쓰지 않습니다.

**Codex 설치 위치: B를 택한 이유**

- A의 문제:
  - 사용자의 개인 설정 저장소에 미커밋 변경을 남깁니다.
  - 대화형 Codex와 orch가 버전을 함께 씁니다.
  - 위쪽 설정을 읽는 함정을 매번 피해야 합니다.
- B의 장점:
  - 사용자 설정에 손대지 않습니다.
  - 읽는 설정이 전용 설정 하나뿐임을 환경 변수로 고정할 수 있습니다.
  - 되돌리기는 링크만 옮기면 됩니다.
- B의 비용:
  - 버전마다 약 450MB를 더 씁니다.
  - 대화형 Codex는 개인 mise에 그대로 남아, orch와 버전이 다를 수 있습니다.
- 동작:
  - orch는 `~/.orch/mise/`에서 `MISE_GLOBAL_CONFIG_FILE`, `MISE_CONFIG_DIR`, `MISE_DATA_DIR`, `MISE_CACHE_DIR`, `MISE_STATE_DIR`을 전용 경로로 주고 mise를 실행합니다.
  - 실행 전에 `mise config ls`가 전용 설정만 내는지 확인합니다.
  - `mise lock --global`은 이전 버전 항목을 지우므로 후보는 설정과 lock의 복사본으로 준비하고, `--apply`가 원자적으로 바꿉니다.
  - 실행 경로는 `~/.orch/bin/codex` 링크입니다.

**재측정 항목과 기준**

- `config/pins.toml`의 재측정 항목, 서명 키, 지문, 코드 서명 팀은 PR과 사용자 확인으로만 바뀝니다.
- `version`은 사람이 마지막으로 재측정한 기준 버전입니다. selfcheck는 이 값을 바꾸지 않습니다.

## 결과

- 순서:
  1. `m-guard-wrapper`가 guard의 차단 누락(`mise exec codex -- codex` 통과)과 오탐을 고칩니다. 같은 작업에서 `orch selfcheck --apply`와 `--rollback`을 사용자 확인(`ask`) 대상으로 넣습니다. 차단 누락을 고치는 일이므로 먼저 합니다.
  2. 그다음 `m-selfcheck`(B, L3)가 구현합니다.
  3. 그 뒤 설치하고, 첫 전환(Code Mode 설정 포함)을 거쳐 M17로 갑니다.
- 새 모듈 `src/orch/selfcheck.py`와 `src/orch/signature.py`는 안전 장치 경로(`targets.toml`의 `safety_paths`)입니다.
- Codex의 차단 항목 S-M2·S-M2b는 허용 목록 프록시와 orch 프로필(`m-p5-codex-sandbox`, M19)이 있어야 검사할 수 있습니다. 그 작업이 검사를 더할 때까지 Codex 후보는 `not_automated`이고 전환하지 않습니다. 첫 전환은 Claude Code만 합니다.
- 차단 항목 가운데 Claude Code 동작을 보는 검사(m14-cli, deny 규칙, 주입)는 새 바이너리로 짧은 비대화형 세션을 띄웁니다. 그래서 사용량을 조금 씁니다.
- 남은 위험:
  - 재측정 항목에 없는 동작 변화는 잡지 못합니다. 새 안전 장치를 만들면 그 전제를 차단 항목에 함께 넣습니다.
  - Codex 체크섬은 GitHub 릴리스를 믿는 것입니다. 발행자 서명은 코드 서명에만 기댑니다.
