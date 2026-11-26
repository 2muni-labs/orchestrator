# 0032. 에이전트 모델을 버전 전환과 함께 따라감

- 상태: 결정됨
- 날짜: 2026-11-26
- 관련 문서: 기술 설계 2장·4.1·4.6·4.13·5.2, 실행 설계 3장·5장, ADR 0019

## 맥락

ADR 0019로 Claude Code와 Codex의 버전은 selfcheck가 검증하고 사용자가 승인해 전환합니다. 그러나
쓰는 모델은 코드와 문서에 고정되어 있었습니다. Codex는 지시서 검사·리뷰·explore·selfcheck의
`gpt-6.1-sol`, Claude는 지시서 검사의 `claude-haiku-5-5`·`claude-sonnet-5-5`·`claude-opus-5-5`,
`orch open`의 `--model claude-opus-5-5`, selfcheck 세션의 `claude-haiku-5-5`, 메인 규칙의 "Opus 5.5
확인"입니다. 새 모델(`gpt-6.2-sol`)이 나와도 버전 전환만으로는 모델이 바뀌지 않고, 바꾸려면 코드
PR과 새 orch 설치가 필요했습니다.

사용자 결정(2026-11-26): 모든 에이전트를 최신 모델로 바꾸고, Codex 버전을 올릴 때 모델도 함께
올라가게 합니다. sol 계열에 한정하지 않고 Claude를 포함한 모든 에이전트에 적용합니다. Claude는
등급 별칭을 유지합니다.

측정:

- Claude Code 2.1.330(지금 버전), `claude -p --model <별칭> --output-format stream-json --verbose
  --max-turns 1`: `haiku`·`sonnet`·`opus`가 각각 `claude-haiku-5-5`·`claude-sonnet-5-5`·
  `claude-opus-5-5`로 해석되었고, `system`/`init` 이벤트의 `model`과 `result`의 `modelUsage` 키가
  같았습니다. 세 번 모두 종료 코드 0, `is_error` 거짓(2026-11-26).
- codex-cli 0.171.0, `codex debug models`: 종료 코드 0, stderr 없음, 표준 출력은
  `{"models": [...]}` JSON 하나(약 820 KiB). 모델마다 `slug`, `visibility`(`list`·`hide`),
  `supported_in_api`, `priority`(작을수록 우선), `supported_reasoning_levels`(항목의 `effort`)가
  있습니다. 기본 실행은 목록을 새로 받아 오고, `--bundled`는 바이너리에 든 목록만 냅니다. 이때 1순위는
  `gpt-6.2-sol`, 다음이 `gpt-6.2-astra`, `gpt-6.1-terra`였고, `gpt-reserve`와 `codex-auto-review`는
  `hide`였습니다(2026-11-26). 같은 날 사용자가 `codex exec -m gpt-6.2-sol`로 답을 받았습니다.

## 선택지

1. 지금처럼 모델을 코드에 고정하고, 바꿀 때마다 PR과 설치
2. 설정 파일에 모델을 두고 사용자가 손으로 바꿈
3. Codex는 selfcheck가 후보 버전의 모델 목록에서 고르고 버전과 함께 검증·전환·되돌리기. Claude는 등급
   별칭을 쓰고 selfcheck가 별칭이 해석된 모델 ID를 기록

## 결정

3을 택합니다.

**Codex**

- selfcheck는 후보 바이너리의 `codex debug models` 목록에서 모델 하나를 고릅니다. 조건은
  `visibility`가 `list`, `supported_in_api`가 참, 추론 수준에 `low`·`medium`·`high`·`xhigh`가 모두
  있는 것이며, 그중 `priority`가 가장 작은 모델입니다. 계열(sol, astra, terra, luna)은 따지지 않습니다.
- 고른 모델로 S-M2·S-M2b·isolation 검사를 합니다. 모두 통과해야 후보가 `ready`이고, 버전과 모델을 함께
  후보로 기록합니다. 목록을 읽지 못하거나 조건에 맞는 모델이 없으면 후보는 `failed`입니다. 기본 모델로
  대신하지 않습니다(닫힌 쪽 실패).
- `--apply codex`는 버전과 모델을 함께 지금 기록으로 옮기고, `--rollback codex`는 둘을 함께 되돌립니다.
- 새 버전이 없어도 selfcheck는 지금 바이너리로 모델을 다시 고릅니다. 고른 모델이 지금 기록과 다르면
  같은 버전, 새 모델의 후보를 만들어 같은 검사를 하고, 사용자가 `--apply codex`로 전환합니다. 이번
  `gpt-6.2-sol` 전환이 이 경로입니다.
- 모델이 없는 지금 기록(v0.25 이전)과 초기 기록은 `config/pins.toml`의 `[codex] default_model`을
  씁니다. 지금 값은 지금까지 검증한 `gpt-6.1-sol`입니다.
- B 레인 지시서 검사, Codex 리뷰, `orch explore`, isolation 검사는 고정 상수 대신 기록된 모델을
  씁니다. 추론 강도 매핑(L1 `low` ~ L3R `xhigh`)은 그대로입니다.

**Claude**

- 지시서와 실행은 등급 별칭을 씁니다: L1 `haiku`, L2·L3 `sonnet`(L3는 `high`), L3R `opus`. `orch
  open`의 메인 세션은 `opus`, selfcheck 세션은 `haiku`입니다. A 레인 정의는 이미 별칭입니다. Fable은
  쓰지 않습니다.
- selfcheck는 후보를 검사할 때, 그리고 지금 기록에 해석 결과가 없을 때 별칭마다 짧은 세션을 띄워 `init`
  이벤트의 `model`을 기록합니다. 전환과 되돌리기는 버전과 해석 결과를 함께 옮깁니다.
- 세션 시작의 주 모델 확인은 고정된 "Opus 5.5" 대신 기록된 `opus`의 해석 결과와 비교합니다.

**공통**

- 이미 `prepare`한 지시서는 적힌 모델을 그대로 씁니다. `prepare`만 지금 기록과 비교하며, 실행 중
  기록이 바뀌어도 진행 중인 작업을 바꾸지 않습니다.

## 결과

- 모델 전환에 코드 PR이 필요 없습니다. 버전 전환과 같은 selfcheck 검증과 사용자 승인을 거칩니다.
- 모델 선택이 Codex의 `priority` 순서에 따릅니다. 원하지 않는 모델이 1순위가 되면 `[selfcheck] hold`로
  버전을 막거나 되돌리는 것 외에 모델만 따로 막는 수단은 없습니다. 필요해지면 새 ADR로 정합니다.
- `codex debug models`의 형식과 목록 갱신 방식은 Codex 버전마다 다를 수 있으므로, Codex 재측정 항목에
  넣습니다.
- 구현은 `m-codex-model-select`(selfcheck_codex), `m-claude-model-alias`(taskfile 별칭, launcher,
  selfcheck 기록), `m-agent-model-use`(지시서 검사와 리뷰·explore가 기록된 Codex 모델을 읽음)로
  나누고 v0.26.0에 담습니다.
