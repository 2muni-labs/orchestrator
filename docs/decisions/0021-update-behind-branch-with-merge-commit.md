# 0021. 뒤처진 PR 브랜치를 orch update가 병합 커밋으로 갱신함

- 상태: 결정됨
- 날짜: 2026-11-17
- 관련 문서: 기술 설계 4.7.1·4.7.2, 실행 설계 3장, `config/main.md` 절차 8a·8b

## 맥락

허브의 main 규칙 세트는 필수 체크 `ci`와 함께 머지 전 브랜치가 최신이기를 요구합니다(`strict_required_status_checks_policy`). 2026-11-17에 PR 두 개(#143, #144)를 같은 main 위에 올렸고, #144가 먼저 머지되자 #143의 머지가 거부되었습니다. 응답은 `Required status check "ci" is expected`였고, head의 `ci`는 통과한 상태였습니다. PR의 `mergeStateStatus`는 `BEHIND`, compare의 `behind_by`는 3이었습니다.

`orch update`는 충돌이 없으면 임시 병합을 검사만 하고 버리며 push하지 않았습니다. 브랜치를 최신으로 만드는 경로는 충돌이 있을 때뿐이어서, strict 규칙 세트에서는 두 번째 PR이 언제나 막혔습니다. 이번에는 사용자가 GitHub의 Update branch로 갱신했습니다.

## 선택지

1. 규칙 세트의 strict를 끕니다. 순서대로 머지할 때 앞 PR이 바꾼 main과 합친 상태를 필수 체크가 보장하지 않게 되어, ADR 0020의 "합친 상태에서 필수 체크" 전제가 약해집니다.
2. 뒤처질 때마다 사용자가 Update branch를 누릅니다. `orch`의 `pushed_sha`가 원격과 어긋나고, 사용자 개입이 PR마다 늘어납니다.
3. `orch update`가 충돌 없는 병합 커밋을 게이트 통과 뒤 push하고, `orch wait`가 뒤처짐을 알려 메인이 `orch update`를 실행하게 합니다.

## 결정

3을 택합니다.

- `orch update`는 충돌이 없고 게이트가 통과하며 병합이 새 커밋을 만들었을 때, 리뷰 라벨과 `needs-user`를 먼저 떼고 병합 커밋을 lease push한 뒤 `pushed_sha`와 `gated_sha`를 갱신합니다. rebase가 아닌 병합 커밋이므로 원격에서는 앞으로 감기이며 force push가 늘지 않습니다.
- 라벨을 push보다 먼저 떼므로, 라벨 제거가 실패하면 아무것도 push되지 않고 다시 실행해도 같은 결과입니다. push가 실패하면 라벨만 빠진 상태이고, `orch wait`가 현재 head의 필수 체크 통과 뒤 다시 붙입니다.
- `orch wait`는 strict 규칙 세트에서 compare의 `behind_by`가 1 이상이면 리뷰 라벨을 붙이기 전에 63(`behind`)으로 끝납니다. `mergeStateStatus`는 `UNKNOWN`이 될 수 있어 쓰지 않습니다.
- `user-approved`는 지금처럼 봇이 건드리지 않습니다. push보다 먼저 붙은 승인은 머지 작업이 인정하지 않으므로, 사용자는 새 리뷰 뒤에 다시 승인합니다.

## 결과

- main이 움직일 때마다 열린 PR에 push가 하나 늘고, `ci`와 예약 작업의 리뷰가 다시 돕니다. 동시에 여는 PR을 1~2개로 두는 지금 규모에서는 감당할 수 있습니다.
- `verify-deep` PR은 main이 움직이면 사용자가 다시 승인해야 합니다.
- 구현은 `m-pr-fix`(`orch update`의 push)와 `m-wait-review`(63)가 맡고, `config/main.md`의 절차 8a·8b는 버전 PR에서 바꿉니다.
