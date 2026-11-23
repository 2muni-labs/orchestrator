# 테스트 시간 측정 (`test-time`)

전체 테스트 시간을 재고 병렬 실행(`pytest-xdist`)을 시험한 기록입니다. 관련 이슈: #374(B33).

- 측정: 2026-11-23 UTC, 허브 `main` `0d155f0`, 설치된 orch v0.22.0, 메인 에이전트
- 환경: 12코어 맥, 모든 실행은 `orch sandbox run`(게이트 프로필) 아래
- 테스트: 6604개 수집, 통과 6539, 건너뜀 65(모두 "already inside a sandbox" 사유의 실제 샌드박스 테스트)

## 재현 방법

```
orch sandbox run --dir <worktree> -- .venv/bin/python -m pytest -rs -q -p no:cacheprovider -n0 --durations=0 --durations-min=0.05
orch sandbox run --dir <worktree> -- .venv/bin/python -m pytest -q -p no:cacheprovider -n0 tests/<group>
orch sandbox run --dir <worktree> -- .venv/bin/python -m pytest -v -rs -p no:cacheprovider -n <4|auto>
```

측정 당시에는 `addopts`가 없어 `-n0` 없이 한 프로세스로 돌았습니다. 소요 시간은 명령 전후의 벽시계 시간입니다.

## 기준 (한 프로세스)

전체 1236초(pytest 보고 1210초). 묶음별로 따로 돌린 벽시계 시간:

| 묶음 | 초 | 묶음 | 초 |
| --- | --- | --- | --- |
| integration | 256 | schedule | 53 |
| review_pr | 255 | track | 32 |
| gate | 233 | agents | 16 |
| cli | 195 | ops | 16 |
| execute | 72 | session | 9 |
| | | core | 5 |

0.05초 이상인 단계만 합하면 setup 485초, call 684초입니다. setup은 대부분 git 저장소를 만드는 fixture입니다(파일별 setup 합: `test_prflow` 108초, `test_gitops` 79초, `test_cli` 67초, `test_gate` 53초, `test_lifecycle` 42초, `test_runner` 35초). 가장 느린 개별 테스트는 시간 제한과 프로세스 정리를 실제로 기다리는 integration 테스트(3~12초)입니다.

## 병렬 실행 (`pytest-xdist` 3.8.0)

| 설정 | 1회 | 2회 | 3회 |
| --- | --- | --- | --- |
| `-n 4` | 618초 | 584초 | 585초 |
| `-n auto` (12 worker) | 351초 | 349초 | 341초 |

여섯 번 모두 통과 6539, 건너뜀 65였고, 테스트별 결과 집합이 서로 같았습니다. 허용 사유가 아닌 건너뜀은 없었습니다. 이 결과로 `pyproject.toml`에 `addopts = "-n auto"`를 두었습니다.

## 중복 테스트

테스트 함수 본문(독스트링 제외), 인자, 데코레이터를 AST로 비교해 완전히 같은 쌍을 찾았습니다. 한 쌍뿐이었고(`tests/agents/test_signature_rules.py`의 `test_only_a_creation_time_verifies`와 `test_no_issuer_subpackets_at_all_verifies`), 뒤의 것을 지웠습니다. 시간에는 영향이 없습니다. 그 밖의 느린 테스트는 같은 경로와 입력을 다른 테스트가 이미 단언한다는 근거가 없어 지우지 않았습니다.

## 남은 일

- git 저장소 fixture를 세션마다 한 번 만들어 복사하는 방식은 setup 시간을 줄일 수 있습니다. 공통 도우미를 바꾸는 일이라 따로 계획합니다.
- CI(Linux)의 병렬 실행 시간과 안정성은 이 PR의 CI에서 처음 확인합니다.
