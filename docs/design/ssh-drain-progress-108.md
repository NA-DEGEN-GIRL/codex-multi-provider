# 수정 108 — SSH 종료 대기의 서버별 진행

## 확인한 결함

`UpdateHooks._reconcile_opened_remotes`는 첫 서버의 graceful drain이 끝나지
않으면 즉시 반환했다. 한 프로필에 여러 SSH 서버가 있어도 첫 서버 뒤에 있는
서버에는 종료 요청이 전달되지 않았다. 이전 완전 종료의 journal을 다음 앱
실행이 이어받은 경우에도 같은 경로를 반복한다. 모든 SSH 시작은 이 journal의
완료를 기다리므로, 사용자에게 연결 시간 초과처럼 보일 수 있다.

종료를 기다린다는 관찰만으로 원격에 실제 작업이 없다고 판단하면 안 된다.
`drain_requested`에는 이미 원격 종료 신호가 전달되었을 가능성이 있다.
이를 지우거나 기존 프로세스에 재접속을 허용하는 것으로 복구하지 않는다.

## 변경

- 한 서버가 대기 중이어도 나머지 서버에 정확한 프로세스 식별자로 종료를
  요청한다. 원격 helper의 기존 단일 신호 및 반복 관찰 규칙은 유지한다.
- 대기 서버를 모두 확인한 뒤 반환한다. 전체 종료 증명이 모일 때까지 준비,
  시작, 연결 게시, 프로필 연결 잠금 해제는 수행하지 않는다.
- 이미 종료된 서버는 다음 검사에서 다시 신호를 보내지 않는다.
- 관리창에 대기 중인 서버 이름과 원격 종료 대기라는 원인을 표시한다.
  SSH child도 이 상태를 알면 90초를 기다린 뒤 일반 연결 실패로 처리하지 않고
  `ssh_remote_drain_pending`을 반환한다.
- 실행 세대·정책·journal 소유권 검증은 각 원격 작업과 상태 저장 전에 유지한다.

## 검증 및 적용 범위

2026-10-04: Python 회귀 71개 통과. 대상은
`test_manager_remote_profile_drain`, `test_manager_ssh_connection_wait`,
`test_manager_local_first_remote`, `test_manager_ssh_deferred_settings`이다.
새 사례는 첫 서버만 작업 중인 경우 다른 서버가 종료되는지, 완료 서버를
반복 종료하지 않는지, 도중 정책 변경이 다음 서버 요청을 막는지 확인한다.
원격 응답 대기 중 완전 종료 요청이 들어오는 경쟁 조건도 추가하고, 마지막
변경 뒤 drain 검사 18개를 다시 통과했다.

후보: `artifacts/manager/releases/20261004-042100-114/candidate.json`.
Rust 서비스와 .NET 구성요소를 컴파일했다. 진단 계정의 Store 패키지 조회가
실패하여, 현재 사용 중인 desktop 사본의 파일 검증 및 adapter 일치 검사를
통과한 뒤 동일 사본을 사용하는 방식으로 후보를 패키징했다. 설치된 새 Store
버전의 호환 검증을 통과한 것으로 해석하면 안 된다. 고정된 후보 Python
모듈에서 핵심 재현 검사 3개와 bundle 파일 해시 검사를 통과했다. 증거는
후보 폴더의 `staging-evidence.json`, `ssh-drain-validation.json`에 있다.
`artifacts/manager/current.json`은 변경하지 않았다.

이는 원격 작업을 강제로 끝내거나 이미 남은 종료 대기를 없애는 수정이 아니다.
실제 서버 접속 검사는 진단 실행 계정의 Windows SSH 설정 파일 접근 거부로
완료하지 못했다. 실행 중인 서비스·프로필·원격 프로세스를 교체하지 않았으며,
후보 빌드와 실사용 적용 여부를 별도로 확인해야 한다. 수정 107의 별도 실험
워크트리는 변경하지 않았다.

## 별도 확인 사항 — 이 수정으로 해결했다고 보고하지 말 것

- Claude SSH: 기본 Linux artifact에는 Claude 계정 인증·실행 프리셋 기능
  표시가 없고, 해당 기능이 있는 `artifacts/remote/candidates/claude105`는
  기본 선택 경로 밖에 있다. 관리창만 다시 열어서는 이를 활성화하지 않는다.
  [103](ssh-execution-presets-103.md), [105](claude-ssh-enrollment-105.md)의
  검증된 Windows/Linux 런타임 적용 절차와 실제 연결 검사가 남아 있다.
- Claude 프로젝트 신뢰: 다른 프로필에서 실행했던 작업이라고 해서 현재
  Claude 프로필의 project trust가 저장되지는 않는다. 원래의 명시적 신뢰
  확인을 생략하거나 모든 경로를 자동 신뢰하는 방식으로 우회하지 않는다.
- Pro $500 표시: 현재 로그인 메타데이터의 `promax`를 구버전 런타임의
  PlanType은 `unknown`으로 바꾸며, 관리용 desktop 26.917의 플랜 표시에도
  해당 등급 정의가 없다. 같은 바이너리의 재실행만으로 지원이 생기지 않는다.
  새 플랜의 API 및 화면 지원, 로그인 재조회는 후속 호환 작업이다.
  사용량 수치와 화면의 가격 표시를 같은 정보로 간주하지 않는다.
  이후 같은 수정 108에서 화면 전환을 막는 개인 플랜 분류와 토큰 파서를
  보정했다. 가격 표기 전체와 네이티브 런타임의 플랜 enum 갱신은 별도다.
  [추가 수정과 최종 빌드](plan-switch-and-full-exit-108.md).

공식 계정 조회와 갱신 계약:
[Codex App Server](https://learn.chatgpt.com/docs/app-server).
`account/read`의 `refreshToken`은 managed ChatGPT 인증에서 갱신을 요청하며,
외부 토큰 인증에서는 무시된다. 계정 표시를 이유로 로그아웃이나 전체 작업
종료를 자동 실행하지 않는다.
