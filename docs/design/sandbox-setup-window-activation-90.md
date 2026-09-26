# 수정 90: 전체 액세스 상태의 샌드박스 설치와 창 활성화

## 확인한 원인

새 Windows PC의 한 프로필에서 `sandbox_mode = "danger-full-access"`와
`windows.sandbox = "elevated"`를 함께 사용했다. 샌드박스 설치가 안 된 상태에서
`windowsSandbox/setupStart`를 호출하면 설치용 권한 변환이 다음 오류로 끝났다.

> only managed permission profiles can be enforced by the Windows sandbox

`core/src/windows_sandbox.rs`의 설치 경로가 현재 작업의 permission profile을
`ResolvedWindowsSandboxPermissions`로 변환한다. 전체 액세스는 이 변환의 대상이
아니므로 UAC나 실제 설치에 도달하기 전에 실패했다. 독립된 임시 CODEX_HOME과
현재 배포 런타임에서 같은 실패를 재현했다. 설치 파일 누락과는 다른 문제다.

## 설치 호환 처리

`scripts/manager_core/windows_sandbox_setup.py`를 로컬 `runtime_proxy.py`에 연결했다.
사용자가 명시적으로 요청하고 런타임이 수락한 설치가 위의 정확한 오류로 실패한
경우에만 별도 app-server로 한 번 재시도한다. 그 설치 프로세스에만
`-c sandbox_mode="workspace-write"`를 전달한다.

- 실행 중인 작업 런타임의 권한이나 저장된 `sandbox_mode`를 변경하지 않는다.
- 기존 네이티브 설치 절차, 요구 사항 검사, UAC 승인을 그대로 거친다.
- 다른 오류·UAC 취소·정상 설치에는 재시도하지 않는다. 진행 중 중복 요청을 거절한다.
- 설치 보조 프로세스는 초기화와 설치 RPC만 보낸다. 모델 호출이나 작업 실행은 없다.
- 완료 응답을 확인해야 성공으로 알린다. 보조 프로세스의 stderr와 환경 값은 로그에 쓰지 않는다.
- 시간 초과는 성공으로 간주하지 않는다. 설치 결과와 Windows 권한 창을 확인하기 전
  강제 종료하거나 반복 실행하지 않는다. UAC 설치 프로세스를 강제 종료하지 않는다.

이 처리는 배포된 런타임을 위한 Windows 연결 계층의 호환 수정이다. Rust 샌드박스의
일반 실행 권한 검사 자체를 완화하거나 다른 바이너리로 교체한 것은 아니다.

## 창 활성화

독립 Codex 창을 클릭했을 때 관리 창을 바로 뒤로 이동시키는 작업이 기존에는
Background 우선순위의 전체 레이아웃 갱신을 기다렸다.
`NativeWindowHost.QueueZOrder`로 활성화·창 순서 이벤트를 별도 큐에 모으고
Normal 우선순위에서 작은 창 순서 작업만 실행한다. 실행 시점의 전경 창과
연결 수명을 다시 확인하고, 연결 해제 시 예약 작업을 취소한다.

관리 창 배치 실패를 무시하지 않고 진단과 재시도 대상으로 남긴다. 기존의
독립 입력 큐, 모달 창 뒤 배치, 비활성 프로필 숨김, 키보드 포커스 유지,
TOPMOST 금지 원칙은 유지한다.

## 검증과 한계

2026-09-24 확인:

- 해당 실제 프로필의 네이티브 설치 완료 응답 `success: true`.
- 새 app-server에서 `windowsSandbox/readiness`가 `ready`를 반환.
- 저장된 실제 작업 권한은 `danger-full-access` 유지.
- 두 SSH 연결은 Windows SSH 실행기 → 관리 WebSocket 경로에서 초기화,
  해당 계정 로그인 유형 확인, 작업 목록 조회 성공. 사용자도 화면 정상 연결 확인.
  이번 검사는 새 모델 답변을 생성하지 않았다. 새로운 SSH 원인은 재현되지 않았다.
- 설치 호환 처리 단위 검사 4개, 런타임 프록시 검사 7개, 권한 유지 검사 18개 통과.
- Windows 네이티브 창 검사 21개 항목 통과: 다른 창을 사이에 놓은 상태에서
  Background 레이아웃을 기다리지 않고 관리 창을 복구하는 경우 포함.
- 초기 네이티브 검사에서 사용자의 외부 앱 전환을 포커스 탈취로 오인했다.
  별도 시험 창으로 포커스를 가져왔는지 판별하도록 검사 조건을 보완한 뒤 통과했다.

실제 Chrome과 Codex 사이의 반복 클릭, 장시간 한글 입력 등 모든 사용자 동작을
자동 검증한 것은 아니다. 수정된 창 코드는 새 Shell을 열어야 적용된다.
이미 설치를 복구한 프로필은 설정 화면을 새로 열거나 그 프로필을 다시 열어
완료 상태를 확인할 수 있다. 실행 중 앱과 원격 작업을 이 빌드가 자동 종료하지 않는다.
