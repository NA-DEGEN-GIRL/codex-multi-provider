# 개발자용 참고 문서

이 문서는 내부 동작을 점검하거나 코드를 수정할 때 읽는 자료입니다. 일반 사용은 [처음 사용 가이드](../README.md)와 [Windows 앱 사용법](desktop-launch.md)만으로 시작할 수 있습니다.

기준일: 2026년 9월 12일. 아래 상대 경로의 기준 폴더는 `C:\dev\codex-multi-provider`입니다.

## 1. 용어와 실제 구조

| 용어 | 이 프로젝트에서의 뜻 |
|---|---|
| 부모 / 오케스트레이터 | 사용자 요청을 받고 일을 배분하는 Astra |
| 자식 / subagent | 부모에게 일부 작업을 받아 수행하는 별도 AI 작업 |
| provider / 공급자 | 모델 요청을 처리하는 서비스. 현재 OpenAI와 DeepSeek |
| 런타임 | 도구 실행과 부모·자식 작업 관리를 담당하는 Codex 실행 엔진 |
| 패치 | DeepSeek 자식 지원을 위해 런타임 소스에 추가한 변경 |
| 프로필 | 특정 실행 환경의 설정·역할·모델 정보가 들어 있는 폴더 |
| 세션 | 한 작업에서 주고받은 메시지와 실제 실행 정보를 저장한 기록 |

부모는 OpenAI `gpt-6-astra`입니다. GPT 자식은 `collaboration.spawn_agent`, DeepSeek 자식은 `external_agents.spawn_agent`의 `agent_type="deepseek"`로 생성합니다. 외부 자식도 Codex의 공통 에이전트 관리 안에 생성됩니다.

외부 자식은 `fork_turns="none"`으로 새 문맥에서 시작합니다. 부모가 필요한 작업 설명을 메시지로 전달하며, provider 사이에서 전체 또는 일부 대화 이력을 복제하는 요청은 거절합니다. OpenAI 전용 암호화 이력의 호환성이 추가된 것은 아닙니다. 평문 메시지 전달 경로를 사용하고, 후속 작업은 외부 메시지 도구로, 상태 확인과 대기는 공통 관리 도구로 처리합니다.

허용한 provider와 역할 파일에 따라 외부 모델을 선택합니다. GPT 모델 이름을 DeepSeek로 치환하지 않습니다. 별도 Claude Code 프로세스나 MCP 질의 래퍼를 자식으로 실행하는 구조도 아닙니다. 부모 모델 선택 메뉴에 DeepSeek를 추가하는 기능은 포함하지 않습니다.

### Flash max 정책

- `scripts/prepare_profiles.py`가 `deepseek-flash` 역할의 `model_reasoning_effort`를 `max`로 생성합니다. 로컬 모델 카탈로그도 기본 수준과 허용 수준을 `max`로 제한합니다.
- `scripts/desktop_launch.py`가 기존 실험용 앱 프로필의 생성·재실행·공급자 갱신 때도 이 정책을 적용합니다. GPT 모델의 추론 수준과 앱의 다른 설정은 유지합니다.
- 외부 spawn 경로는 호출자가 모델·추론 수준을 덮어쓰는 요청을 거절하고 역할 설정을 사용합니다.
- 직접 Flash를 호출하는 실험도 `max`를 선택합니다. 실험 결과에는 실제 세션의 `reasoning_efforts`를 수집하고, Flash가 max 이외로 기록되면 실패 처리합니다.
- 적용 전부터 실행 중이던 실험용 앱은 종료 후 다시 열고 새 작업으로 확인합니다. 과거 세션 값은 수정하지 않습니다.

## 2. 폴더 지도

| 위치 | 용도 |
|---|---|
| `Open-Lab.cmd` | 모델 실험실 실행 |
| `Open-Experimental-Codex.cmd` | 별도 실험용 Windows 앱 실행 |
| `scripts/` | 실행기, 설정 생성, 테스트·진행 표시 코드 |
| `upstream/` | 비교용 공식 소스 |
| `runtime/` | 패치 소스가 들어 있는 별도 Git worktree |
| `artifacts/upstream/` | 수정하지 않은 비교용 빌드 |
| `artifacts/runtime/` | 패치 실행 파일과 동반 실행 파일 |
| `profiles/upstream/`, `profiles/runtime/` | CLI 실험용 설정과 세션 |
| `profiles/desktop-runtime/` | 실험용 앱의 Codex 설정·세션·로그인 |
| `profiles/desktop-runtime-ui/` | 실험용 앱의 UI 데이터 |
| `profiles/provider.json` | DeepSeek 연결 주소·프로토콜·모델 ID |
| `profiles/deepseek.dpapi` | Windows 현재 사용자용으로 암호화한 API 키 |
| `tests/sandbox/<실행 ID>/` | 실험실이 매번 만드는 작업 폴더 |
| `artifacts/results/<실행 ID>/` | 해당 실험의 보고서와 진행 기록 |
| `work/custom-task.txt` | 마지막으로 제출한 자유 작업 요청문 |
| `work/desktop-launch-last.json` | 마지막 앱 실행기의 진단 결과 |
| `work/launcher-error.log` | 더블클릭 실행 준비 중 발생한 오류 |

실험용 앱의 실제 프로젝트 파일은 앱에서 선택한 폴더에 생깁니다. 위 `tests/sandbox`는 CLI 실험실에 해당합니다.

### 결과 파일 읽기

| 파일 | 내용 |
|---|---|
| `progress.log` | 사람이 읽을 수 있는 진행 기록. 실행 중에도 기록 |
| `report.json` | 처음에는 `RUNNING`, 종료 후 상태·파일 경로·실제 모델·provider 정보 |
| `events.jsonl` | 실행 중 계속 저장하는 필터링된 이벤트 |
| `events.json` | 종료 시 저장하는 필터링된 이벤트 묶음 |

새 진행 로그는 공개 메시지, 도구 활동과 출력을 표시하고 계정·인증 이벤트와 내부 추론 내용은 제외합니다. 이 필터가 모든 도구 출력의 임의 민감정보까지 자동 제거한다는 의미는 아닙니다.

## 3. CLI로 실행하기

일반 사용자는 `.cmd` 실행기를 사용하면 됩니다. 아래 명령은 Python·Git·PowerShell 7을 찾을 수 있는 개발용 터미널에서 실행하는 예시입니다. `python`이나 `pwsh`를 찾지 못하면 설치 경로를 먼저 확인하세요. 더블클릭 실행기는 필요한 경로를 자식 프로세스에 직접 준비합니다.

```powershell
Set-Location -LiteralPath 'C:\dev\codex-multi-provider'
python .\scripts\live_test.py --mode upstream --scenario baseline
python .\scripts\live_test.py --mode runtime --scenario mixed
python .\scripts\live_test.py --mode runtime --scenario selection
```

각 줄은 별도의 실제 모델 테스트입니다. `mixed`는 Astra·DeepSeek·Sol 위임과 같은 DeepSeek 자식 후속 작업을, `selection`은 Astra의 자율 선택을 확인합니다.

Windows 앱과 같은 실행 옵션을 사용하는 혼합 테스트:

```powershell
python .\scripts\live_test.py --mode runtime --scenario mixed --desktop-host
```

이 명령은 `features.code_mode_host=true`로 app-server를 실행합니다. Windows 앱 화면을 조작하는 테스트는 아닙니다.

자유 작업은 UTF-8 요청 파일을 먼저 작성한 뒤 실행합니다.

```powershell
python .\scripts\live_test.py --mode runtime --scenario custom --prompt-file .\work\my-task.txt
```

CLI 테스트는 별도 app-server 프로세스와 새 작업 폴더를 사용합니다. 자유 작업의 모델 턴 제한은 600초입니다. 이 프로젝트의 실험은 한 번에 하나씩 실행합니다.

### 앱 점검·설정 갱신

```powershell
python .\scripts\desktop_launch.py --check
```

읽기 전용 점검으로, 파일 생성·키 복호화·앱 실행을 하지 않습니다.

저장한 DeepSeek 주소·모델을 기존 실험용 앱 설정에도 반영하려면 **실험용 앱을 종료한 뒤** 실행합니다. 이 명령은 설정 갱신 후 앱도 엽니다.

```powershell
python .\scripts\desktop_launch.py --refresh-provider
```

공급자 URL, DeepSeek 역할의 모델·카탈로그 연결, 카탈로그 첫 모델 이름을 갱신하고 Flash max 정책을 적용합니다. 일반 재실행에서도 Flash max 정책은 적용하지만, 바뀐 공급자 주소·모델은 위 명시적 갱신이 필요합니다. 갱신은 기존 앱의 다른 설정과 로그인을 보존합니다.

원래 앱 열기:

```powershell
python .\scripts\desktop_launch.py --mode original
```

## 4. Windows 앱 연결 방식과 확인 근거

설치된 앱 실행 파일은 그대로 두고 시작할 때 아래 경로를 지정합니다.

| 설정 | 실험용 값 |
|---|---|
| `CODEX_CLI_PATH` | `artifacts/runtime/codex.exe`의 절대 경로 |
| `CODEX_HOME` | `profiles/desktop-runtime`의 절대 경로 |
| `CODEX_ELECTRON_USER_DATA_PATH` | `profiles/desktop-runtime-ui`의 절대 경로 |
| 네이티브 `--user-data-dir` 인수 | 위와 같은 실험용 UI 데이터 경로 |

확인 설치본은 `OpenAI.Codex_26.903.9818.0_x64__2p2nqsd0c76g0`입니다. `app/resources/app.asar`에서 다음 구현을 확인했습니다.

- `.vite/build/src-B6LqG3ek.js`: `kU()`가 `CODEX_CLI_PATH`를 읽고 `uU()`가 번들 실행 파일보다 먼저 선택합니다. `dU()`/`pU()`는 CLI를 `-c features.code_mode_host=true app-server --analytics-default-enabled`로 실행합니다.
- `.vite/build/bootstrap-Cv3cpqAG.js`: 별도 UI 경로를 `app.setPath("userData", ...)`에 지정하고 인스턴스 잠금을 획득합니다.
- `.vite/build/main-CMBCj4XL.js`: 별도 UI 경로를 지정하면 셸 환경 로딩 후에도 `CODEX_HOME`을 보존합니다.
- `src-B6LqG3ek.js`의 `iU()`는 앱 서버에 프로세스 환경을 전달합니다. `ZO()`는 Crashpad 관련 값을 제거합니다.

이 설치본은 Owl/Chromium 셸입니다. 환경 변수만 사용한 첫 시도는 JavaScript 초기화 전에 기존 앱으로 전달됐습니다. 설치된 `ChatGPT.exe`와 `chrome.dll`에서 확인한 `--user-data-dir` 인수를 함께 전달한 뒤 독립 앱 실행에 성공했습니다.

확인 당시 실험용 앱 PID `30152`가 패치 앱 서버 `118512`를 직접 실행했고, 원래 앱 `82332`와 원래 앱 서버 `81488`도 유지됐습니다. 이 숫자는 당시 기록이며 현재 PID로 쓰면 안 됩니다. 이후 사용자 Windows 앱 작업의 세션·파일·테스트 결과까지 확인했습니다. [검증 범위](implementation-status.md)를 참고하세요.

실행기는 시작한 앱의 직접 자식 프로세스를 몇 초간 확인합니다. 패치 앱 서버 경로가 일치하면 `패치 앱 서버 실행 확인`을 표시합니다. 기존 창으로 전달됐거나 시작이 지연되면 확인 대기 상태를 남깁니다. 단순 실행 요청과 실제 시작 확인은 구분합니다.

상위 프로세스의 `CODEX_*` 값을 제거한 뒤 실험 모드에 필요한 값만 앱에 지정합니다. 앱 업데이트 때 설치 경로는 다시 찾지만, 내부 환경 변수나 앱 서버 프로토콜 변경에는 재검증·수정이 필요할 수 있습니다.

## 5. 로그인과 API 키 처리

DeepSeek 키는 Windows 현재 사용자용 DPAPI로 보관하고 실험 프로세스에 환경 변수로 전달합니다. 셸 도구 환경에서는 제외하며 실행기 인수·진단 결과에는 기록하지 않습니다.

CLI 실험실은 현재 Codex 로그인에서 access token만 읽어 별도 app-server에 일시 전달합니다. refresh token을 복사하거나 사용하지 않으며 CLI 실험 홈에 로그인 파일을 만들지 않습니다. 이 경로는 실험적 app-server 인증 API를 사용합니다.

실험용 Windows 앱은 자체 로그인과 인증 파일을 사용합니다. 앱 프로필을 처음 만들 때 CLI 프로필의 설정·역할·모델 카탈로그만 가져옵니다. 원래 앱의 인증 파일을 복사하지 않습니다.

## 6. 소스 출처와 버전 호환 수정

| 항목 | 값 |
|---|---|
| 공식 기반 | OpenAI Codex `rust-v0.153.4` |
| 기반 커밋 | `3d2ee51ca2d5db578f328aa75e20aa22c0197c9a` |
| 참고한 외부 provider 패치 | Bozentan/codex `af3072ebbcde8d47bb8592a6a3226ef1b83b560a` |
| 초기 로컬 패치 커밋 | `e13fcc1a0cceb3f428599bd5c514b5c104f3c6bc` (현재 누적 패치에는 이후 수정도 포함) |
| 내보낸 패치 | `patches/codex-0.153.4-cross-provider.patch` |

이 버전에 맞추며 다음을 조정했습니다.

1. 0.153.4에 없는 `ResponseItem::ConfigurationUpdate` / `ConfigurationReasoning` 처리와 테스트 자료를 제외했습니다. 암호화 작업 거절과 요청 정규화 검사는 유지했습니다.
2. 재개 테스트에 이 버전의 `decode_rollout_line(serde_json::Value)`를 사용했습니다.
3. 공식 lockfile의 로컬 패키지 149개 버전을 `0.0.0`에서 `0.153.4`로 정규화했습니다. 외부 의존성 기록은 동일합니다. 근거는 `work/lockfile-audit.json`입니다.
4. 공급자 토큰 검색 테스트가 다른 공급자의 ChatGPT 인증에 의존하지 않고 해당 공급자 endpoint를 조회하도록 수정했습니다. 실험실은 외부 모델 카탈로그를 명시하며, 테스트 응답에는 모델 지침 템플릿을 포함했습니다.

### 빌드와 회귀 검사

준비된 Rust/MSVC 환경에서 실행합니다. 공식 MSVC 설정 도우미, Rust 1.95, SHA256을 확인한 OpenAI rusty-v8 v150.4.0 Windows 아카이브·바인딩을 사용했습니다.

```powershell
pwsh -NoProfile -File .\scripts\build.ps1 -Source runtime
pwsh -NoProfile -File .\scripts\test-runtime.ps1
```

공식·패치 소스는 Cargo 빌드 캐시를 따로 사용합니다. `stamp_build.py`가 외부 자식 구현 포함 여부와 SHA256을 기록합니다. 현재 실행 파일은 개발용 디버그 빌드입니다.

내보낸 패치는 깨끗한 공식 `rust-v0.153.4` 소스 기준입니다. 이미 패치된 `runtime/`에 중복 적용하지 않습니다. 실제 실행 및 회귀 검사 결과는 [검증 기록](implementation-status.md)에 있습니다.

### 파일 동일성 확인값

SHA256은 파일이 바뀌었는지 대조하는 값입니다. 실행 파일 해시는 초기 실험 당시 값입니다. 현재 소스는 [패치 복원 안내](../patches/README.md)를 따릅니다.

| 파일 | SHA256 |
|---|---|
| 테스트한 패치 런타임 | `72e361578b4a736f67b11bfd62ea65f2807bef200e1e7a32d111020d1998a6e0` |
| 현재 누적 패치 | `patches/runtime-source.json`의 SHA256 및 적용 후 트리 ID 참고 |
| 당시 설치된 원본 Codex CLI | `3d6ca7085c932b62ef4ee4877e92f15b050fb94b2eb8e6c10a346a06248c6004` |

소스 출처: [OpenAI Codex](https://github.com/openai/codex), [참고한 외부 provider 변경](https://github.com/Bozentan/codex/commit/af3072ebbcde8d47bb8592a6a3226ef1b83b560a). 법적 고지와 출처 표기는 프로젝트 루트의 `THIRD_PARTY_NOTICES.md`에 있습니다.
