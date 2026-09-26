# Windows 실행 권한 선택 (수정 85)

수정 86에서 기존 일반 서비스가 남은 경우의 전환 흐름과 Windows pipe 소유자 검사를 보완했다. 현재 사용 방법과 실제 UAC 검증 결과는 [관리자 전환 복구](administrator-transition-86.md)를 먼저 참고한다. 아래 검증 상태는 수정 85를 게시한 당시의 기록이다.

## 문제와 동작

Codex에서 전체 액세스를 선택해도 Windows 프로세스의 UAC 토큰은 바뀌지 않는다. 일반 권한으로 실행된 관리 서비스와 그 자식 프로세스에서는 Hyper-V 관리 등 Windows 권한 검사가 계속 실패할 수 있다. 관리창만 나중에 관리자 권한으로 실행해도 남아 있는 서비스의 권한은 변하지 않는다.

- `설정 및 관리 → Windows 실행 권한…`은 `work/control-center/workspace-launch.json`의 다음 실행 설정만 저장한다. 기본값은 일반 실행이다.
- 왼쪽 아래에 관리창·서비스의 실제 토큰 권한을 표시한다. 설정값을 현재 권한으로 표시하지 않는다.
- `Open-Control-Center-Admin.cmd`는 수정 85 이상의 기존 빌드를 선택해 관리자 실행을 요청한다. 패키지 설치나 빌드는 하지 않는다. 이 실행기는 저장된 기본값을 바꾸지 않는다.
- 새 실행은 `ShellExecute`의 `runas`로 UAC 승인을 요청한다. 취소하면 실행을 종료하며 기존 작업은 유지한다. UAC에서 다른 Windows 계정을 사용하면 데이터에 접근하기 전에 거절한다.
- 이미 열린 관리창은 새로 만들지 않고 그 창으로 이동한다. 관리자 실행기를 사용했다면 현재 권한 확인과 완전 종료 후 재실행 방법을 안내한다.
- 기존 서비스가 다른 권한이면 서비스 조회·업데이트·종료 요청 전에 연결을 거절한다. 기존 서비스를 강제 교체하지 않는다.
- 서비스도 첫 요청을 읽기 전에 연결한 프로세스의 OS 토큰을 검사한다. 사용자나 관리자 권한이 다르면 연결만 닫는다. 관리자 서비스의 pipe에는 높은 무결성 수준의 쓰기 보호를 지정한다.
- 설정 변경은 작업을 마친 뒤 완전 종료하고 다시 실행하여 적용한다. 일반 모드는 일반 권한의 탐색기에서 시작해야 하며, 관리자 셸에서 시작하면 그 권한을 상속한다.

이는 같은 Windows 계정의 관리자 실행 기능이다. SSH의 sudo, 다른 계정의 인증, Windows 정책으로 거절된 별도 권한을 자동으로 바꾸는 기능은 아니다. 기존 프로필 프로세스의 권한도 실행 중에 바꾸지 않는다.

## 코드 경로

| 파일 | 책임 |
|---|---|
| `manager/Shell/WorkspaceExecutionMode.cs` | 버전 1 설정 읽기·원자적 저장, UAC 실행 인수, 원래 Windows 사용자 확인 |
| `manager/Shell/App.xaml.cs` | 단일 창 전달, 필요 시 UAC 실행, 최신 빌드 이동 시 사용자·알림 인수 유지 |
| `manager/Shell/Dialogs.ExecutionMode.cs`, `MainWindow.cs` | 다음 실행 설정과 현재 권한 표시 |
| `manager/Shared/WindowsExecutionIdentity.cs` | 현재 프로세스와 실제 named pipe 서버 PID의 `TokenUser`·`TokenElevation` 조회 |
| `manager/Shared/ManagerClient.cs` | 첫 RPC 전에 같은 사용자·같은 권한인지 확인; 실패 시 연결만 해제 |
| `manager/service/src/windows.rs`, `main.rs::serve` | 서비스에서 실제 pipe 클라이언트의 토큰 검사, 무결성 수준별 pipe 쓰기 보호 |
| `scripts/start-manager-admin.ps1` | 현재 빌드 경로와 수정 번호 확인, 앱 실행 |

서비스가 JSON으로 보내는 PID나 `elevated` 값은 권한 증거로 쓰지 않는다. Windows의 `GetNamedPipeServerProcessId`로 연결 상대를 찾고 그 프로세스 토큰을 읽는다. 서비스에서는 `GetNamedPipeClientProcessId`로 같은 검사를 수행한다. 토큰 조회에 실패하면 권한 불명으로 거절한다. IPC 형식은 27을 유지하며 이전 일반 서비스도 OS 권한이 일치하면 기존 호환 규칙으로 연결한다. 새 서비스의 보호까지 적용하려면 완전 종료 후 새 릴리스로 시작해야 한다.

`--expected-user-sid`는 앱이 요청한 UAC 전환에서 다른 계정 선택을 감지하는 인수다. 외부 실행을 인증하는 비밀 토큰은 아니다. 실제 연결 권한 검사는 기존 사용자별 pipe ACL과 OS 토큰 비교가 담당한다.

설정 파일이 손상되면 원하는 권한을 추측해서 실행하지 않고 시작을 중단한다. 복구하려면 설정 파일만 따로 보관한 뒤 `{"version":1,"administrator":false}`로 수정하고 일반 권한에서 다시 실행한다. 이 설정 복구는 서비스나 프로필을 종료하지 않는다. UAC 취소로 기본 설정이 해제되지는 않는다.

## 검증과 적용 상태

2026-09-21 22:51 KST, 사용자의 업데이트 요청에 따라 수정 85를 빌드하고 `artifacts/manager/current.json`이 `releases/20260921-135127-810`을 선택하도록 게시했다. 빌드·번들 생성은 통과했다. 실행 중인 관리창과 서비스는 계속 수정 84이며 자동 종료하거나 재실행하지 않았다.

- Rust 서비스 단위·fixture 검사: **30개 통과**. 실제 임시 pipe의 토큰 조회, 권한 불일치 거절과 pipe 보안 설정을 포함한다.
- .NET 임시 서비스 통합 검사: **132개 통과**, 실행 권한 설정·UAC 실행 인수 검사 **별도 19개 통과**.
- 관리자 실행기 PowerShell 구문 검사: 오류 없음.

적용 순서: 진행 중인 작업을 마친 뒤 **완전 종료 → `Open-Control-Center-Admin.cmd` → Windows 권한 허용**. 실행 후 왼쪽 아래에 관리창·서비스 모두 `관리자`로 표시되는지 확인한다. 이후에도 관리자 모드를 기본으로 쓰려면 `설정 및 관리 → Windows 실행 권한…`에서 체크하고 저장한다. 기본 설정 자체는 이번 빌드에서 바꾸지 않았다.

별도 시험은 임시 폴더와 임시 pipe/서비스만 사용한다. 실제 UAC 승인·취소, 관리자 모드의 Hyper-V 조회, 서로 다른 권한으로 실행된 실제 앱 사이의 전환, 새 Windows 복원은 실행하지 않았다. 운영 중 작업·서비스는 중단하지 않았다. 최신 시험 결과는 [HANDOFF](../HANDOFF.md)의 검증 표에 기록한다.

`manager/NativeHost.LogicTests`는 기존 테스트 대역의 누락된 타입 때문에 이 조사 시점에 빌드되지 않았다. 이번 권한 설정 시험은 `manager/Supervisor.Tests`에 추가했다. 이 기존 실패를 창 동작 검증 완료로 취급하지 않는다.

## Windows 참고 문서

UAC는 승인 후 별도 프로세스의 토큰을 통해 권한을 부여한다. [Microsoft UAC 구조](https://learn.microsoft.com/en-us/windows-server/security/user-account-control/how-user-account-control-works)

실행 요청은 `ShellExecuteEx`의 `runas` 동작을 사용하고, 실제 권한은 토큰 조회로 확인한다. [ShellExecuteExW](https://learn.microsoft.com/en-us/windows/win32/api/shellapi/nf-shellapi-shellexecuteexw), [GetTokenInformation](https://learn.microsoft.com/en-us/windows/win32/api/securitybaseapi/nf-securitybaseapi-gettokeninformation)

서비스의 연결 상대 조회와 pipe 무결성 정책: [GetNamedPipeClientProcessId](https://learn.microsoft.com/en-us/windows/win32/api/winbase/nf-winbase-getnamedpipeclientprocessid), [Mandatory Integrity Control](https://learn.microsoft.com/en-us/windows/win32/secauthz/mandatory-integrity-control).
