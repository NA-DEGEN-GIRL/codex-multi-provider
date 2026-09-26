# 수정 92 — 상태 갱신 응답성과 SSH 내장 브라우저 포트 전달

## 측정한 증상

2026-09-24~25에 실제 로그와 `state.json` 사본으로 측정했다. 실행 중인 앱은 조작하지 않았다.

- `state` RPC(셸이 4초마다 호출): `rust-service.jsonl` 약 17,900건 기준 하한 약 650-700 ms,
  p90 약 1.5-1.7 s. 누수 전 하한은 약 285 ms였다. 기록이 늘어나던 동안에는 p50 2.2 s,
  20 s 시간 초과까지 있었다.
- `work/control-center/state.json`: 6.4 MB. 그중 약 5.4-5.9 MB가 한 프로필에 남은 SSH
  `native-proxy` 작업 기록 15,051개였다. 살아 있는 프로세스의 기록은 수십 개뿐이었다.
  파싱 1회 약 60 ms(정상 크기 약 1.5 ms), 전체 쓰기 1회 약 260-290 ms.
- `work/control-center/auth-probes/`: 로그인·사용량 검사 폴더 약 8,300개, 약 125 GB.
  폴더당 15-27 MB이며 분당 5-7개씩 늘었다.
- 첫 Windows 알림: 셸 프로세스마다 첫 알림에서 UI 스레드가 3-10 s 멈췄다(한 번은 9.6 s).
  최근 `ui_stall` 7건 중 6건이 이 경우였다.
- 실행 준비: 콜드 실행마다 PowerShell `Get-AppxPackage`(1-2 s)를 대개 두 번 실행했다.
  브라우저 구성요소 검사가 digest 캐시를 밀어내 실행마다 693 MB 데스크톱 복사본을 다시
  해시했다. 이미 실행 중인 프로필 클릭도 0.4-0.75 s 걸렸다(수정 88 전 12-15 ms).
- 프로필 표시: `profile.show` 응답 뒤 전체 `state`(0.8-2.1 s)를 기다린 다음 창을 연결했다.
- SSH 내장 Browser: SSH 프로젝트에서 원격 `http://127.0.0.1:<port>/`를 열면 빈 새 탭만
  생겼다. 데스크톱 로그에 `browser_sidebar.remote_localhost_port_forward_failed`
  (`code=125`) 26건, 같은 시간대 해당 프로필 `ssh-routing.jsonl`에 `blocked` /
  `unsupported_native_ssh_version` 기록 52건(전달 26, 취소 26)이 있었다.

## 원인

### 상태 저장소와 `state` RPC

- SSH shim은 실행마다 `ssh_inventory`에 작업을 등록하고 종료할 때 해제한다. 정리 코드가
  실행되기 전에 끝난 프록시의 기록은 원격 유지보수·업데이트 경로가 `coverage()`를 호출할
  때만 회수했으므로 그 사이 계속 쌓였다(시간당 약 2,200회 등록).
- `Store.read()`는 배타 잠금 아래 파일 전체를 파싱하고, 쓰기는 전체를 `indent=2`로 다시
  쓰고 fsync한다. `atomic_json`의 스트리밍 `json.dump(indent=2)`는 CPython 3.13에서
  C 인코더를 쓰지 않아 필요보다 3-4배 느렸다.
- `state()` 한 번이 저장소를 6번 읽었다. 셸이 읽지 않는 `ssh_inventory`(4.94 MB)를 포함한
  약 5 MB 응답을 Python → Rust → C#가 차례로 다시 파싱했다. 셸 메모리는 80-150 MB에서
  300-1,150 MB로 늘었다.
- `accounts.sync`는 8번째 poll마다 변경이 없어도 전체 쓰기를 3번 해 p90 급등(약 +800 ms)을
  만들었다. `RemoteUpdates.start()`는 첫 요청을 처리하기 전에 전체 쓰기를 24번 했다(사본 기준 8.7 s).
- 프로세스 식별용 Rust broker 호출은 응답을 10 ms sleep 간격으로 확인했다. `state()`는
  실행 중인 프로필마다 이 호출을 한다.
- 백그라운드 루프가 초당 4-6번 같은 문서를 파싱해 Python 백엔드가 평균 한 코어의 35%를 썼다.

### 로그인 검사와 실행 준비

- `login_probe.verify`는 검사마다 새 `auth-probes/<uuid>` CODEX_HOME에서 app-server를
  실행하고 폴더를 지우지 않았다. 사용량 확인은 프로필마다 60 s 간격이었고, 검사마다 캐시
  없는 PowerShell 패키지 조회도 실행했다.
- `desktop_launch.find_app()`은 호출마다 PowerShell을 실행한다. `Instances`의 5 s memo는
  실행 도중 만료되어 한 실행에서 조회를 두 번 했다.
- `browser_bundle`(수정 88)의 파일 해시가 공유 64칸 digest 캐시를 채워 데스크톱과 로그인
  런타임의 digest를 밀어냈다.

### 관리창

- 알림 toolkit의 1회성 앱 등록(시작 메뉴 바로가기 조회, 아이콘 저장, HKCU AUMID·COM
  activator 등록)이 첫 알림 때 UI 스레드에서 실행됐다.
- `ShowProfileCoreAsync`는 `TryAttach(returnedProfile)`가 반환된 프로필만 읽는데도 전체
  `state`를 먼저 기다렸다.

### SSH 내장 Browser

- 데스크톱은 SSH 대화의 원격 localhost URL을 열 때 `ssh <터미널 명령 꼬리> -N -L
  <local>:127.0.0.1:<remote>`와 고정 `-o` 5개로 OpenSSH 포트 전달을 만든다. 5 s 안에 로컬
  포트가 열리면 주소를 `localhost:<local>`로 바꾼다. 실패하면 `-O cancel -L <같은 spec>`을
  보내고 경고만 남긴 채 이동을 버린다. 빈 새 탭은 이 결과다.
- 관리 프로필의 `ssh`는 SshProxy → `ssh_shim.py`다. 설치된 데스크톱(26.915·26.917)의 SSH
  소스 해시가 `VERIFIED_SOURCES`(`ssh_compatibility.py`)에 없어 모든 프로필 binding이
  `native_compatible: false`였다. 이 모드의 `route_arguments`는 완전히 해석된 네이티브
  관리 명령만 받는다. 전달 명령은 `unsupported_native_ssh_version`으로 거절되어 exit 125로
  끝났고 실제 OpenSSH는 실행되지 않았다.
- 잠재 결함: 검증된 모드에서는 같은 명령이 `passthrough`로 통과하지만, `SshInventory.execution`이
  그 프로필을 `unclassified`로 표시하고 전달이 끝날 때까지 비관리 작업을 유지한다. 그러면
  원격 유지보수·업데이트의 coverage가 완료되지 않는다. `wait_for_settings`와 등록도
  데스크톱의 5 s 대기 안에서 `state.json`을 쓴다.
- 관리 데스크톱 패치, 수정 88의 Browser plugin 버전(모든 프로필 캐시가 데스크톱과 일치),
  config 플래그, 수정 46의 `enterprise_policy_unavailable`은 원인이 아니었다. 실패는 탐색 전
  ssh 실행 단계에서 났다.

## 변경

### 상태 저장소와 `state` RPC

`scripts/control_center.py`, `scripts/manager_core/store.py`, `accounts.py`, `remote_updates.py`,
`profile_restart.py`, `startup_updates.py`, `rust_service.py`

- `store.atomic_json`: `json.dumps(ensure_ascii=False, indent=2, allow_nan=False)`로 한 번
  만들어 UTF-8로 한 번 쓴다. 출력 바이트는 기존과 같다. mkstemp·fsync·`os.replace` 재시도는
  유지한다. 잘못된 문서는 임시 파일을 만들기 전에 실패하고 이전 파일은 그대로 남는다.
- `store.Unchanged(value)`: 연산이 이 값을 반환하면 `Store.mutate`가 revision 증가와 쓰기를
  생략한다. 이미 있는 계정의 `add_profile`, 변경 없는 `accounts.sync`, `RemoteUpdates.start()`의
  `checking` 정리(24번 → 필요할 때 1번), 회수 0건인 `ssh_inventory`에서 사용한다.
- `ControlCenter.state()`: poll마다 저장소를 한 번 읽고, 그 snapshot을
  `ProfileRestarts.status(state=)`, `StartupUpdates.status(state=, jobs=)`,
  `RemoteUpdates.status_all(state=)`, `UsageRefresh.schedule(profiles=)`에 넘긴다. 인수를
  생략하면 기존처럼 동작한다. 응답에서는 `ssh_inventory`만 뺀다. 저장소는 그대로 두며
  C#·Rust·Supervisor에는 이 값을 읽는 코드가 없다.
- `state` 단일 실행: 서비스 모드에서 `state`는 `_shared_state()`로 처리해 동시에 한 계산만
  실행한다. 새 요청은 진행 중 계산이 `STATE_SHARE_SECONDS`(2 s) 안에 시작했고 그 뒤 끝난
  다른 요청이 없을 때만(`_request_epoch`) 합류한다. 그 밖에는 진행 중 계산이 끝나기를 기다렸다가
  새로 계산한다. 완료된 결과는 캐시하지 않는다. 명령이 끝난 뒤 보낸 `state`는 그 명령 전
  snapshot을 받지 않는다.
- 오류 응답: `request()`의 예상 밖 예외와 응답 직렬화·UTF-8 인코딩 실패(짝 없는 surrogate
  포함)도 요청 id와 함께 `internal_error`로 답한다. 예외 내용은 응답에 넣지 않는다. 전에는
  응답이 없어 Rust 요청이 pending으로 남았고, 이후 완전 종료가 `backend_busy`로 거절될 수
  있었다. 응답은 LF로 끝나는 UTF-8 한 줄로 쓴다.
- `rust_service.request`: kernel32 prototype을 모듈에서 한 번 만든다. 응답 확인 간격을
  0.5 ms에서 두 배씩 10 ms까지 늘린다. 20 s 기한, 요청 형식, 오류 메시지는 같다.

### 누수 기록 회수

`scripts/manager_core/ssh_inventory.py`, `scripts/control_center.py`

- `SshInventory.reconcile_all(stopping=)`: 저장소를 한 번 읽고, 잠금 밖에서 모든 생존 확인을
  한 뒤, 한 번의 `mutate`로 관찰한 값과 아직 같은 작업만 지운다. 회수할 것이 없으면 쓰지 않는다.
- `start_sweeper(interval=60, delay=5)` / `stop_sweeper()`: `control_center.py --serve`만
  시작한다. 백엔드 시작 5 s 뒤 첫 회수를 하고 이후 60 s마다 반복한다. 종료 요청이 오면 진행 중
  탐색을 쓰기 없이 버린다. 일시적인 저장소 오류는 다음 주기에 다시 시도한다.

### 사용량 확인과 로그인 검사 폴더

`scripts/manager_core/usage_refresh.py`, `native_usage.py`, `login_probe.py`,
`manager/Shell/ProfileCardData.cs`, `MainWindow.cs`

- 백그라운드 사용량 확인 간격을 60 s에서 300 s(`native_usage.REFRESH_INTERVAL`)로 늘렸다.
  백엔드는 선택된 프로필을 모르므로 모든 프로필에 같은 값을 쓴다. `refresh_all`과 로그인
  확인은 이전처럼 바로 검사한다.
- 검사 결과의 `windows`·`reset_credits`가 쓰기 시점의 저장값과 같고, 그 저장값이 깨끗하며
  30분(`_REWRITE_AFTER`) 이내이면 `Unchanged`로 `state.json` 쓰기와 revision 증가를 생략하고
  메모리에만 둔다. 이 판단은 저장소 잠금 안에서 하므로, 검사 도중 저장된 stale·오류 표시는
  뒤이어 끝난 성공 결과로 대체된다. 쓰기를 생략해도 판단을 위해 잠금 안에서 저장소를 한 번
  읽는다. 저장값에 stale·오류 표시가 있으면 메모리 값에도 그 표시를 덧씌운다.
- 오래된 값 기준을 `STALE_AFTER = 2 × 300 s`로 올렸다. 카드와 선택 프로필의 "N분 전 값"도
  600 s부터 표시한다.
- `login_probe.verify`: app-server 종료와 auth.json 확인이 끝난 뒤 검사 폴더를 지운다. 시작에
  실패해도 지운다. 종료되지 않은 프로세스의 폴더는 남긴다. 읽기 전용 git pack 파일은 일반
  파일의 read-only 속성만 해제한다. 잠긴 파일은 0.2 s·0.4 s 간격으로 3번 시도한 뒤 정리
  작업에 넘긴다. 삭제 실패는 예외로 올리지 않는다(Python 3.11의 `onerror` 경로 포함).
- `purge_stale_probes`: 생성·수정 시각이 모두 1시간을 넘은 UUID 폴더만 하나씩 지우고 폴더
  사이에 50 ms 쉰다. 스레드 우선순위는 낮추지 않는다. Python 코드가 GIL을 잡고 실행되므로
  background 모드로 밀려나면 백엔드 요청 스레드도 함께 멈출 수 있다. `schedule()`이 root마다
  첫 호출 120 s 뒤 시작한다. 이전 정리가 끝났고 마지막 시작 뒤 6시간이 지났을 때만 다시
  시작한다(`_purge_due`).

### 패키지 조회와 digest 캐시

`scripts/desktop_launch.py`, `scripts/manager_core/instances.py`, `desktop_publication.py`,
`browser_bundle.py`, `current_account.py`, `native_login.py`

- `desktop_launch.cached_app()`: 실행 파일과 app.asar의 크기·mtime_ns가 같고, 180 s
  (`APP_CACHE_SECONDS`) 이내이며, Windows `GetPackagesByPackageFamily`가 보고하는 설치 버전
  목록이 같을 때만 결과를 재사용한다. 나머지는 PowerShell로 다시 조회한다. app.asar나 실행
  파일이 없는 결과는 저장하지 않고, 호출자마다 사본을 준다. `Instances.installed_app`(기존
  5 s memo 유지), 사용량 검사, 로그인 확인, 현재 계정 서버 확인이 이것을 쓴다.
  `forget_app()`으로 비울 수 있다.
- `desktop_publication._hash(cache=, limit=)`: 호출자가 별도 캐시를 쓸 수 있다.
  `browser_bundle`은 8,192칸 전용 LRU를 쓰므로 공유 64칸 캐시에는 데스크톱·로그인 런타임
  digest만 남는다. `GetFileInformationByHandleEx` prototype은 모듈에서 한 번 만든다.
- `browser_bundle.ensure`: 검증된 `ready` 결과를 (home, 실행 파일 경로·ino·크기·mtime,
  manifest SHA-256) 단위로 기억한다. 사용할 때마다 게시 파일의 크기·mtime·NTFS ChangeTime과
  대상 폴더 및 모든 하위 폴더의 (ino, mtime)을 다시 확인한다. 불일치, 사라진 폴더, reparse
  point가 있으면 수정 88의 전체 검사로 돌아간다. 모든 stamp가 1 s 이상 지난 경우에만 기억한다.

### 관리창

`manager/Shell/WorkspaceNotifications.cs`, `MainWindow.cs`, `WorkspaceBuild.cs`

- 알림 toolkit과 WinRT 알림 호출은 message loop가 있는 전용 STA 스레드 하나에서 실행한다.
  첫 화면 표시 뒤 `Warm()`이 등록과 notifier 생성을 미리 한다. UI 스레드는 1.5 s 클릭 가드,
  대상 창·프로필 확인, 프로필 alias 사본 생성만 한다. 작업 스레드는 `_state`를 읽지 않는다.
- 수락 판정(수정 40)은 유지한다. Windows `Show`가 반환된 뒤에만 true로 답한다. 기한은
  `ClickedAt + 1.5 s`로 Electron의 1.8 s 대체 알림보다 앞선다. 기한을 넘기면 false로 답하고,
  늦게 표시된 알림은 `Hide`와 `History.Remove`로 거둬 중복 알림을 막는다. URI 처리기
  레지스트리는 값이 다를 때만 쓴다(`Registered()`).
- 프로필 표시: `profile.show`/`profile.login`이 성공하면 반환된 프로필로 창을 먼저 연결한다.
  반환된 실행에는 새 25 s 연결 기한을 준다. 반환된 프로필은 프로필별로 `_shownProfiles`에
  두고, 그 뒤에 요청한 state가 적용될 때 지운다(자체 시험의 `UseFixture`와 종료 drain에서도
  지운다). 표시할 때마다 `_stateRevision`을 올리므로 적용된 state는 항상 보관된 값보다 나중에
  요청한 것이다.
- 실행 식별은 `Latest(id)`(보관된 값, 없으면 `_state`)로 판단한다. `TryAttach`, layout 재시도
  Render, `ReconcileBackgroundWindows`, 재클릭의 `ShowRecovery`/`IsReplacing`/기존 창 판단,
  `RecoverProfileAsync`의 `expected_generation`·PID, `LoginProfileAsync`의
  `RequiresRestartForLogin`, `RestartRemoteProfileAsync`의 `generation`, `DetachAsync`의
  `_detachedProfiles`가 이 값을 쓴다. 새 state가 오기 전에 다른 프로필로 바꿔도 background host는
  반환된 실행과 비교되므로 연결된 창이 분리되지 않는다.
- 프로필 동작 gate는 창 연결 직후 해제한다. 이어서 진행 중 poll이 끝나기를 기다렸다가, 보관된
  값이 남아 있으면 새 state를 받는다. `ShowProfileAsync`는 이 갱신까지 기다린다.
  `updating`/`opening` 결과와 오류 경로는 이전처럼 gate 안에서 state를 먼저 기다린다.
- 지표: `profile.attached.<id>`는 클릭부터 창 연결까지다. 연결이 뒤로 미뤄지면
  `AttachProfileWindow`가 성공할 때 기록하며 탐색마다 한 번만 남긴다.
  `profile.switch.refresh.<id>`는 표시 뒤 대기와 state 갱신이다. `profile.switch.<id>`는 그대로
  `ShowProfileAsync` 전체를 재므로 표시 뒤 state 갱신과 진행 중 poll의 남은 시간을 포함한다.
- `WorkspaceBuild.Revision = 92`, 설명 "상태 갱신·프로필 열기 속도 개선".

### SSH 내장 Browser 포트 전달

`scripts/manager_core/ssh_shim.py`

- `local_forward(arguments, manifest)`는 다음 형식만 받고 `{operation, alias}`를 반환한다.
  - 목적지 앞은 비어 있거나 정확히 `[-i <id>] [-p <숫자>]`
  - 목적지는 `ALIAS` 형식이며 이 manifest의 binding이나 `auto_prepare_aliases`에 있는 host
  - 꼬리는 정확히 `-N -L <1-65535>:127.0.0.1:<1-65535>`와 데스크톱의 `-o` 5개를 같은
    순서로 붙인 것(`local-forward`), 또는 `-O cancel -L <spec>`(`local-forward-cancel`)
- `route_arguments`는 설정 조회 검사 직후, `native_compatible is False` 분기 전에 이 검사를
  한다. 통과한 argv는 바꾸지 않고 실제 OpenSSH로 실행한다. 환경에서 `CODEX_MANAGER_*`를
  지우고 원래 PATH를 쓰는 기존 `_execute` 경로다.
- `main()`은 이 명령에서 `wait_for_settings`, manifest 재로드, `SshInventory` 등록을 건너뛴다.
  `-N`은 원격 명령을 실행하지 않으므로 원격 유지보수·drain(수정 56·89)과 겹치지 않는다.
  감사 기록에는 operation과 alias만 남기고 포트·URL은 쓰지 않는다.

### Codex 데스크톱 26.917 지원

설치된 Codex가 26.915.4065.0에서 26.917.9434.0으로 자동 업데이트되고 26.915 원본은
사라졌다. 기존 릴리스는 같은 adapter로 만든 26.915 관리용 복사본으로 대체 실행했지만,
adapter 파일(`desktop_publication.py`, `desktop_bundle.py`, `original_sync_bundle.py`)이
바뀐 새 빌드는 그 복사본을 쓸 수 없어 `prepare_manager_desktop.py`에서 "알림 클릭 연결
위치를 확인하지 못했습니다"로 중단됐다. 26.915 변형은 모두 유지하고 26.917 변형을 추가했다.

- 알림 클릭: 26.917은 알림 변수를 `d`로 바꿨다(`l`은 소리 설정). 클릭 hook은 변형 map으로
  바꾸고 정확히 한 변형이 한 번만 맞아야 한다.
- 알림 표시: 26.917은 Windows 알림을 무음으로 띄우고 closure `f`에서 앱 자체 소리를 재생한다.
  hook은 `f` 전체(표시+소리)를 대신한다. 관리 앱이 받으면 관리 알림의 Windows 기본 소리 하나만
  나고, 거절하면 원래 표시와 소리가 그대로 실행된다. 거절은 pipe 대기 뒤에 올 수 있으므로,
  26.917의 staged 경로와 같은 "아직 현재 알림인지·창이 닫혔는지" 확인을 거친 뒤에만 되돌린다.
  관리 알림은 Codex 소리 설정(`none`/`classic`)을 따르지 않는다(남은 문제).
- 추론 강도 UI(`desktop_reasoning_ui.py`): 26.917에서 effort 판별 함수 이름이 `vw`로 바뀌어
  찾지 못한 것을 "ambiguous"로 보고하던 문제다. 알 수 없는 이름은 "검증되지 않은 버전"으로
  보고한다.
- 보관 기록 숨김 수정(`renderer_host_identity_patches`): 26.917 이름표를 추가하고, 삽입하는
  catalog-enabled atom 선언(`Fw=Go(X,!1)`, 26.915 `AT=rf($,!1)`)이 정확히 한 번 있을 때만
  적용한다. 이 패치는 맞는 변형이 없으면 조용히 건너뛰므로 다음 버전에서도 확인이 필요하다.
- SSH native-start: 26.917은 시작 명령을 `{ … </dev/null >LOG 2>&1 & }`로 감쌌다. 이 형식을
  몰라 새 빌드의 모든 SSH 연결이 시작 단계에서 막힐 뻔했다. `native_body_variants`에 26.917
  본문을 정확한 두 번째 형식으로 추가했고 경로 변환 결과는 26.915와 같다. 26.917 fixture를
  추가했지만 `VERIFIED_SOURCES`에는 넣지 않았다(명령별 검사 유지).
- 원격 준비: 서버에 기존 `codex` CLI가 없어도 관리용 런타임 준비를 막지 않는다.
  `stock_cli_missing`은 안내로만 남는다. 관리 실행은 항상 `runtime/codex`를 쓴다.

### 첫 실행 뒤 보완

26.917 빌드의 첫 실행에서 8개 프로필이 동시에 뜨며 01의 Codex 화면에 4-12 s long task가
반복됐다(설정 화면 멈춤). 01 한 곳에서 4분 동안 `thread/read` 1,626건(대화 356개, 각 3-9회)이
나갔고, 1,019건이 main process였다.

- 원인은 `desktop_record_sync.cjs`였다. 시작 시 다른 프로필 signal 파일(16개, 각 최대 256개
  변경)의 보존된 변경을 모두 새 변경으로 보고 각각 3번 읽었고, 없는 작업은 3 s 간격으로 3번
  재시도했다. 모든 프로필이 같은 backlog를 동시에 재생했다.
- 첫 scan에서 얻은 backlog 항목은 한 번만 읽고, 작업을 찾을 수 없으면 바로 버린다. 그 밖의
  실패와 시작 이후의 변경은 기존처럼 3회 읽기·재시도를 유지한다. 01 main 기준 약 1,070회가
  약 330회로 줄 것으로 추정한다. 오래된 peer signal 파일 정리는 남은 문제다.
- 오래된 probe home 정리는 backend thread에서 rmtree를 돌려 GIL을 두고 state 요청과 경쟁했다
  (첫 실행 중 state 1.6-2 s). `purge_in_child`가 같은 규칙의 삭제를 별도 Python 프로세스에서
  실행한다. 자식은 스스로 `PROCESS_MODE_BACKGROUND_BEGIN`으로 CPU·I/O 우선순위를 낮추고,
  `CODEX_MANAGER_*` 환경 변수를 받지 않으며, stdin pipe가 닫히면(backend 종료) 즉시 끝난다.

실제 26.917 archive로 관리용·동기화 원본 번들 패치를 끝까지 적용하고, 바뀐 JS 3개를
`node --check`로 확인했다. 원격 관리 CLI는 여전히 0.153.4이며 데스크톱에 포함된
0.155.0-alpha와 맞추려면 런타임 패치 rebase가 필요하다(별도 작업).

## 유지한 규칙

- 회수 규칙: 생존 확인 결과가 `exited` 또는 `reused`인 작업만 회수한다. `alive`, `unknown`,
  확인 오류(권한 거부 포함)는 남긴다. `hosts`와 `unclassified`는 바꾸지 않으므로 수정 89의
  고정 host cohort가 유지된다. 비교 후 삭제라서 동시 등록과 세대 변경이 보존되고, 도중에
  삭제된 프로필은 다시 만들지 않는다.
- sweeper는 SSH 연결 경로 밖에서만 실행한다. `prepare()`와 `execution()`은 `reconcile_all`을
  호출하지 않으며 테스트로 고정했다.
- 검사 폴더 삭제 범위: 해석된 `work/control-center/auth-probes` 바로 아래의 canonical 소문자
  UUID 이름을 가진 실제 폴더만 지운다. symlink·junction·reparse point 대상은 거절하고, 내부
  junction은 따라가지 않는다. 검사 직후 삭제는 auth.json 확인 뒤, 프로세스 종료가 확인된
  경우에만 한다. 정리 작업은 1시간이 지난 폴더만 다루므로 진행 중인 검사(최대
  25 s)를 건드리지 않는다.
- 업데이트 확인은 캐시 없는 조회를 유지한다. `updates.installed_package()`는 계속 PowerShell을
  직접 실행한다(수정 66).
- 창 연결·IME: 부모 설정, 포커스, 소유 관계, 입력 큐(수정 33), viewport 정착, 알림 범위
  (수정 71), TOPMOST 금지(수정 90)는 바꾸지 않았다. 창 연결은 이전과 같은 입력(반환된 프로필과
  host 상태)만 읽는다.
- shim 허용 목록은 정확히 일치해야 하며 아니면 차단한다. 추가 명령, 127.0.0.1이 아닌 대상,
  bind 주소, `-R`/`-D`, `ProxyCommand`·`RemoteCommand` 같은 다른 `-o`, `-J`, 모르는 host,
  범위 밖 포트, `-O exit`/`-O forward`는 모두 기존 규칙을 따른다. 검증되지 않은 모드에서는
  거절되고 검증된 모드에서는 일반 passthrough다. 데스크톱이 옵션 목록이나 순서를 바꾸면 다시
  차단된다.
- Rust의 종료·drain 확인은 공유 `state` 계산에 합류하지 않는다. 대기 중인 요청이 없을 때만
  실행되기 때문이다.

## 바꾸지 않은 것

- `profile.login_status`의 Rust 프로필별 gate: `main.rs`는 `profile_id`가 있는 모든 요청에
  프로필별 Mutex를 걸어, 상태 확인이 같은 프로필의 45-180 s show/login 뒤에서 기다린다.
  그러나 `native_login.status`가 호출마다 `account_fingerprint`·`login_observed_at`를 쓰므로
  읽기 전용이 아니다. 그 쓰기를 조건부로 바꾼 뒤 Rust에서 예외를 두어야 한다. Python gate만
  풀면 Rust가 이미 직렬화하므로 효과가 없다. 이번 수정은 Rust 서비스를 바꾸지 않았다.
- 실행 admission fence: 전역 fence 때문에 실행이 서로 기다린다(대기 p50 8.2 s, p90 31 s,
  최대 123 s). fence는 업데이트·유지보수 순서와 프로세스 간 잠금(수정 66·71·81)을 지키므로
  구조 변경은 별도 설계가 필요하다. 이번에는 fence 안의 작업만 줄였다.
- 쓰지 않는 프로필의 대기 해제: 8개 프로필이 항상 실행 중이며, 23시간 동안 한 번도 표시되지
  않은 4개가 약 7 GB와 최대 약 0.85 코어를 썼다. 대기 해제는 실행 중 작업과 SSH 연결을 끊고
  수정 73의 작업 유지 규칙과 충돌할 수 있어 사용자 정책 결정이 먼저 필요하다.
- 포트 1455 로그인 전달과 대화형 `ssh <alias>`: 원격 ChatGPT 로그인 전달(다른 옵션)과 통합
  SSH 터미널은 검증되지 않은 모드에서 계속 차단한다. 기존 테스트가 이를 의도적으로 확인한다.
  이를 허용하거나 26.915·26.917을 `VERIFIED_SOURCES`에 추가하는 일(네이티브 명령 fixture
  수집 필요)은 별도 보안 결정이다.
- `rust_service` 연결 재시도: Python `open()`은 named pipe 오류의 `winerror`를 None으로
  보고하므로 `ERROR_PIPE_BUSY` 재시도 분기가 실행되지 않는다. 서비스가 pipe 인스턴스를 다시
  만드는 짧은 틈에 연결하면 바로 "Rust 관리 서비스에 연결하지 못했습니다."가 난다. 고치면
  서비스가 없을 때의 동작(즉시 실패 또는 20 s 대기)이 바뀌므로 결정이 필요하다. 이번 변경
  전부터 있던 결함이며 이번 변경으로 나빠지지 않았다.

## 남은 문제

세 차례 검토했다. 1·2차 검토는 모두 `ship_with_fixes`였고, 1차 지적은 2차 수정에서, 2차
지적 6건은 3차 수정에서 반영했다. 3차에서 반영한 것은 사용량 쓰기 생략을 poll 시작 snapshot이
아닌 쓰기 시점 저장값과 비교하기, 정리 스레드의 background 모드 제거, 창 연결 직후 동작 gate
해제, 창 연결 지표(`profile.attached.<id>`) 추가, 반환된 프로필을 프로필별로 보관하기, 자체
시험에 대기 loop·실패·프로필 전환 경우 추가다. 3차 셸 검토도 `ship_with_fixes`였고 minor 2건을
남겼다. 남은 항목과 해결하지 않은 항목은 다음과 같다.

- 표시 뒤 state 갱신은 gate 밖으로 옮겼지만 `ShowProfileAsync`는 여전히 그 갱신을 기다린다.
  3차 검토 뒤, 진행 중인 poll을 기다리는 시간을 최대 2 s로 제한했다. 그보다 느린 poll이면
  별도 갱신을 생략하고 다음 timer poll에 맡긴다(그동안 `_shownProfiles`가 반환된 실행을
  유지한다). 따라서 추가 대기는 최대 2 s와 새 state 한 번이며, 변경 전의 "표시 + state 한 번"과
  같은 수준이다. `OpenNotificationAsync`의 35 s 취소 기한은 서비스가 매우 느릴 때 변경 전과
  같은 이유로만 넘을 수 있다. 호출자가 갱신을 전혀 기다리지 않게 하는 방식(`awaitRefresh:
  false`)은 자체 시험을 실행해 확인할 수 있을 때 적용한다.
- `Latest(id)`로 바꾼 `RecoverProfileAsync`·`LoginProfileAsync`·`RestartRemoteProfileAsync`·
  `DetachAsync`는 표시 뒤 state가 오기 전 구간에서 시험하지 않는다. 어느 하나를 `_state`로
  되돌려도 자체 시험은 모두 통과한다. 되돌리면 표시 직후의 복구가 교체된 실행의
  `expected_generation`을 보내거나, 분리한 창이 다른 프로필로 바꾼 뒤 다시 연결될 수 있다.
- 사용량 검사 중 프로필이 제거됐거나 binding이 바뀌면 `save()`가 `False`를 반환해
  `Store.mutate`가 revision을 올리고 파일을 쓴다(변경 전과 같다). `Unchanged(False)`로 바꾸면
  이 쓰기도 없앨 수 있다.
- `native_login.status`는 여전히 `login_status`와 로그인 대기 프로필의 poll마다
  `account_fingerprint`·`login_observed_at`를 다시 쓴다. `Unchanged` 적용이 남았다.
- 실행 중 프로필을 클릭할 때의 브라우저 검사는 여전히 동기식이다. 기억된 결과도 게시 파일
  약 383개를 열어 80-115 ms 걸린다.
- `Warm()`은 알림을 보내지 않아도 셸을 시작할 때마다 toolkit 등록(HKCU AUMID·COM
  activator·아이콘)을 background 스레드에서 실행한다.
- 첫 회수의 `mutate`는 큰 파일을 잠금 안에서 다시 파싱해 약 94 ms 잠금을 잡는다. 첫 탐색은
  background 스레드에서 0.5-1.7 s 동안 GIL을 자주 사용한다(1회).
- `profile-launch-latency-66.md`의 "패키지 조회는 Instances별 최대 5초 재사용" 설명은
  이 문서의 `cached_app()`(서비스 전체, 최대 180 s, 파일 stamp·등록 버전 확인)로 대체된다.

## 검증

- 전체 Python suite(`test_launchers` 제외, 3차 수정 뒤): 1,489개 실행, 실패 0, 오류 1, skip 51.
  오류는 Release SshProxy 빌드가 필요한 `test_manager_ssh_pump.PumpIntegrationTests` setUpClass로
  변경 전에도 같았다. skip은 symlink 생성 불가, POSIX 전용 같은 환경 조건이다. 변경 전
  기준은 1,372개 실행, 실패 0이었다. 수정 92의 테스트를 뺀 나머지 차이는 같은 기간 다른 작업이
  추가한 테스트다.
- 수정 92가 추가한 테스트는 90개다. 새 모듈은 `test_manager_state_performance`,
  `test_manager_login_probe_cleanup`, `test_desktop_launch_package_cache`,
  `test_manager_desktop_publication_cache`, `test_manager_rust_service`이다. ssh_inventory,
  ssh_shim(`BrowserForwardTests`), usage_refresh, native_usage, browser_bundle, accounts,
  remote_updates, startup_updates, profile_restart, current_account, native_login 테스트도
  보강했다. 주요 테스트는 변경 전 코드나 일부러 망가뜨린 scratch 사본에서 실패하는 것을
  확인했다.
- 3차에서 usage_refresh에 검사 도중 저장된 stale 표시가 뒤이은 성공 결과로 대체되는지 보는
  테스트를 추가했다. 2차 코드에서는 실패하고 지금은 통과한다. 쓰기 생략 테스트는 `mutate`
  호출 여부 대신 revision과 `state.json` 바이트가 그대로인지 확인한다. login_probe_cleanup의
  background 모드 테스트 2개는 폴더마다 50 ms 쉬는지, 정리 전·중·후 스레드 우선순위가 같은지
  확인하는 테스트로 바꿨다.
- Shell: `dotnet build ... -c Release` 경고 0, 오류 0(3차 수정 뒤 다시 빌드).
  `WorkspaceNotificationsSelfTest`와 `ProfileOpenStatusSelfTest`의 새 검사는 컴파일만 했고
  실행하지 않았다. 다음 `build-manager.ps1 -SelfTest`에서 처음 실행된다. 3차에서
  `ProfileOpenStatusSelfTest`는 창 연결 직후 gate 해제와 재클릭의 두 번째 `profile.show`, 표시가
  반환될 때 진행 중인 poll, 표시 뒤 state 실패, 새 state 전 다른 프로필로 전환하는 경우를
  다룬다.
- 기준 이후 변경·추가된 `.py` 41개와 git의 수정·미추적 `.py`를 합친 67개(3차 수정 4개 포함)의
  `py_compile` 통과.
- 앱 없이 한 shim 확인: 데스크톱 Browser가 보내는 전달 argv를 `native_compatible: false`
  manifest와 함께 넣으면 `{'operation': 'local-forward', 'alias': '<ssh-host>'}`와 바뀌지 않은
  argv를 반환한다. 변경 전 `ssh_shim.py`는 같은 입력에 `ShimError`를 냈다. 유사한 형식 31개는
  모두 거절됐고, 2차 검토가 OpenSSH의 옵션 재해석을 흉내 낸 fuzz로 추가 확인했다.

합성 데이터와 scratch 폴더에서 잰 값이다. 실제 앱의 end-to-end 시간이 아니다.

| 항목 | 변경 전 | 수정 92 |
| --- | ---: | ---: |
| `atomic_json`, 5.86 MB | 162-209 ms | 55-71 ms |
| `state()` poll, 4.85 MB 합성 저장소 | 읽기 5회, 중앙값 272-287 ms | 읽기 1회, 88-92 ms |
| `state` 응답 크기 | 3,680,236 B | 20,028 B |
| `RemoteUpdates.start()` | 쓰기 24회, 4.9-6.0 s | 쓰기 0회, 76-89 ms |
| 변경 없는 `accounts.sync` | 쓰기 3회, 553-689 ms | 쓰기 0회, 177 ms |
| broker 왕복(fixture pipe) | p50 10.6 ms | p50 1.0-1.2 ms |
| 누수 15,051건 첫 회수 | 회수 없음 | 0.89 s, 6.28 MB → 1.8 KB |
| `browser_bundle.ensure` 반복 호출 | 602-831 ms | 기억된 결과 100-122 ms |
| 패키지 조회(재사용 시) | PowerShell 1-2 s | 0.45 ms |

실제 환경의 `state` poll은 변경 전 저장소를 6번 읽었다. 앱, 런처, 빌드된 exe, 셸 자체
시험은 실행하지 않았다. 실제 프로필, SSH 서버, 실제 app-server의 검사 폴더 삭제, 첫 알림,
내장 Browser 이동도 아직 확인하지 않았다.

## 적용과 확인

실행 중인 관리창은 `artifacts/manager/releases/` 아래의 고정 릴리스를 사용하므로 저장소
변경이 적용되지 않는다. 이 작업에서는 새 릴리스를 만들지 않았다.

적용하려면 `build-manager.ps1 -SelfTest`로 새로 빌드하고(새 셸 자체 시험이 여기서 처음 실행됨),
작업을 마친 뒤 `완전 종료…`하고 다시 실행한다. 대부분의 변경이 관리 서비스의 Python 백엔드에
있어 셸만 바꾸는 업데이트(수정 73)나 제목줄 X로는 적용되지 않는다. 실행 중인 프로필은 시작할
때의 shim 경로를 계속 쓰므로, Browser 전달은 새 빌드에서 다시 연 프로필에서만 동작한다.

새 서비스의 첫 시작에서는 5 s 뒤 누수 기록을 한 번 회수해 `state.json`이 약 0.15 MB 이하로
줄어든다. 첫 `state` poll 120 s 뒤부터 검사 폴더 약 8,300개를 폴더마다 50 ms씩 쉬며 지운다.
7분 이상 걸리고 약 125 GB가 확보된다. 이 동안 디스크 작업이 보일 수 있다.

다시 실행한 뒤 확인할 것:

1. `work/control-center/logs/rust-service.jsonl`의 `state` 소요 시간 하한이 650 ms보다 크게
   낮아지고, 약 30 s마다 있던 급등이 없어야 한다. 실제 목표치는 아직 측정 전이다.
2. `state.json` 크기가 작게 유지되고 `ssh_inventory` 작업 수가 실제 SSH 연결 수 수준이어야 한다.
3. `work/control-center/logs/shell-*.performance.jsonl`: 첫 알림 직후 `ui_stall`이 없어야 하고
   500 ms 이상의 `rpc.state` 기록이 드물어야 한다. `profile.switch.<id>`는 위 이유로 창 연결
   시점 지표가 아니다.
4. `work/control-center/logs/profile-launch.performance.jsonl`: `desktop_bundle`·`browser_bundle`
   단계가 짧아져야 한다. fence를 바꾸지 않았으므로 admission 대기는 남을 수 있다.
5. `work/control-center/auth-probes/`는 정리가 끝난 뒤 거의 비어 있어야 한다. 사용량 카드는
   10분이 지난 값에만 "N분 전 값"을 표시한다.
6. SSH Browser: 새 빌드에서 다시 연 프로필에서 `<ssh-host>`의 개발 서버
   `http://127.0.0.1:<port>/`를 내장 Browser로 연다. 페이지가 `http://localhost:<임시 포트>/`로
   열려야 한다. 그 프로필의 `work/control-center/profiles/<profile-id>/ssh-routing.jsonl`에
   `"operation": "local-forward"`, `"alias": "<ssh-host>"` 기록이 생기고, 새
   `unsupported_native_ssh_version`은 없어야 한다. 데스크톱 로그
   `%LOCALAPPDATA%\Codex\Logs\<날짜>\codex-desktop-*.log`에 새
   `browser_sidebar.remote_localhost_port_forward_failed`가 없어야 한다. `state.json`의
   해당 프로필 `ssh_inventory` 항목의 `unclassified`가 true로 바뀌지 않아야 한다.

> 수정 94 참고: 실행 fence 구조를 바꿨다(다른 프로필은 함께, 업데이트·유지보수·같은 프로필은 단독). [병렬 프로필 실행](parallel-profile-launch-94.md)을 보라.
