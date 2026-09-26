# 원격 런타임 준비 상태

이 폴더는 기존 원격 Codex CLI를 보존하면서 별도 관리 프로필을 준비하는 도구입니다. **준비 성공과 실제 연결 성공은 다른 상태**입니다. 2026-09-13에는 원본 앱과 같은 SSH 명령·WebSocket 연결을 사용하는 화면 없는 시험에서 Windows 04 계정 연결, Astra·DeepSeek Flash max·Sol의 실제 작업을 확인했습니다. 원본 앱 화면에서 프로젝트와 대화를 선택하는 흐름은 별도 검증 대상입니다.

최신 실제 시험은 `artifacts/results/manager-ssh-live-20260913-203156-eb4100/report.json`이며 20개 확인 항목을 통과했습니다. 관리 원본 저장소 연결, 빈 대화의 엄격한 기록 쓰기 종료, 현재 SSH 연결의 새 작업 차단·해제도 포함합니다. 원격 서버 전체 종료나 계정 간 SSH 인계가 완료됐다는 의미는 아닙니다.

## 구현된 범위

- `RemoteManager.list_hosts()`는 사용자의 SSH 설정에서 정확한 Host 별칭만 읽습니다. 키 파일을 읽거나 `Match exec`를 실행하지 않습니다.
- `inspect(alias)`는 선택한 SSH 설정을 `-F`로 사용하고, 알려진 호스트 키와 비대화형 인증을 요구합니다. 로그인 셸 환경에서 CLI/OS/CPU/Python 상태를 검사합니다. 원격 파일은 쓰지 않습니다.
- `prepare(...)`는 Linux 실행 파일의 실제 형식·CPU·빌드 기록·SHA256을 검사합니다. 묶음이 없으면 중단하며 Windows exe를 Linux에 전송하지 않습니다.
- 런타임과 설정 정의는 원격 사용자의 `~/.local/share/codex-control-center` 아래에 버전별로 저장됩니다. 기본 CLI·OS HOME·전역 PATH·SSH 설정은 변경하지 않습니다.
- 선택한 공급자의 키만 SSH 표준 입력으로 전송해 원격 사용자 전용 파일에 저장합니다. 키는 패키지·명령행 인수·관리 상태·로그에 넣지 않습니다.
- 원격 사용자의 `~/.codex`에서 MCP 선언만 가져오고 skills 폴더는 프로필 안의 링크로 공유합니다. Windows의 명령·경로·로그인 설정을 복사하지 않습니다. 사용자가 직접 추가·수정한 설정은 소유권 기록으로 구분해 보존합니다.
- 실행 도우미는 프로필 잠금을 실제 런타임에 넘깁니다. 일반 진입점은 stdio만 허용하고, 관리 SSH 어댑터는 해당 프로필 전용 Unix 소켓에만 연결합니다. 원래 Codex daemon을 종료하는 명령이나 외부 TCP 리스너를 허용하지 않습니다.

관리 SSH 어댑터의 호출 계약은 `python3 <profile>/launch.py <revision> native-probe|native-version|native-start|native-proxy|native-stop`입니다. `native-start`는 같은 revision의 프로필 런타임을 재사용하며 다른 revision이 실행 중이면 대기 오류를 반환합니다. `native-proxy`는 같은 프로필의 전용 Unix 소켓으로 연결합니다. `native-stop`은 작업 정리 증거가 없을 때 종료를 거절합니다. 원본 앱의 bootstrap 명령에 있는 `pkill`을 그대로 실행하지 않습니다.

`managed_sources.py`는 해당 기능을 포함한 빌드에서 호스트 내부의 원본 저장소 목록과 실행 소유 기록을 연결합니다. Windows 경로를 Linux에 복사하지 않습니다. 다른 프로필이 소유한 대화의 참조에도 실제 소유자를 유지하며, 실행 소유권을 이 파일 생성만으로 바꾸지 않습니다. `ssh_runtime_control.py`의 관리 통신은 기존 Windows SSH 연결 하나에만 적용됩니다. 그 연결이 끊겼다는 이유로 원격 서버나 작업이 끝났다고 판단하지 않습니다.

`handoff_store.py`와 `manager_core/remote_handoff.py`는 같은 SSH 호스트 안에서 원본 대화·자식의 실행 소유권을 인계합니다. 기존 SSH 관리 연결에서 전체 자식의 기록 쓰기 해제를 확인하고, 실제 Linux 프로세스의 PID·시작 시각·부팅 ID·실행 파일·프로필 연결을 재확인합니다. 원격의 영구 소유권 잠금과 기록 쓰기 잠금을 잡은 뒤 버전을 증가시키며, Windows와 Linux 양쪽에 변경 기록을 남깁니다. 미확인 변경을 자동 반복하지 않습니다. 로그인 파일이나 대화 기록을 대상 프로필로 복사하지 않습니다.

실제 시험 `artifacts/results/manager-ssh-handoff-20260913-205547-4cc0be/report.json`은 11개 항목 모두 통과했습니다. 서로 다른 실제 계정 02 → 04 → 02, 동일 Astra 부모·동일 DeepSeek Flash max 자식, 자식의 3회 셸 파일 작업, 독립 대화 유지, 전환 중 원본 바이트 보존을 확인했습니다. 재개 요청에는 처음 선택한 작업 폴더·승인·샌드박스 설정을 명시합니다. 원격 프로세스 전체 종료나 실제 GUI의 SSH 대화 이동은 이 시험 범위 밖입니다.

추가 시험 `artifacts/results/manager-ssh-handoff-identity-20260913-210728/report.json`에서는 새 모델 실행 없이 대화를 재개한 직후 왕복 인계했습니다. 아직 로드하지 않은 자식은 `coldDescendantRequiresWriterClaim` 관측만으로 유휴라고 판단하지 않고, `managedCloseIdle`의 실제 쓰기 잠금·대기 입력·원본 관계 검증으로 넘깁니다. 다른 종류의 차단 사유가 함께 있으면 인계를 시작하지 않습니다. 양쪽 Linux 프로세스 식별자도 Windows SSH 연결 식별자와 구분해 영구 인계 기록에 남겼습니다.

재시험 명령은 `python scripts/test_manager_ssh_handoff_live.py --execute`입니다. 두 계정의 기존 인증을 메모리에서 사용하며 새 시험 프로필과 폴더를 만듭니다. `--execute`가 없으면 시험 계획만 출력합니다.

Linux 소켓 경로 길이 제한 때문에 소켓은 사용자 소유의 0700 디렉터리 `/tmp/codex-control-<uid>/<profile UUID>.sock`에 두고 명시적인 `--sock`으로 연결합니다. 인증·세션·설정은 계속 프로필의 `CODEX_HOME` 안에 둡니다. 이 도우미만으로 Windows 원본 앱의 SSH 호출이 자동으로 연결되지는 않습니다.

## Linux 묶음 만들기

지원 대상은 현재 Linux x86_64/aarch64, Python 3.11 이상입니다. 해당 Linux 환경에서 이 저장소와 저장소가 요구하는 Rust 빌드 도구를 준비하고 다음을 실행합니다.

```sh
python3 scripts/remote_helpers/package_runtime.py --build
```

생성된 `artifacts/remote/linux-<CPU>` 폴더를 Windows의 같은 저장소 경로 아래에 놓으면 관리창의 원격 준비 단계에서 검사합니다. 빌드 도구 자체가 CLI를 설치하거나 SSH로 전송하지는 않습니다. 빌드 성공만으로 실제 앱·모델·SSH 호환 시험을 통과했다고 표시하지 않습니다.

Linux 묶음에는 `codex`, `codex-code-mode-host`, `bwrap` 세 실행 파일이 필요합니다. 빌드 도구는 upstream 순서대로 bubblewrap을 먼저 만들고 해시를 계산한 다음 그 해시를 Codex에 넣습니다. `libcap` 개발 헤더 등 Linux 빌드 의존성이 필요하며(Ubuntu: `pkg-config libcap-dev`, upstream CI와 같음), 샌드박스 파일이 없거나 해시가 다르면 준비를 거절합니다. 묶음 버전은 `<CLI 버전>-managed-<패치 SHA256 앞 16자>`이고 `build_source_sha256`에 패치 해시 전체를 남깁니다. 새 `codex` 해시는 `native_controller.py`의 `DRAIN_AUDITED_SHA256`에 넣어야 SSH 작업을 기다리는 안전한 재시작을 사용합니다.

Ubuntu 24.04의 사용자 네임스페이스 정책 때문에 실행이 거절될 수 있습니다. 이 경우 샌드박스를 끄지 않고 원인을 표시합니다. 호스트별 관리자 정책 변경 도우미는 배포 소스에 포함하지 않으며 일반 원격 준비 과정에서도 실행하지 않습니다.

## 남은 실제 연결 검증

현재 설치 앱은 SSH에서 기본 CLI의 daemon bootstrap/control 경로를 사용합니다. 단순히 로컬 `CODEX_CLI_PATH`에 Windows exe를 설정하면 원격 런타임까지 바뀌지 않습니다. 앱의 SSH 시작·검사·중지 경로를 모두 프로필에 맞게 연결하고, 원래 원격 daemon에 영향을 주지 않는 것을 확인해야 합니다. 이를 확인하기 전 상태는 `prepared_not_connected`입니다.

기존 원격 CLI에 로그인됐다는 사실도 새 관리 프로필이 사용자가 선택한 GPT 계정으로 로그인됐다는 증거가 아닙니다. 별도의 인증과 계정 일치 확인이 필요합니다.

로컬 자동 검증: `python -m unittest discover -s tests -p test_manager_remote.py -v`. 이 검증은 가짜 SSH 응답과 임시 폴더를 사용하며 실제 서버·키·모델 API를 사용하지 않습니다.
