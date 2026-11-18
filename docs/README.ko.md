# orchestrator 문서

[English](README.md) | 한국어

`orch`의 현재 구현, 운영, 설계와 향후 계획을 구분하여 찾아볼 수 있는 문서 목차다.
구현 설명의 기준 커밋은 `b757a619`(2026-11-18), 패키지 선언 버전은 `0.15.0`이다.
같은 버전 문자열에서도 소스와 설치 바이너리가 다를 수 있으므로 배포 여부를 별도로 확인한다.

## 현재 구현과 운영

| 문서 | 내용 |
| --- | --- |
| [구현 기능과 CLI](reference/features.ko.md) | 사용 가능한 명령, 부분 구현, 아직 없는 기능 |
| [아키텍처와 데이터 흐름](reference/architecture.ko.md) | 모듈 경계, 상태 전이, DB, 실행·검증·GitHub 흐름 |
| [백엔드 환경과 운영](operations/runtime.ko.md) | Python·macOS·tmux, 설정, 저장소, 서비스, 검증·복구 |
| [우선도와 의존관계](operations/work-priority.ko.md) | 현황 진단, 필수 기록 규칙, 프로젝트 우선순위 제안 |
| [작업 계획 템플릿](../templates/work-plan.md) | 우선도·의존 조건·증거·변경 이력 |
| [태스크 템플릿](../templates/task.md) | 실행 spec과 메인 소유 Planning 항목 |

## 설계 원본

설계 문서는 요구사항을 정의하며, 그 내용이 전부 구현되었다는 뜻은 아니다.
충돌 시 적용 순서는 요구→기술→실행→보안→화면이며 구축 절차는 현재 단계의 범위를 정한다.

- [요건 정의](design/요건%20정의.md): 역할, 권한, 게이트, 작업공간 요구사항.
- [기술 설계](design/기술%20설계.md): 데이터 모델, 상태 기계, 명령·출력 계약.
- [실행 설계](design/실행%20설계.md): 레인, 디렉터리, 주입·운영 절차.
- [보안 설계](design/보안%20설계.md): 위협 모델, sandbox, 인증, 기기 분실 대응.
- [화면 설계](design/화면%20설계.md): 작업공간 UI와 기기별 인수 조건.
- [구축 절차](design/구축%20절차.md): 단계별 선행 조건과 진행 기록.
- [ADR 목차](decisions/README.md): 각 설계 결정의 이유와 변경 기록.
- [GitHub 복구 대응표](github-tracking.json): 검토된 그룹/issue 매핑 seed, 실행 상태 원본 아님.

## 진행 중인 제안

[PR #506](https://github.com/2muni-labs/orchestrator/pull/506)의 작업공간 구현 설계·계획과
[PR #145](https://github.com/2muni-labs/orchestrator/pull/145)의 화면 확장안은 기준 시점에
미머지 상태다. 머지 후 로컬 문서 링크로 연결하고 구현 상태를 다시 점검한다.

## 문서 유지 규칙

기능을 추가할 때 features의 상태, architecture의 소유 모듈, runtime의 설정/운용
변경을 같이 갱신한다. CLI 목록은 `src/orch/cli.py`의 parser와 대조한다.
설치되지 않은 소스 기능, 설계만 존재하는 명령, API 구현만 있는 기능은 구분한다.
실제 토큰·로그·개인 절대 경로를 문서 예제로 게시하지 않는다.
