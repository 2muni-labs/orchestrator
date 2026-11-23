# Codex 샌드박스 프로필 측정 (`cx-measure`)

`m-codex-profile`, `m-codex-proxy`, `m-codex-lane-sandbox`의 지시서를 쓰기 전에, Codex를
`--sandbox danger-full-access`로 orch 프로필 안에서 실행하는 데 필요한 최소 허용을 잰
기록입니다. 관련 이슈: #573. 결정은 ADR 0028에 있습니다.

- 측정: 2026-11-23~24 UTC, codex-cli 0.160.0(`config/pins.toml`의 기준 버전), macOS 15.8.3,
  모델 `gpt-6.1-sol`(`model_reasoning_effort=low`)
- 방법: 사용자가 메인 셸에서 측정 스크립트를 실행(메인 세션은 `codex`를 직접 실행하지 않음).
  스크립트는 샌드박스 밖에 모든 호스트를 통과시키고 CONNECT 대상을 세는 프록시를 띄우고,
  `orch.sandbox.PROFILE`(게이트 프로필)에서 출발한 후보 프로필로 `sandbox-exec`를 실행
- 환경 변수: `child_env`와 같은 허용 목록(PATH, HOME, USER, LOGNAME, LANG, LC_*, CODEX_HOME)에
  실행마다의 `TMPDIR`과 프록시 변수
- 기록에 남긴 것: 종료 코드, 이벤트 종류별 개수, 명령 실행 이벤트의 상태와 종료 코드,
  `~/.codex`의 변경 경로(숫자와 uuid를 지운 형태), 프록시가 본 `host:port`. 토큰, 설정 내용,
  개인 경로는 남기지 않음

## 결과

**프록시 환경 변수.** 대문자 `HTTPS_PROXY` 하나만 두면 Codex가 프록시를 거치지 않는
실행이 있었습니다. `HTTPS_PROXY`, `https_proxy`, `ALL_PROXY`, `all_proxy`, `HTTP_PROXY`,
`http_proxy` 여섯 개를 모두 두면 프록시를 거쳤습니다(로그의 `proxy intercepts`). 다만 같은
조건에서 프록시 기록이 0건인 실행도 두 번 있었고 원인을 찾지 못했습니다. 둘 다 프로필이
없는 실행이었습니다. 프로필 안에서는 직접 연결이 거부되므로, 이런 경우 Codex는 실패합니다.

**호스트.** 모든 실행에서 프록시가 본 대상은 `chatgpt.com:443`과 `ab.chatgpt.com:443`뿐이었습니다
(실행마다 수 건에서 수십 건). 토큰 갱신 등 다른 호스트는 나타나지 않았습니다. 이 기간에 토큰 갱신이
일어났는지는 확인하지 않았습니다.

**TLS.** 게이트 프로필은 키체인 관련 서비스 조회를 막습니다. 프록시까지 가도 TLS에서 실패했습니다.

| 프로필에 더한 허용 | 결과 |
| --- | --- |
| `com.apple.trustd`, `com.apple.trustd.agent` 조회, `/Library/Keychains` 읽기 | 실패 |
| 위 + 사용자 키체인 파일 읽기 (securityd 조회 없음) | 실패 |
| 위 첫 줄 + `com.apple.SecurityServer`, `com.apple.securityd`, `com.apple.securityd.xpc` 조회 | 통과 |

통과한 조건에서 사용자 키체인 디렉터리의 임시 키체인(잠금 해제, 항목 하나)을 `security
find-generic-password`로 읽으면 종료 코드 44로 실패했습니다. 게이트 프로필에서도 같았습니다.
측정이 끝난 뒤 임시 키체인을 지우고 검색 목록이 그대로인지 확인했습니다. 데이터 보호
키체인(`secd`)은 시험하지 않았고 계속 막습니다.

**`~/.codex` 쓰기.** `~/.codex` 전체 쓰기를 허용한 실행에서 바뀐 경로와, 실행 중 20ms마다
살펴 잠시 생겼다 사라진 경로입니다.

| 경로 | 용도로 보이는 것 |
| --- | --- |
| `<이름>_<n>.sqlite`, `-wal`, `-shm` (goals, logs, memories, state, thread_history) | 상태 DB |
| `sessions/` | 세션 기록(rollout) |
| `tmp/arg<n>/` | 실행용 임시 파일(apply_patch 등) |
| `cache/` | 앱 정보 캐시 |
| `thread-writer-locks/` | 스레드 잠금 |
| `shell_snapshots/` | 셸 스냅숏 |
| `models_cache.json` | 모델 목록 캐시 |

이 목록만 허용하면 Codex가 `failed to initialize in-process app-server client: Operation not
permitted`로 곧바로 끝났습니다. 바뀌지 않았지만 쓰기 모드로 여는 파일이 있다는 뜻입니다.
최상위 기존 항목 20개를 반씩 줄여 가며 시험해, 더 필요한 것은 `installation_id` 하나라는 것을
찾았습니다. `.sqlite-maintenance.lock`은 함께 허용했고 꼭 필요한지는 따로 시험하지 않았습니다.

**쓰지 않아도 되는 것.** `config.toml`은 쓰기를 막아도 동작합니다. Codex는 경고를 남기고
작업 디렉터리 신뢰를 메모리에만 둡니다. 막지 않으면 실행마다 임시 워크트리의 신뢰 항목을
덧붙입니다. `auth.json` 쓰기를 막은 실행도 모두 통과했습니다.

**최종 후보 (2026-11-24).** 게이트 프로필에서 `~/.codex` 거부를 빼고, 네트워크는
`localhost:<프록시 포트>`로 나가는 연결만, 위 TLS 허용, `TMPDIR`과 위의 `~/.codex` 경로만 쓰기
허용.

| 실행 | 결과 |
| --- | --- |
| 구현(워크트리 쓰기 허용) | 종료 코드 0. `ls`, `touch made.txt` 성공. `~/.codex/cx-probe-denied.txt` 만들기는 실패(종료 코드 1). `~/.orch/config` 1바이트 읽기는 0바이트와 `Operation not permitted` |
| 리뷰(워크트리 쓰기 없음, `-o` 파일만) | 종료 코드 0, `-o` 파일 2바이트 |

## 명령 실행 이벤트

`codex exec --json`의 명령 실행은 `item.started`와 `item.completed` 이벤트로 나옵니다.
`item`의 키는 `aggregated_output`, `command`, `exit_code`, `id`, `status`, `type`이고,
`type`은 `command_execution`, `status`는 `in_progress`, `completed`, `failed` 중 하나입니다.
명령이 실패하면 `status`가 `failed`이고 `exit_code`가 0이 아닙니다.

## 남은 것

- 프록시를 거치지 않은 두 실행의 원인. 프로필이 직접 연결을 막으므로 안전에는 영향이 없고,
  실패는 실행 결과에 드러납니다.
- 토큰 갱신 호스트. 거부된 CONNECT가 실행 로그와 결과에 드러나게 한 뒤 관찰되면 설정 PR로
  더합니다(ADR 0018, 0028).
- macOS가 `config/pins.toml`의 기준(15.8.1)보다 새 버전(15.8.3)입니다. S-M2b의 루프백 제한은
  이 측정에서 다시 재지 않았습니다.
