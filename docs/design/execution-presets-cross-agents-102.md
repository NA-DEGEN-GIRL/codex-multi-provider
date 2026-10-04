# 수정 102 — 작업별 실행 프리셋과 Codex↔Claude 하위 에이전트

이 문서는 수정 102 시점의 구현·검증 기록이다. 이후 SSH 지원과 네이티브 검증의
진행 상태는 [수정 103](ssh-execution-presets-103.md)을 확인한다.

## 목적과 사용법

하나의 로그인 계정에 여러 하위 에이전트 조합을 보관하고, 작업별로 선택한다.
예를 들어 기본 GPT 작업, Claude 검토 담당을 포함한 GPT 작업, 다른 GPT 계정을
구현 담당으로 지정한 Claude 작업을 별도로 만들 수 있다. 계정이나 Electron 창을
복제하는 기능은 아니며 로그인 계정의 사용 한도도 달라지지 않는다.

1. 필요한 Codex/Claude 계정을 기존 프로필 추가·로그인 기능으로 등록한다.
   API/로컬 모델은 기존 외부 모델 관리에서 등록·확인한다.
2. 프로필 우클릭 또는 설정 및 관리의 **실행 프리셋**을 연다.
3. 이름을 정하고 **하위 에이전트 추가**에서 역할 이름, 실행 계정/모델, 추론 강도를 선택한다.
   `코드 검토`, `UI 구현`처럼 역할을 설명하는 이름을 사용한다.
4. 저장한 뒤 필요하면 **새 작업 기본으로** 지정한다.
5. 작업 상단 **실행 프리셋**에서 해당 작업에 쓸 조합을 선택한다.
   주 에이전트 모델·추론 강도는 기존 대화 입력창에서 선택한다.

새 실행기 도입과 새로운 역할·제공자 추가에는 해당 프로필의 실행 준비가 한 번 필요하다.
현재 작업을 강제로 끝내지 않고 선택을 보관하며, 작업 종료 후 프로필을 다시 열면 준비된다.
이미 준비된 역할로 만든 조합끼리는 프로필 재시작 없이 다음 응답부터 전환한다.
선택 시 진행 중인 답변이나 이미 생성된 하위 에이전트는 시작 당시 설정을 유지한다.

프리셋을 편집하면 새 설정 버전을 만든다. 기존 작업은 원래 버전에 계속 연결된다.
삭제는 새 선택 목록에서 제외하는 동작이며 기존 작업의 설정 버전을 없애지 않는다.
**프로필 기본 설정**을 선택하면 그 작업의 프리셋을 명시적으로 해제한다.
새 작업 기본값 변경은 이미 만들어진 작업의 설정을 바꾸지 않는다.

현재 Windows 로컬 작업을 지원한다. SSH에는 Windows 계정 경로나 인증 정보를 그대로
전달하지 않는다. 원격 작업에서 선택하면 미지원 안내를 표시하고 기존 설정을 보존한다.
기존 SSH 주 프로필·하위 에이전트 설정 기능은 계속 사용한다.

## 저장과 적용 경계

- `scripts/manager_core/execution_presets.py`: 버전이 고정된 설정, 계정/모델 참조,
  기본값, 작업 연결, 실행용 역할·설정 파일과 승인된 레지스트리 생성.
- `execution_preset_service.py`: `presets.list/save/delete/default/select/status` UI 요청.
  프로필 종료·재시작·로그인 변경을 호출하지 않는다.
- `manager/Shell/ExecutionPresets.cs`: 관리 화면, 역할 선택, 작업 상단 선택 버튼.
- `instances.py`: 중단된 프로필을 준비할 때 역할 합집합을 생성하고, 기능을 지원하는
  런타임에만 레지스트리 경로와 필요한 등록 모델의 실행 환경을 전달한다.
- `runtime_admin.py`: `thread/settings/update`에 정확히 `threadId`와
  `executionPreset: {id, revision} | null`만 허용한다. 권한·임의 경로 주입을 허용하지 않는다.
- `app_transport.py`: 네이티브 적용 알림에서 ID/버전만 보관한다. RPC의 빈 응답은
  요청 접수이므로 적용 완료로 표시하지 않는다.

런타임은 `CODEX_MANAGER_EXECUTION_PRESETS`가 가리키는 등록 레지스트리만 읽는다.
역할의 공급자, 모델, 추론 강도와 준비된 파일 식별을 검증하며 임의 모델 덮어쓰기를
거절한다. 아직 준비하지 않은 조합을 조용히 기본 설정으로 바꾸지 않는다.
이력을 포함한 조합 버전 256개·고유 역할 256개·실제 직렬화 1MiB 제한을 저장과
게시 전에 확인한다. 초과하면 마지막 정상 설정을 보존하며, 이력을 임의로 버리지 않는다.
선택한 조합과 명시적 해제를 작업 설정 기록에 저장해 콜드 재개에도 유지한다.
선택 순서는 명시적 관리 연결, 저장된 작업 설정, 새 작업 기본값이다.

내부 작업 설정에는 소유 프로필과 프로필별 선택 지도를 최대 64개까지 보관한다.
계정 A→B→A 전환 중 B가 압축해 제한된 재개 문맥만 남는 경우에도 A의 기존 설정을
찾을 수 있게 하는 메타데이터다. API 호출자가 소유 계정이나 임의 설정 지도를 지정하는
인터페이스는 추가하지 않는다. 모든 불변 프리셋 버전을 보관하므로 관리 앱이 아직
관측하지 못한 초기 기본값을 편집·삭제해도 해당 작업의 재개 참조는 남는다.

주요 네이티브 변경은 `runtime/codex-rs/core`의 execution presets,
session settings, thread manager, multi-agent routing/auth 및 Claude task에 있다.
API는 app-server protocol과 protocol의 thread settings snapshot에 추가한다.
현재 턴의 Config는 불변이며 설정 변경은 이후 턴의 Config에 적용한다.

## 교차 하위 에이전트와 인증

Codex→Claude는 선택된 Claude 계정의 공식 CLI 실행기를 자식 에이전트로 사용한다.
Claude→Codex는 별도 `codex_agents` MCP를 통해 동일한 네이티브 에이전트 핸들러를 호출한다.
생성, 추가 지시, 메시지, 목록, 대기, 중단이 기존 에이전트 그래프·권한·깊이 제한을 따른다.
이는 Claude 자체 Agent 도구를 Codex 하위 에이전트라고 표시하는 방식이 아니다.
프리셋으로 관리하는 위임은 Claude 자체 Agent/Task 우회 실행도 차단한다.
압축용 체크포인트 요청은 여전히 도구와 MCP 없이 수행한다.

다른 GPT 계정을 지정하면 등록된 계정의 auth source에서 읽기 전용으로 접근 토큰을
읽는다. 계정 fingerprint를 확인하고, 부모와 분리된 메모리 인증 저장소를 사용한다.
계정 확인 실패 시 부모 인증으로 대체하지 않는다. 선택 정보·MCP 메시지·로그에는
토큰을 넣지 않는다. 외부 공급자 revision이 바뀌면 예전 endpoint에 새 키를 보내지
않도록 기존 프리셋 참조를 준비 불가로 표시하고 새 버전 저장을 요구한다.

같은 것으로 확인된 GPT 계정만 네이티브 문맥 포크를 허용한다. 다른 계정이나
다른 공급자로 보내는 자식 요청은 새 평문 작업 설명을 사용한다. 암호화된 내부 상태를
복호화하거나 다른 공급자에 보내지 않는다. 같은 대화에서 GPT↔Claude 주 모델 전환과
검증된 평문 체크포인트/기존 공급자 상태 보존은 수정 98–101의 기존 기능이다.

## SSH 후속 구현에 필요한 사항

현재 로컬 코드의 경로를 SSH로 바꾸는 것만으로 지원할 수 없다.

- 호스트별 registry/역할 파일 생성·원자적 배포와 Linux 런타임 capability 검증.
- Windows 계정별 인증 broker 또는 명시적으로 등록한 원격 인증 source. 부모 계정만
  전달하는 기존 SSH AuthProxy로 다른 계정의 자식 인증을 대신하지 않는다.
- 원격 Claude CLI 설치·로그인 및 runner/MCP 묶음 배포.
- 호스트별 AdminClient endpoint, task preset 적용 알림과 재개 검증.
- 관련 진입점: `remote.py`의 `settings_files/_install`, `remote_helpers/launch.py`,
  `ssh_auto_prepare.py`, `ssh_runtime_control.py`, `ssh_shim.py`.

## 검증과 배포 상태

검증 중인 내용은 이 절의 완료 기록으로 구분한다. 실제 유료 모델 호출, 사용자
프로필/서비스 재시작, 원격 계정 설치는 이번 자동 검사에 포함하지 않는다.

- Python: 프리셋 버전/연결/명시적 해제, 계정·외부 공급자 경계, 독립된 유효 조합,
  관리 RPC 범위, 적용 대기 상태, 기본값 표시 및 기존 프로필 기능을 합성 데이터로 검사.
- WPF: 저장된 혼합 조합의 편집·저장 왕복과 관리 화면 동작을 가짜 백엔드로 검사.
- Native: 선택 변경 시 현재 턴 유지, 다음 턴 역할 변경, 재개 시 버전 유지,
  계정별 인증 분리, 평문 라우팅과 Claude↔Codex 생성/추가 지시/결과/중단을
  가짜 공식 CLI와 loopback Responses 서버로 검사.
- 새 native binary의 공유 편집·공통 저장소 검증을 통과해야 다음 실행용으로 활성화한다.
  실행 중인 앱·서비스·프로필은 강제로 교체하지 않는다.

### 확인된 결과와 현재 차단 사항 (2026-10-01)

- 관련 Python 통합 회귀 묶음 204개 실행: 203개 통과, 기존 1개 제외.
- 가짜 계정으로 교차 실행 설정·스크립트를 생성하는 오프라인 검사 1개 통과.
  새 네이티브 실행기가 필요한 양방향 위임·턴 중 전환/재개 3개 검사는 **실행하지 못했다**.
- WPF 실제 대화상자를 숨겨서 검사한 13개 항목 통과. 저장된 GPT/Claude/API 역할의
  편집·저장 왕복, 역할 이름, 정확한 계정/모델/추론 유지, 기본값·삭제를 확인했다.
- 관리 앱 후보 `20260930-172530-234`의 Release 빌드 성공. 활성 배포 포인터는 변경하지 않았다.
- 고정 관리 묶음 파일 207개의 해시와 원천 Python 사본 일치를 확인했다. 후보 실행 파일에서도
  동일한 WPF 13개 검사가 통과했다. 별도 `codex-protocol` 크레이트 빌드는 통과했다.
- `codex-core`, `codex-protocol`, `codex-app-server-protocol`의 테스트 코드까지 포함한
  scoped Clippy 검사와 자동 수정은 통과했다. 컴파일 중 발견한 TypeScript optional 필드,
  프리셋 상태 비교와 인증 경로 타입 오류는 수정 후 이 검사로 확인했다.
- 네이티브 Windows 빌드는 `rmcp_macros` DLL을 불러오는 단계에서 Windows 애플리케이션
  제어에 차단됐다(`LoadLibraryExW`, OS 4551). 실제 양방향 실행, 네이티브 동작 검사와
  API 스키마 fixture 재생성은 완료하지 못했다. 보안 정책을 해제하거나 차단 파일을
  우회하지 않았다. 정적 검사 통과를 실제 실행 검증으로 대신하지 않는다.
- 저장소가 지정한 Bazel 9.0.0을 공식 체크섬과 대조했으나, `bazel mod deps
  --lockfile_mode=update`도 Windows JNI DLL 차단으로 실패했다(exit 37).
  `MODULE.bazel.lock`은 변경하지 않았다. 기존 `just.exe`도 애플리케이션 제어에
  차단되어 실행 가능한 고정 Rust 도구로 동일한 Cargo 명령을 사용했다.
- 누적 소스 패치를 새 worktree에 복원해 트리
  `90ffc4c5082cec81d1e5a2e4eaf045a000dfd844` 일치를 확인했다(384개 변경 파일).
  패치 SHA256은 `6664e83f474782c2268787fe1b8bd410010b0c3cfbdfc102870e3b4ef67801ea`다.
  실제 작업 인덱스가 바뀌지 않았으며 기존 변경 사항을 포함해 보존했다. 복원 성공은
  실행 성공을 의미하지 않는다. 임시 검증 기록은 `work/runtime-restore-102.log`,
  후보 검증 결과는 `artifacts/results/execution-presets-102-candidate.json`에 있다.

### 다음 빌드 담당자의 순서

1. 애플리케이션 제어가 필요한 빌드 구성요소를 허용하는 Windows 빌드 환경에서 진행한다.
   보안 정책을 이 기능의 일부로 변경하지 않는다. 현재 후보를 완료된 릴리스로 취급하지 않는다.
2. `scripts/build-env.ps1`과 고정 Rust 1.95.0을 사용해 새 실행기를 빌드한다.
   기존 build helper와 같이 `codex`, `codex-app-server`, code-mode host,
   Windows sandbox setup 및 command runner를 함께 만든다.
3. `just test -p codex-core --lib -E 'test(execution_presets) | test(preset_auth) |
   test(multi_agent_message) | test(tasks::claude_code::messages) |
   test(tasks::claude_code::delegation)'`로 관련 코어 검사를 실행한다.
   앱 서버 프로토콜 fixture는 `just write-app-server-schema`와
   `just write-app-server-schema --experimental` **둘 다** 생성한다.
   `thread/settings/update`가 실험 API이므로 첫 명령만으로는 요청 스키마가 빠진다.
   `app-server-protocol/schema/` 아래 TypeScript, JSON, stable/experimental
   precomputed JSON.zst를 함께 확인한다. durable rollout 내부 스키마는 stable
   precomputed 묶음에 포함된다. scoped `just fix`, `just fmt`,
   `just bazel-lock-update`도 완료하고 변경된 fixture/lock을 확인한다.
4. `CLAUDE_NATIVE_E2E_RUNTIME`에 **새** 앱 서버 경로를 지정하고 `PYTHONPATH=tests`에서
   `python -m unittest test_manager_cross_harness_e2e test_manager_claude_runtime_e2e -v`를 실행한다.
   전자는 가짜 Claude CLI·loopback Responses·합성 OAuth 계정으로 양방향 생성,
   추가 지시, 결과 수신, 중단, 선택 계정 토큰 사용과 다음 턴/재개 설정을 확인한다.
   환경 변수가 없어서 제외된 검사를 통과로 계산하지 않는다.
5. 새 실행기의 공유 편집·공통 저장소 검증 보고서를 만들고 기존 활성화 도구의
   같은 바이너리 검증을 통과시킨다. 소스 누적 패치·해시/트리 복원 검증과
   관리자 후보의 고정 파일 해시를 확인한 뒤 다음 실행용으로 게시한다.
   현재 사용자 작업을 강제 재시작하지 않는다.
