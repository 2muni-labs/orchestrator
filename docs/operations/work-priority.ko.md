# 작업 우선도와 의존관계 운영

[English](work-priority.md) | 한국어

이 문서는 메인 에이전트의 계획·기록 규칙과 GitHub 운영 제안을 구분한다.
기준은 2026-11-18의 소스 `1c191614` 및 같은 날 조회한 orch Projects #2–9이다.
소스 상태는 [구현 기능](../reference/features.ko.md), 실행 절차는
[config/main.md](../../config/main.md)를 함께 읽는다.

## 확인 결과

| 영역 | 현재 동작 | 부족한 부분 |
| --- | --- | --- |
| task spec / DB | `depends_on`에 task id를 저장 | 사업 우선도 필드 없음 |
| scheduler | 선행 task가 `closed`여야 예약; 폐기된 선행 작업은 차단 | 외부 이슈·설치·측정 조건은 검사하지 않음 |
| `orch next` | queued 작업을 생성 순서로 검사 | Priority 정렬·자동 선점 없음 |
| TODO / status | 실행 상태와 일부 차단 사유 표시 | 전체 의존 DAG·우선도 표시는 없음 |
| GitHub Projects | orch 8개 프로젝트에 Status 있음 | Priority 필드 없음 |
| tracking | issue/project 대응표·동기화 엔진 존재 | 우선도/의존관계 동기화와 서비스 연결 미완료 |

`models.Severity.P0` 등은 리뷰 결함의 심각도이며 작업의 사업 우선도가 아니다.
L1–L3R은 위험도/검증 분류다. 세 값을 서로 복사하지 않는다.

## 이번에 적용하는 기록 규칙

계획은 `R/plans/<bundle-id>.md`에 [템플릿](../../templates/work-plan.md)으로
남긴다. `R`은 `~/.orch/<target>`이다. 메인 에이전트가 디렉터리를 만들고 관리한다.
현재 CLI가 이 파일을 생성·검증하지는 않는다. 새 묶음과 다음에 다시 계획하는
기존 작업부터 적용하며, 과거 완료 작업 전체에 임의의 우선도를 소급하지 않는다.

필수 항목은 record/task id, 대상 issue/project, 우선도와 이유, 착수 가능 여부,
선행 작업, 선행 조건 충족 증거, 후속 작업, 검토 날짜, 변경 이력이다.
의존성이 없으면 `none`, 아직 모르면 `unknown`으로 기록한다.
`unknown`인 필수 조건은 착수 전에 해소한다.

- **실행 의존성:** 같은 실행 체계에 등록된 task id를 TOML `depends_on`에 기입한다.
  task 본문의 목록도 같아야 한다. 선행 작업의 `closed`를 scheduler가 검사한다.
- **외부 필수 조건:** 이슈 URL, 결정, 측정, 버전 설치, 실제 운용 인수 조건을 본문에
  적고 메인 에이전트가 증거를 확인한다. 이슈가 닫힌 것만으로 설치 완료로 간주하지 않는다.
- **선호 순서:** 반드시 기다릴 필요가 없는 작업은 soft ordering으로 기록한다.
  불필요하게 `depends_on`에 넣어 병렬 진행을 막지 않는다.
- **변경:** 계획 승인, dispatch 직전, 선행 작업 완료/폐기, 인계 시에 다시 확인한다.
  이전 값→새 값, 이유, 영향을 받는 후속 작업을 날짜와 함께 기록한다.
- **순환/누락:** A→B→A와 존재하지 않는 predecessor를 계획 검토에서 해결한다.
  외부 관계는 CLI가 검사하지 않으므로 확인 결과를 계획에 남긴다.

우선도·외부 조건의 원본은 로컬 계획이다. GitHub 이슈의 `Planning` 블록과
프로젝트 Priority는 그 사본이다. 실행 상태의 원본은 계속 `state.db`다.
사용자가 GitHub에서 우선도를 바꾸면 다음 계획 검토 때 근거와 함께 반영한다.
GitHub 쓰기 실패는 계획의 pending sync에 남겨 재시도하며 원본을 잃지 않는다.
자동 진행 블록 밖에 Planning을 작성하여 tracksync와 충돌하지 않게 한다.
생성 파일 `TODO.md`나 가져오기 스키마에 지원하지 않는 키를 직접 추가하지 않는다.

## GitHub 우선도 운영 제안

다음 설정은 제안이며 이번 문서 변경만으로 원격 필드나 전체 이슈를 변경하지 않는다.

| Priority | 의미 | 적용 예 |
| --- | --- | --- |
| P0 | 현재 발생한 장애·데이터 손실·확인된 심각한 안전 문제의 긴급 복구 | 실제 증거와 영향 범위가 확인된 문제 |
| P1 | 현재 목표의 필수 경로를 여는 작업 | 추적 기능 연결·출시, 5a 필수 조건 |
| P2 | 다음 목표 준비 또는 일반 개선 | 작업공간 사전 측정·설계 결정 |
| P3 | 후속 단계·조건부 개선 | v2/v3, 아직 필요성이 확인되지 않은 개선 |

P0는 프로젝트 전체에 붙이지 않는다. 보안 관련 제목이라는 이유만으로 P0/P1을
자동 부여하지 않는다. 높은 우선도의 결과가 막혀 있으면 첫 미충족 선행 조건을
찾아 그 작업의 우선도 상향 여부와 근거를 기록한다. 완료·취소는 선택 대상에서
제외하고 기존 우선도 이력은 보존한다.

프로젝트별로 동일한 `Priority` single-select(P0–P3)를 두고, Status는 그대로 둔다.
Priority는 아이템의 필드이므로 프로젝트 자체의 순위를 대신하지 않는다.
프로젝트 README 첫머리에 다음 표를 유지하여 프로젝트 선택에 사용한다.

| 항목 | 기록 값 |
| --- | --- |
| Focus rank | 현재 착수 후보 프로젝트의 순서 또는 maintenance/deferred |
| Outcome | 이번에 완료하려는 결과 |
| Next eligible issue | 선행 조건이 충족된 다음 이슈 |
| First blocker | 첫 미충족 조건과 담당/다음 확인 |
| Review date | 마지막 확인 날짜 |

필요하면 별도의 포트폴리오 Project에 그룹 대표 이슈를 한 개씩 등록하여 비교한다.
기존 그룹 프로젝트를 유지하고 동일 작업을 중복 이슈로 생성하지 않는다.
이슈의 필수 선후 관계는 Planning 블록에 URL로 먼저 기록한다. GitHub의 네이티브
blocked-by 관계를 추가할 때에는 해당 조직의 기능·App 권한을 실측한 후 같은
관계만 반영한다. parent/sub-issue는 분해 관계이므로 dependency로 대신 쓰지 않는다.

추천 뷰는 Ready(Priority 오름차순), Blocked(선행 조건 해결용), Needs Decision,
Needs Verification, Done이다. 현 Status에 Ready가 없으므로 Todo만으로 착수 가능을
판정하지 않는다. 이슈 Planning의 readiness를 확인하거나 별도의 Readiness 필드를
도입한다. 동기화 엔진이 쓰는 Status 옵션을 임의로 바꾸지 않는다.

## 현재 프로젝트 선택안

아래 순서는 이슈 수가 아니라 구축 절차의 선행 조건에 따른 제안이다.
이슈 OPEN은 미구현의 확정 증거가 아니다. 예를 들어 추적 테이블과 동기화 엔진은
이미 소스에 있으므로 연결 PR·실행 증거부터 확인한다.

| 그룹 / 프로젝트 | 권장 선택 | 먼저 확인할 일 |
| --- | --- | --- |
| [G05 / #7](https://github.com/orgs/2muni-labs/projects/7) | 1순위, 필수 경로 P1 | #495 CLI, #496 서비스, #497 출시, #498 절차 전환의 완료 증거와 남은 작업 |
| [G07 / #2](https://github.com/orgs/2muni-labs/projects/2) | 2순위 후보, P2 | #149 기준/정책부터 CU 단계별 선행 조건 확인; 실제로 5a를 여는 항목만 P1 검토 |
| [G06 / #8](https://github.com/orgs/2muni-labs/projects/8) | 3순위, 준비 P2 | #349–352 결정, #409–411 측정; 구현은 5a 종료 후 |
| [G01 / #3](https://github.com/orgs/2muni-labs/projects/3) | 유지보수 | 현 장애 또는 필수 경로를 막는 잔여 작업만 개별 상향 |
| [G02 / #4](https://github.com/orgs/2muni-labs/projects/4) | 유지보수 | PID/프로세스 정리·테스트 불안정 이슈는 현재 재현 근거로 판단 |
| [G03 / #5](https://github.com/orgs/2muni-labs/projects/5) | 유지보수 | sandbox 잔여 검증 중 5a 안전 전제를 막는 항목을 선별 |
| [G04 / #6](https://github.com/orgs/2muni-labs/projects/6) | 유지보수 | #398 복구 기록, #414–415 실환경 증거 확인 |
| [G08 / #9](https://github.com/orgs/2muni-labs/projects/9) | 후순위 P3 | v1 인수 후 외부 대상 확대·v2/v3 검토 |

G05 내부에서는 추적 기능의 이미 머지된 작업을 재구현하지 않고 CLI·서비스·출시
연결을 먼저 대조한다. 다음으로 FL, M17→M18→M19의 미충족 조건을 구축 절차와
맞춘다. #328의 실제 1주 무인 운용은 설치 이후의 관찰 기간이며 문서·테스트로
대체할 수 없다. G07 CU6의 2주 시험도 이 조건을 대신하지 않는다.

작업공간의 상세 제안은 [PR #506](https://github.com/2muni-labs/orchestrator/pull/506),
화면 추가안은 [PR #145](https://github.com/2muni-labs/orchestrator/pull/145)에 있다.
이 문서의 기준 시점에는 둘 다 OPEN이며 확정 구현으로 취급하지 않는다.

## 적용 순서와 후속 자동화

1. 미완료 이슈를 PR·task·설치 증거와 대조하여 readiness를 정한다.
2. 위 우선도 정의와 프로젝트 선택안을 검토한 뒤 각 프로젝트의 동일 필드와 뷰를 만든다.
3. 다음 실행 묶음부터 Priority/Planning을 채우고 read-back으로 확인한다.
4. 메인 에이전트는 ready 항목만 작은 묶음으로 승인·dispatch한다.
5. 매 묶음 종료 시 next eligible issue와 blocker를 갱신한다.

후속 코드 구현은 별도 작업으로 분해한다: task/DB 우선도 스키마 및 migration,
호환 기본값, priority→생성 시각→id의 결정적 정렬, TODO/status/show 출력 계약,
GitHub 필드 대응·권한·재시도·충돌 정책. 테스트에는 기존 DB/태스크 호환, 같은 우선도
정렬, 높은 우선도라도 dependency/lock/pause로 대기하는 경우, 실행 중인 작업의
비선점, GitHub 실패 시 실행 상태 보존을 포함한다. 이 구현 전에는 `orch next`가
GitHub 우선도를 따른다고 안내하지 않는다.

이 문서 작업과 Discussion 게시 추적: [이슈 #509](https://github.com/2muni-labs/orchestrator/issues/509).
