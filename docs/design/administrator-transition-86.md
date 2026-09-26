# 관리자 실행 전환 복구 (수정 86)

## 확인한 원인

2026-09-22, 수정 85 관리창이 관리자 권한으로 시작할 때 `supervisor_access_denied`로 연결되지 않았다. 읽기 전용 조사에서 기존 수정 84 서비스는 같은 Windows 사용자의 **일반 권한**으로 계속 실행 중이었다. 이후 일반 권한으로 연 수정 85 창은 이 서비스에 정상 연결됐다. 실행 파일 업데이트와 실행 중인 서비스의 권한 변경은 별개다.

`NamedPipeClientStream`의 `CurrentUserOnly`는 Windows에서 실제 사용자의 `TokenUser`가 아니라 pipe 소유자와 `WindowsIdentity.Owner`를 비교한다. UAC 전후에 기본 소유자가 사용자에서 Administrators 그룹으로 달라질 수 있어, 실제 토큰을 검사하기도 전에 모호한 권한 오류가 발생했다. [해당 .NET 구현](https://github.com/dotnet/runtime/blob/main/src/libraries/System.IO.Pipes/src/System/IO/Pipes/NamedPipeClientStream.Windows.cs)의 `ValidateRemotePipeUser`와 별도 UAC 시험으로 확인했다.

## 최종 동작

- 관리 클라이언트는 실제 pipe 서버의 사용자 SID와 관리자 여부를 첫 RPC 전에 검사한다. `CurrentUserOnly`의 기본 소유자 비교는 쓰지 않는다. 클라이언트의 impersonation 수준은 Identification으로 제한한다. Rust 서비스의 사용자 DACL, 무결성 보호와 클라이언트 토큰 검사는 유지한다.
- `ManagerClient.ProbeExistingAuthorityAsync`는 기존 pipe의 OS 정보만 읽으며, 관리 서비스 시작·RPC·종료 요청을 하지 않는다. 읽기 실패는 서비스 없음으로 바꾸지 않는다.
- 일반 실행기에서 관리자 실행을 요청했지만 같은 사용자의 일반 서비스가 남아 있으면 기존 작업에 일반 권한으로 연결하고 **관리자 전환 대기**를 표시한다. 작업이나 서비스를 자동 종료하지 않는다.
- `설정 및 관리 → 완전 종료 후 관리자 실행…`은 작업 중단을 명시한 확인창을 표시한다. 기존 완전 종료 절차로 프로필·관리 서비스를 정리하고, 서비스 프로세스의 종료까지 확인한 뒤에만 UAC를 요청한다. 종료 확인 실패 시 창을 유지하고 관리자 실행을 보류한다.
- 새 관리창이 닫히는 창으로 잘못 전달되지 않도록 앱 종료 시 단일 창 mutex를 먼저 해제한다. UAC 취소 시 자동 재시도하지 않는다.
- 다음 실행의 기본 권한 설정은 별도다. 이 버튼이나 관리자 실행기는 저장된 기본값을 자동 변경하지 않는다.

이미 관리자 권한으로 직접 실행된 창은 기존 일반 서비스에 붙지 않는다. 기존 작업에 접근하려면 일반 실행기로 열고 위 전환 메뉴를 사용한다. 기존 권한의 프로세스를 실행 중에 승격하거나, 일반·관리자 서비스를 동시에 만들지 않는다.

## 검증과 배포 상태

- Shell 빌드: 경고 0, 오류 0.
- .NET 임시 서비스 통합 검사: **134개 통과**.
- 권한 설정·관리자 실행 보류 판단 검사: **별도 25개 통과**.
- 실제 Windows UAC 승인 후 임시 관리자 프로세스/서비스 검사: **8개 통과**. 기존 소유자 검사 오류 재현, 같은 사용자의 다른 권한 식별, 혼합 권한 연결 거절, 새 관리자 서비스 연결, 기존 일반 시험 서비스 보존을 확인했다. 보고서: Git 제외 경로 `work/administrator-transition-86.json`.
- 실제 운영 프로필 종료 및 메뉴를 통한 전체 앱 재시작은 수행하지 않았다. Hyper-V 조회도 이번 시험에 포함하지 않았다. 기존 운영 서비스와 작업은 유지했다.
- 빌드 `20260922-081659-020`을 수정 86의 다음 실행 파일로 게시했다. IPC는 27이다. 새 관리창은 기존 일반 서비스와 연결할 수 있으며 새 서비스 적용·권한 전환은 완전 종료 이후다.

실제 UAC 회귀 시험은 명시적으로 다음 명령을 실행할 때만 수행한다. 임시 경로에 합성 백엔드를 만들며, 원본 작업공간·계정·SSH를 대상으로 삼지 않는다.

```powershell
dotnet run --project manager/Supervisor.Tests/Codex.ControlCenter.Supervisor.Tests.csproj -c Release -- --verify-uac-transition --report "$env:TEMP\codex-uac-transition.json"
```

관련 파일: `manager/Shared/ManagerClient.cs`, `manager/Shell/{App.xaml,MainWindow,WorkspaceExecutionMode}.cs`, `manager/Supervisor.Tests/AuthorityTransitionTests.cs`. 이전 권한 모델: [수정 85 설계](windows-execution-mode-85.md).
