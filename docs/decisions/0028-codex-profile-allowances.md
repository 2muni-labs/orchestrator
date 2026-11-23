# 0028. Codex 프로필의 허용 범위와 프록시 설정

- 상태: 결정됨
- 날짜: 2026-11-24
- 관련 문서: `보안 설계.md` 5장, 기술 설계 4.6, ADR 0016, ADR 0018, `docs/measurements/codex-sandbox.md`

## 맥락

ADR 0016과 0018은 Codex를 자체 샌드박스 없이 orch 프로필 안에서 실행하고, 네트워크는 허용 목록
프록시로만 내보내기로 정했습니다. 프로필이 무엇을 더 허용해야 하는지는 정하지 않았습니다.
측정(`cx-measure`, codex-cli 0.160.0, macOS 15.8.3)의 결과는 다음과 같습니다.

- 대문자 `HTTPS_PROXY` 하나로는 프록시를 거치지 않는 실행이 있었습니다. 소문자와 `ALL_PROXY`,
  `HTTP_PROXY`까지 여섯 개를 모두 두면 거쳤습니다. 그래도 원인을 모르게 프록시를 거치지 않은
  실행이 프로필 밖에서 두 번 있었습니다.
- 게이트 프로필은 키체인 서비스 조회를 막아 TLS가 실패합니다. trustd와 securityd 조회,
  `/Library/Keychains` 읽기를 열면 통과하고, 이 조건에서도 사용자 키체인 항목은 읽히지 않았습니다.
- `~/.codex`에서 쓰는 곳은 상태 DB, `sessions/`, `tmp/`, `cache/`, `thread-writer-locks/`,
  `shell_snapshots/`, `models_cache.json`, `installation_id`(쓰기 모드로 열기만 함)입니다.
  `config.toml`과 `auth.json`은 쓰지 못해도 동작합니다.
- 프록시가 본 호스트는 `chatgpt.com`과 `ab.chatgpt.com`의 443뿐이었습니다.

## 선택지

1. `~/.codex`는 측정된 경로만 쓰기 허용. 나머지(`config.toml`, `auth.json`, `hooks.json`, 전역
   지시 파일 등)는 거부
2. `~/.codex` 전체 쓰기 허용, `config.toml`만 거부
3. `~/.codex` 전체 쓰기 허용

## 결정

1을 택합니다. 2와 3은 Codex가 실행한 명령이 `hooks.json`이나 전역 지시 파일을 고쳐, 다음
Codex 실행(orch 밖의 사용자 실행 포함)에 영향을 남길 수 있습니다. 1에서 남는 쓰기 경로는
Codex의 기록과 캐시입니다.

## 결과

- 프로필은 게이트 프로필에서 출발해 아래만 바꿉니다.
  - 네트워크: `localhost:<그 실행의 프록시 포트>`로 나가는 연결만 허용
  - 조회: `com.apple.trustd`, `com.apple.trustd.agent`, `com.apple.SecurityServer`,
    `com.apple.securityd`, `com.apple.securityd.xpc`
  - 읽기: `/Library/Keychains`, `~/.codex`(인증 파일 포함, ADR 0016)
  - 쓰기: `~/.codex`의 `^[a-z_]+_[0-9]+\.sqlite(-wal|-shm)?$`, `sessions/`, `tmp/`, `cache/`,
    `thread-writer-locks/`, `shell_snapshots/`, `models_cache.json`, `.sqlite-maintenance.lock`,
    `installation_id`. 구현은 워크트리와 실행마다의 `TMPDIR`을 더하고, 리뷰와 탐색은 워크트리
    대신 `-o` 파일 하나만 더함
- orch는 Codex 환경에 프록시 변수 여섯 개를 모두 둡니다. 프록시를 거치지 않으려는 연결은
  프로필이 막으므로, 그때 Codex는 실패로 끝나고 우회해서 성공하지 않습니다.
- 허용 목록은 `config/pins.toml`의 `[codex] proxy_hosts`에 둡니다. 호스트는 소문자로 바꾸고
  끝의 점과 포트를 떼어 정확히 같을 때만 통과시키고, IP 주소로 된 CONNECT는 거부합니다. 거부한
  CONNECT는 실행 로그와 결과에 남깁니다. 새 호스트는 거부 기록으로 관찰된 뒤에만 설정 PR로 더하고,
  측정을 위해 토큰 갱신을 억지로 일으키지 않습니다.
- 프록시가 시작하지 못하거나 실행 중에 끝나면 그 Codex 실행은 실패로 닫습니다. 프록시 없이
  Codex를 실행하는 경로는 두지 않습니다.
- 명령 실행이 기대되는 실행에서 `command_execution` 항목의 `item.completed` 이벤트가 하나도 없으면,
  종료 코드가 0이어도 성공으로 보지 않습니다(ADR 0016).
- 키체인 서비스 조회를 여는 것이 남기는 통로: 프로필 안의 프로세스가 securityd에 요청할 수
  있습니다. 측정에서 잠금 해제된 사용자 키체인의 항목은 읽히지 않았고, 데이터 보호 키체인(`secd`)은
  계속 막습니다. Codex나 macOS 버전을 올릴 때 이 조건과 `~/.codex` 쓰기 경로를 다시 잽니다
  (`config/pins.toml`의 S-M2, S-M2b).
