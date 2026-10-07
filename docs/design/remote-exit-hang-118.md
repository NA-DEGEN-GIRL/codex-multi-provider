# 수정 118 — 원격 Codex 종료 멈춤(sqlx 풀 정리 작업), 완전 종료 대기 선택지

## 증상
- 완전 종료가 "SSH 작업 종료를 기다립니다. 현재 답변이 끝나면 원격 실행을 종료합니다."에서 1분 넘게 멈췄다.
  진행 중인 답변은 없었다. 약 63초 뒤 "SSH 작업이 아직 진행 중이거나…" 오류가 나고 관리창이 남았다.
  다시 눌러도 같았고, 결국 강제 종료했다.
- 대기 대상은 한 서버(remote-host)의 세 프로필(06, 07, 01-Claude)이었다. 다른 서버와 프로필은 2~3초 안에 종료가 확인됐다.

## 원인
- 매니저는 원격 Codex에 SIGHUP을 보낸다(`native_controller.drain`, 정상 종료 전용). app-server는 진행 중인 답변을 끝낸 뒤
  종료하는데, 세 프로세스는 SIGHUP을 받은 뒤 6분이 지나도 살아 있었다. 각 프로세스의 tokio 작업 스레드 하나가
  CPU 100%(사용자 영역, 시스템 호출 없음)를 쓰고 있었다. 로그에는 "no assistant turns running; stopping acceptor"까지만 남았다.
- perf 표본을 같은 빌드의 디버그 심볼로 풀었다. 1479개 중 1479개가 `sqlx_core::pool::inner::spawn_maintenance_tasks`의
  정리 작업(`CloseEvent::do_until` 안의 poll)이었다.
- sqlx-core 0.9.0의 알려진 버그다(launchbadge/sqlx#3645, 수정 #4289는 아직 릴리스되지 않음).
  - `release()`는 연결을 대기열에 넣고 허가를 돌려준 *뒤에* `num_idle`을 올린다. 그 사이 다른 작업이 연결을 꺼내며
    `num_idle`을 내리면 0에서 `usize::MAX`로 넘어간다.
  - 정리 작업은 `for _ in 0..pool.num_idle()`를 돌며 `try_acquire`만 하므로 한 번도 양보(await)하지 않는다.
    그래서 그 작업 스레드를 사실상 영원히 점유한다.
  - 종료할 때 런타임은 이 스레드가 돌아오기를 기다리므로 프로세스가 끝나지 않는다.
    이 버그는 연결 하나짜리 풀(`open_read_only_pool`)처럼 경합이 잦은 풀에서 일어난다.
- 평소에는 다른 작업 스레드가 일을 이어받아 겉으로 드러나지 않는다. 대신 프로세스마다 CPU 한 코어를 계속 쓴다
  (멈춘 세 프로세스의 누적 CPU는 각각 2.3h, 2.5h, 7.2h였다).

## 변경
### 런타임(`codex-state`)
- `open_read_write_pool`, `open_read_only_pool`에 `.idle_timeout(None).max_lifetime(None)`을 지정했다.
  두 제한이 모두 없으면 sqlx는 정리 작업을 만들지 않는다(상류 이슈가 권하는 회피책). 로컬 SQLite 연결은 재활용할 필요가 없다.
  임시 기록 DB(`temporary_history`)는 원래부터 이렇게 열었다.
- 패치 SHA256 `485cb97b…`, 결과 트리 `37ad5066…`. Windows와 Linux 런타임을 다시 빌드한다.
  Linux `codex`는 격리 SIGHUP 감사를 통과해야 `DRAIN_AUDITED_SHA256`에 추가된다.

### 관리창(완전 종료)
- 원격 종료 확인을 기다리는 동안 대상 프로필과 경과 시간을 보여 준다("원격 Codex 종료를 확인하고 있습니다 · 06, 07 · 12초").
  예전처럼 답변이 진행 중이라고 단정하지 않는다.
- 제한 시간(30초 + 서버당 11초)이 지나도 확인이 없으면 오류를 내지 않는다. 대신 거절된 원격 종료와 같은 선택 창을 띄운다.
  확인을 누르면 원격 실행을 서버에 남겨 두고 완전 종료하고, 취소를 누르면 관리창을 유지한다.
  서비스의 종료 요청은 어느 쪽이든 유지된다.

## 즉시 조치
- 멈춘 원격 app-server 세 개를 정확한 PID로 SIGKILL했다. 실행 파일이 `codex-control-center/runtime/*/codex`이고
  `--listen unix:///tmp/codex-control-1000/…`인 것만 대상으로 삼았다. 사용자가 직접 띄운 다른 codex는 건드리지 않았다.
- 다음 실행 때 매니저가 `native-drain.json`(signalled)과 프로세스 부재·잠금 해제를 확인해 종료를 완료로 처리한다.

## 같은 날 확인한 다른 사항
- Claude 데스크톱 02/03 창이 꺼진 것은 이 작업 때문이 아니다. 08:30에 Claude 데스크톱이 2.19675.1을 내려받았고,
  08:41에 등록하면서 `RegisterByPackageFamilyName`에 `ForceApplicationShutdown`을 썼다(AppXDeploymentServer 로그).
  Windows는 같은 패키지의 모든 인스턴스(01/02/03)를 닫는다. 자동으로 다시 열린 것은 기본 인스턴스(01)뿐이었다.
- 그 재시작으로 Claude 세션에서 띄운 도우미(토큰 갱신, 종료 후 런타임 교체)도 함께 꺼졌다. 다시 띄웠다.
  14:11 완전 종료 직후 수정 117 런타임 활성화와 Linux 번들 교체가 정상적으로 끝났다.
  긴 빌드는 이제 WMI(`Win32_Process.Create`)로 띄워 세션과 분리한다.

## 빌드 서버 사용 제한, Windows 런타임 교차 빌드
Windows 릴리스 빌드는 이 PC(12코어, 사용자가 작업 중이라 8작업·낮은 우선순위)에서 85~237분 걸렸다. 그래서 빌드 서버에서 교차 빌드한다.
빌드 서버는 128스레드 공유 서버라 64스레드만 쓴다.

- **사용 제한:** `package_runtime.py`, `package_windows_runtime.py` 모두 다음을 적용한다.
  - `-j 64`, CPU 0-31·64-95(물리 코어 32개)
  - `nice 10`, 열린 파일 한도 65536
  - 사용자별 빌드 잠금 `~/.cache/codex-runtime-build.lock`
  - `--jobs`, `--cpus`로 바꿀 수 있다. 제한 코드는 Linux의 `__main__`에서만 실행된다(관리창이 Windows에서 이 모듈을 import한다).
- **교차 빌드(`package_windows_runtime.py`):** 상세 절차는 `scripts/remote_helpers/README.md`.
  - 도구: Ubuntu LLVM 20의 `clang-cl`·`lld-link`·`llvm-lib`, xwin 0.10.0으로 받은 MSVC CRT 14.44 / SDK 10.0.26100(로컬 빌드와 같은 계열).
  - Microsoft Build Tools 라이선스는 사용자가 동의했다.
  - `runtime/`이 기록된 패치 트리일 때만 빌드하고, 빌드가 끝난 뒤에도 다시 확인한다. `RUSTFLAGS`는 지운다(`/STACK`·`+crt-static` 보존).
  - C 컴파일러와 헤더 경로는 Windows 대상에만 지정한다.
  - 다섯 exe가 로컬 빌드와 같은 속성을 갖는지 검사한다: x64, 콘솔, ASLR/NX, 8 MiB 스택, 동적 CRT 가져오기 없음, 설치 exe에만 ID 1 asInvoker 매니페스트.
- **찾아서 막은 문제:**
  - lld의 기본 UAC 매니페스트를 설치 exe의 asm.v2 매니페스트와 병합하면 속성에 네임스페이스 접두사가 붙는다(`ms_asmv1:level`).
  - Windows는 이 매니페스트를 거부한다(CreateActCtx·CreateProcess 오류 14001, 실행 불가).
  - 교차 빌드에만 `/MANIFESTUAC:NO`를 넣어 입력 매니페스트만 병합한다. Windows가 받아들이고 관리자 권한 없이 시작하는 것을 일시정지 생성으로 확인했다.
- **스테이징:** `stage_manager_runtime.py --source <패키지>`.
  - 패치 트리·패치 해시·기준 커밋·파일 해시를 다시 확인한다.
  - 실패하면 만든 release 폴더를 지운다.
  - 활성화 조건(공유 편집·공통 저장소 검증)은 로컬 빌드와 같다.
- **차이:** 교차 빌드는 내장 텍스트를 LF로 넣는다. 로컬 Windows 체크아웃은 `core.autocrlf=true`라 CRLF로 넣는다. Linux 런타임과 같은 바이트가 된다.

## 검증
- 교차 빌드 결과:
  - 서버 첫 전체 빌드는 8분 18초 걸렸고, 매니페스트 검사에서 위 문제를 잡았다.
  - 수정 뒤 503초에 빌드돼 PE 검사를 모두 통과했다. 이 PC로 옮기는 데는 19초가 걸렸다.
  - 설치 exe는 CreateActCtx와 관리자 권한 없는 일시정지 시작을 통과했다.
  - `codex --version`은 `codex-cli 0.153.4`, 후보 기능 19개가 모두 있었다.
- 패치 재생성 후 서버의 `restore_runtime.py`가 결과 트리 `37ad5066…`을 재현했다. 이전 트리와의 차이는 `state/src/sqlite.rs` 한 파일이다.
- Linux 번들 `0.153.4-managed-485cb97bd8e33789`(codex `02b5eda3…`)는 7분 만에 빌드됐다. 격리 SIGHUP 감사에서 진행 중인 답변 유지,
  반복 SIGHUP으로 강제 종료되지 않음, 답변 완료 후 종료(코드 0)를 확인했다(`work/validation/release-118/drain-audit.json`).
- `cargo test -p codex-state`: 190개 모두 통과했다. 기본 fd 한도(1024)에서 전체 병렬로 돌리면 95개가 "unable to open database file"로 실패한다.
  한도를 올리거나 `--test-threads=4`로 돌리면 모두 통과하므로, 테스트 환경의 fd 고갈이다.
- `tests.test_remote_drain` 34개 통과(7개 건너뜀), 관리창 컴파일 경고 0개.

## 실제 적용 뒤 발견: 교차 빌드본이 기존 기록 DB를 열지 못함(줄바꿈)
- **증상:** 교차 빌드본을 활성화하고 관리창을 열자 모든 프로필이 "Codex를 준비하고 있습니다"에서 멈췄다.
  - 각 프로필의 런타임이 initialize를 받은 직후 종료됐다.
  - 같은 실행을 직접 재현하자 0.2초 만에 `failed to initialize sqlite state runtime under <기록 홈>`으로 끝났다.
- **원인:**
  - sqlx는 각 마이그레이션 SQL 파일 바이트의 SHA-384를 실행 파일에 넣고, 이미 적용된 마이그레이션의 체크섬이 다르면 시작을 거부한다.
  - 이 PC의 기록 DB는 CRLF 체크아웃(`core.autocrlf=true`)으로 빌드된 런타임이 마이그레이션했다. 적용된 행 366개가 모두 CRLF 바이트와 일치했고, LF 바이트와 일치한 것은 0개였다.
  - 서버 체크아웃은 LF였다.
  - 무인 검증은 새 DB를 만들어 쓰므로 이 차이를 볼 수 없었다.
- **즉시 조치:** 같은 소스를 이 PC에서 빌드한 런타임(검증 통과)으로 되돌렸다. 실제 프로필 설정과 기록 홈으로 initialize가 정상 응답하는 것을 확인했다.
- **변경:**
  - **교차 빌드 체크아웃:** 검증된 트리를 `core.autocrlf=true`, `core.symlinks=false`로 `work/remote-build/windows-source`에 다시 풀어(`windows_checkout`) 그 소스로 빌드한다. Windows 체크아웃과 같은 바이트가 되고, 마이그레이션 파일의 CRLF를 확인한다.
  - **패키지 기록:** `windows-build.json`에 `line_endings: "crlf"`와 마이그레이션 목록(버전·설명·SHA-384)을 남긴다.
  - **호환 검사(`manager_core/runtime_migrations.py`):** 후보가 넣은 마이그레이션 체크섬을 실제 기록 DB들(공통 기록 홈과 프로필 홈의 `*.sqlite`, 읽기 전용)의 `_sqlx_migrations`와 비교한다.
    - 같은 버전·설명인데 체크섬이 다르면 스테이징을 거부한다.
    - 활성화 직전에도 다시 확인한다(`activate_manager_runtime.py`).
