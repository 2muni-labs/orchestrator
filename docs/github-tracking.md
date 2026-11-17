# GitHub 작업 기록 운영 절차

`config/main.md`를 주입받은 메인이 수행합니다(ADR 0024). 기존 기록에 대상별 이슈·Projects를 추가하는 절차이며 CLI·DB·훅·서비스의 자동 동기화 구현은 아닙니다.

## 원본과 책임

- 상태의 원본은 `state.db`, 판정 근거는 지시서·결과·리뷰·게이트·git입니다. 자동 생성된 `TODO.md`도 유지합니다.
- 메인은 확인한 결과를 GitHub에 요약합니다. GitHub 편집으로 배분·재시도·승인·머지·로컬 전이를 하지 않습니다.
- 하네스·측정·리뷰 후속도 기록합니다. 상위 이슈를 자식 PR 하나의 완료로 닫지 않습니다.
- 같은 대상의 외부 기록은 메인 한 곳이 갱신합니다. 서브는 링크를 참고할 뿐 인증·API 쓰기·대응표 수정을 하지 않습니다.

## 영속 대응표

운용 파일은 `R/github-tracking.json`입니다. `R`은 `~/.orch/<대상>`이며 운용 파일은 커밋하지 않습니다. 메모·scratchpad만을 유일한 대응표로 쓰지 않습니다.

검토된 대상 기본 브랜치에 `docs/github-tracking.json`이 있으면 최초 복구 seed로 씁니다. 기존 운용 파일은 보존하고 빠진 연결만 대조해 추가합니다. seed는 식별자·링크 복구 자료이며 과거 상태를 현재 task 위에 덮어쓰는 자료가 아닙니다. 새 연결은 우선 운용 파일에 추가하고, 복구 자료·그룹 정의를 갱신할 때 PR로 seed를 바꿉니다.

형식 버전 1의 예시:

```json
{
  "schema_version": 1,
  "target": "example",
  "repository": "owner/example",
  "projects": {
    "G01": {"title": "Foundation", "url": "https://github.com/orgs/owner/projects/1"}
  },
  "records": {
    "task-a": {
      "title": "Implement an approved task",
      "kind": "task",
      "issue_url": "https://github.com/owner/example/issues/1",
      "project_key": "G01",
      "completion_owner": "task"
    }
  },
  "tasks": {"task-a": {"record_id": "task-a"}},
  "aliases": {},
  "sync": {
    "task-a": {
      "revision": 1,
      "desired": {"local_state": "prepared", "project_status": "In Progress", "issue_state": "OPEN"},
      "pending": true,
      "last_verified_at": null,
      "last_error_code": null
    }
  }
}
```

- 모르는 형식 버전은 추측해 고치지 말고 보고합니다. target·repository는 등록된 현재 대상과 일치해야 합니다.
- records는 외부 기록 단위, tasks는 실제 orch task와의 대응입니다. 같은 이슈에 여러 task를 묶으면 완료 책임을 aggregate로 두고 전체 종료 조건을 적습니다.
- 완료 책임은 task(단일 task), harness(하네스 PR), aggregate(전체 범위), manual(측정·결정의 개별 수락 근거)입니다. 기존 이슈의 완료 책임을 추측으로 task에 넘기지 않습니다.
- aliases는 옛 inventory의 B번호 등을 기존 record에 연결합니다. 별명 때문에 새 이슈를 만들지 않습니다.
- sync에는 미전송 의도와 최종 확인만 둡니다. 원하는 상태는 매번 현재 원본에서 다시 만듭니다. 토큰·원문 로그·설정·비밀은 넣지 않습니다.
- `R/github-tracking.lock` 배타 잠금 안에서 파일을 다시 읽고 같은 디렉터리의 임시 파일을 `os.replace`로 원자적으로 바꿉니다. API 응답 뒤에도 revision을 대조해 오래된 응답이 새 pending을 지우지 않게 합니다. 같은 대상의 갱신 담당을 하나로 정하기 전에는 외부 쓰기를 멈춥니다.

## 세션 시작과 계획

1. 기존 orch open·모델·하네스 최신 여부 확인 뒤 대응표를 읽습니다. 미동기화·실행 중·PR 대기·중단된 기록부터 확인하고 완료된 모든 이슈를 매번 다시 쓰지 않습니다.
2. tasks→records로 기존 연결을 찾습니다. 없으면 대상 이슈의 marker·task 이름·계획을 확인합니다. 여러 개가 일치하면 추측하지 말고 계획에서 정리합니다.
3. 승인된 계획의 작업은 대상 저장소의 기존 이슈·그룹을 쓰고, 없으면 그 계획 범위에서 만듭니다. 허브 외 작업을 자동으로 orchestrator 이슈에 보내지 않습니다. 적합한 그룹이 없으면 계획에 새 그룹을 포함하고, 접근 권한·공개 여부는 바꾸지 않습니다.
4. 새 marker는 `<!-- orch-task:<target>:<record-id> -->`입니다. 기존 `<!-- orch-inventory:... -->`는 유지합니다. 생성 전·응답 유실 후에는 모든 페이지에서 marker를 확인해 중복을 피합니다.
5. 이슈·프로젝트 URL, record key, 완료 책임을 계획과 지시서 **본문**의 GitHub tracking에 넣습니다. TOML 앞머리에 미지원 issue/project 필드를 추가하거나 DB 이관을 가정하지 않습니다.

## 갱신 시점

| 기존 단계·통지 | GitHub에 반영할 내용 |
| --- | --- |
| 계획 확정·spec/prepare | 승인 범위, task/record 연결, 그룹, 선행 조건, 미착수/준비 중 |
| dispatch/next/run | 실제 예약·시작과 대기 사유. 백그라운드 실행의 턴 종료 전에 시작 반영 |
| run 완료 통지 | 결과 상태·짧은 미결 사항·다음 판정. 통지 exit code만으로 판정하지 않음 |
| gate/review save | 검사 결과, 리뷰 대기·판정·triage, 다음 행동 |
| pr/update/수정 요구 | PR URL·공개 HEAD·CI·리뷰·충돌. 기존 review packet 유지 |
| wait 완료·close | 머지 근거와 orch close의 closed 확인 뒤 해당 범위가 끝난 이슈만 완료 |
| retry/resume/kill | 같은 task의 이슈 유지, 회차·정지/재개 사유. GitHub에서 승인을 추론하지 않음 |
| discard→재분해 | 이전 이슈 not_planned/Cancelled, 후속은 새 record로 연결하고 상호 참조 |
| 측정·하네스 | 검증·수락 근거와 PR. orch task가 없어도 추적 |
| 세션·작업 묶음 종료 | pending 재대조, 미동기화와 다음 행동 보고 |

새 폴링·상주 처리를 추가하지 않습니다. 기존 완료 통지·사용자 입력으로 메인 턴이 열릴 때 갱신합니다. 메인 부재 중 즉시 동기화는 보장하지 않으며 다음 턴·세션에서 복구합니다.

## 상태와 종료

| 원본에서 확인한 상황 | Project Status | 이슈 |
| --- | --- | --- |
| 승인된 미착수, slot만 기다리는 미착수 queued | Todo | open |
| prepared/reserved/running/judging/gating/reviewing/ready/pr_open, 해결 중 conflict | In Progress | open |
| 의존 미충족, 실제 테스트·리뷰 실패 | Blocked | open |
| 사용자 판단, quota/auth, 승인·계획 수정 필요 | Needs Decision | open |
| 의도적 연기 | On Hold | open |
| 과거 근거·외부 설정의 재확인 | Needs Verification | open |
| task 소유: 머지 후 orch close에서 closed 확인 | Done | closed / completed |
| 하네스·측정·집계: 정한 범위의 수락 근거 확인 | Done | closed / completed |
| discarded | Cancelled | closed / not_planned |

대기 사유가 기본 대응보다 우선합니다. 관리 블록에 정확한 local state·reason·다음 행동을 함께 씁니다. DONE 보고·게이트 통과·ready·PR 생성은 완료가 아닙니다. 머지되지 않은 closed PR도 완료가 아닙니다.

집계 이슈는 자식 task/PR 하나만으로 닫지 않습니다. 보통 재시도는 같은 이슈를 쓰고 폐기 후 새 id는 별도 record·이슈로 연결합니다. 새 기록에는 이전 이슈를, 이전 기록에는 후속을 남깁니다. 현재 CLI가 미지원 replaces 필드를 처리한다고 가정하지 말고 본문·대응표에 저장합니다.

PR에 Closes/Fixes 등 자동 종료 키워드를 넣지 않습니다. 이슈·PR은 참조 댓글로 연결하고 생성된 review packet은 수정하지 않습니다. 메인이 직접 쓰는 하네스 PR도 `Refs`로 참조합니다.

## API와 메인 관리 블록

인증은 config/main.md의 App 봇만 사용합니다. 대상 Issues·조직 Projects의 실제 접근을 확인하고 공개 조회만으로 쓰기 권한을 판단하지 않습니다. 발급 스크립트의 토큰은 해당 gh 환경에만 전달하며 표시·파일 저장·서브 전달을 하지 않습니다. 개인 인증으로 바꾸지 않습니다.

요청 JSON·댓글은 scratchpad 파일로 작성합니다. GraphQL은 `gh api graphql --input <request.json>`, 댓글은 `--body-file <path>`를 씁니다. 긴 heredoc·토큰 인자는 쓰지 않습니다. 훅이 거부하면 형태를 바꿔 재실행하지 않습니다.

쓰기 전 등록 repository와 issue repository, project owner·대상 연결을 확인합니다. GraphQL ID도 URL에서 조회해 확인하고 다른 대상의 ID를 재사용하지 않습니다. 권한 부족은 설정·계획 문제로 보고합니다.

사용자 본문은 보존하고 아래 블록만 갱신합니다. 근거가 변한 주요 사건은 짧은 참조 댓글로 남길 수 있습니다. 같은 내용·HEAD를 반복 댓글로 남기지 않습니다.

```markdown
<!-- orch-progress:begin -->
Task: <id or harness record>
State: <exact local state and reason>
Next: <next action>
PR: <URL if available>
Evidence: <public commit, test counts, recorded decision>
<!-- orch-progress:end -->
```

요약은 전송 전에 기존 redact 경로로 확인합니다. 코드 대량 복사, 원문 로그·세션·설정·비밀·실제 개인 경로를 보내지 않습니다. 차단되는 내용은 보내지 않고 항목명·상대 경로·수치·공개 링크로 제한하며 미전송 사유를 기록합니다.

## 쓰기 확인과 실패 복구

1. 원본에서 최신 desired를 계산해 revision을 올리고 pending을 먼저 저장합니다. 쓰기 전에 대상·완료 책임을 확인합니다.
2. 기존 이슈를 조회하고 관리 블록만 수정합니다. 사용자 추가 내용을 보존하며 오래된 본문 전체로 덮어쓰지 않습니다.
3. 프로젝트 항목을 **명시적으로 추가**하고 기존 membership/item을 재사용합니다. 생성 시 project 인자가 성공한 듯 보여도 소속 확인의 근거가 아닙니다.
4. 종료 시 issue state·reason을 먼저 갱신합니다. project의 close 자동화가 Done을 붙인 뒤 필요한 Cancelled를 재적용하고 최종 값을 확인합니다. 자동화 설정을 임의로 끄지 않습니다.
5. issue state/reason·membership·Status를 다시 읽습니다. 초기 등록·그룹 변경은 개요와 저장소 연결도 확인합니다. revision과 원하는 값이 모두 맞아야 pending을 지우고 last_verified_at을 남깁니다.
6. timeout/502는 쓰기가 성공하고 응답만 유실된 경우일 수 있습니다. 생성부터 반복하지 말고 marker·대상·현재 값을 먼저 대조합니다. 여러 페이지면 전부 확인합니다.
7. 실패·권한 부족·불일치는 비밀 없는 오류 종류만 기록하고 pending을 유지합니다. task/gate 결과를 바꾸지 않고 `GitHub tracking pending`을 보고합니다. 인증·안전 훅의 기존 중단 규칙을 유지하며 quota가 나도 다른 제공자로 자동 위임하지 않습니다.
8. 다음 메인 턴·세션에서 오래된 desired를 그대로 재생하지 않고 현재 원본에서 다시 계산합니다. 성공한 쓰기는 재사용합니다. pending이 남아 있으면 전체 동기화 완료라고 보고하지 않습니다.

## 허브의 기존 기록

`docs/github-tracking.json`은 8개 그룹, 기존 336개 이슈와 이번 정책 이슈, 백로그 별명 64개, 등록된 task 대응을 복구하는 seed입니다. Status는 넣지 않고 현재 근거로 판단합니다. 가장 오래된 그룹은 기존 project #3, Codex 그룹은 기존 project #2를 계속 씁니다.

적용 순서는 정책 PR 머지 → 허브 clone 갱신 → 최신 규칙을 읽는 새 `orch open`입니다. 공개 seed만으로 이미 실행 중인 Claude 세션이나 CLI 동작이 바뀌지는 않습니다.
