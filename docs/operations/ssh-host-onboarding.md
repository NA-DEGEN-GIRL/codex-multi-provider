# 새 Linux SSH 호스트 준비와 기록 복원

작업 공간 앱은 Windows에서 실행하고, SSH 호스트에는 CLI와 관리 런타임을 둡니다.
이 문서는 새 서버를 준비할 때 확인할 위치와 현재 코드의 적용 범위를 설명합니다.
기존 PC/VM의 전체 이전 순서는 [HANDOFF](../HANDOFF.md)와 [복원 합격 기준](../RESTORE-CHECKLIST.md)을 먼저 확인하세요.

## 설치 위치

아래 `$HOME`은 SSH로 로그인한 **Linux 사용자**의 홈입니다.

| 용도 | 위치 |
|---|---|
| 터미널용 공식 CLI 진입점 | `$HOME/.local/bin/codex` |
| 공식 CLI 전체 패키지 | `$HOME/.local/opt/codex/<version>/` |
| 일반 CLI의 복원 대상 기록 | `$HOME/.codex/` |
| 작업 공간 앱의 공유 실행 파일 | `$HOME/.local/share/codex-control-center/runtime/<version-digest>/` |
| 프로필별 실행기·정의 | `$HOME/.local/share/codex-control-center/profiles/<profile-id>/` |
| 프로필별 관리 기록 | 위 경로의 `codex/` |
| 관리 기록 출처 목록 | `$HOME/.local/share/codex-control-center/catalog-sources.json` |
| 일반 기록을 포함한 출처 목록 | `$HOME/.local/share/codex-control-center/catalog-mixed-sources.json` |

공식 CLI는 [공식 Linux 패키지](https://github.com/openai/codex/releases)로 설치할 수 있습니다.
다운로드한 릴리스의 해시를 검증하고 전체 패키지를 보존하세요. `bin/codex`만 복사하면
같은 패키지의 샌드박스·실행 보조 파일이 누락될 수 있습니다. 이 방식에는 npm이 필요하지 않습니다.
선택한 버전·출처·해시를 설치 경로의 `INSTALLATION.json` 등에 기록합니다.

관리 런타임은 Windows 앱의 `RemoteManager.prepare()`가 호환되는 번들을 설치합니다.
공식 CLI와 관리 런타임의 버전은 서로 다를 수 있습니다. 임의로 관리 실행 파일을 공식 CLI로
교체하지 마세요. 큰 실행 파일은 같은 호스트의 프로필들이 공유하고, 설정과 기록은 분리합니다.
Windows 로그인 갱신 토큰을 Linux에 복사할 필요는 없습니다. 터미널에서 직접 CLI를 쓰는
로그인은 작업 공간 앱의 프로필 연결과 별개입니다.

## 등록과 적용

1. Windows 사용자 SSH config의 실제 별칭·대상 사용자·키·호스트 키 검증을 확인합니다.
   같은 서버에 별칭이 여러 개이면 앱에는 대표 별칭 하나를 등록합니다.
2. SSH에서 `$HOME`, Python 3, CLI 위치를 확인하고, 이전 서버와 다른 host identity로 준비합니다.
   이전 VM의 실행 중 상태 파일을 새 서버의 현재 상태로 사용하지 않습니다.
3. 기존 관리 경로로 각 프로필을 준비하고, 성공한 binding과 기록 source를 Store에 등록합니다.
   실행 중인 프로필의 binding을 보완할 때는 generation 검증과 기존 lock 규약을 지킵니다.
4. 공통 앱 설정에 연결 선언·자동 연결을 추가합니다. 현재 프로필의 네이티브 화면 설정은
   프로세스 메모리에도 있으므로 실행 중 JSON 파일만 덮어쓰지 않습니다.
5. **각 프로필의 작업을 마친 뒤 `다시 열기`** 하면 공통 연결을 가져옵니다. 관리창의 X는
   프로필을 백그라운드에 유지하므로 관리창만 닫았다 여는 것은 프로필 재실행과 다릅니다.

관련 구현: `scripts/manager_core/remote.py`, `ssh_shim.py`, `store.py`,
`app_preferences.py`, `app_workspace.py`.

## Ubuntu의 샌드박스 실행 검사

SSH 접속과 `thread/list` 성공만으로 샌드박스가 정상이라고 판정하지 않습니다.
실제 관리 연결에서 `command/exec`로 읽기 전용 간단한 명령까지 확인합니다.

Ubuntu의 비특권 user namespace 제한 때문에 `bwrap`에서 `RTM_NEWADDR: Operation not permitted`
등이 발생할 수 있습니다. 실제 커널/AppArmor 거부 로그와 실행 파일 경로를 먼저 확인하세요.
[Ubuntu 24.04 안내](https://documentation.ubuntu.com/release-notes/24.04/)에 따라
확인한 샌드박스 실행 파일의 **정확한 경로**에 한정된 AppArmor profile로 user namespace를
허용할 수 있습니다. 시스템 전체의 제한을 해제하지 않습니다.

관리자가 검토해 `/etc/apparmor.d/codex-workspace-bwrap`에 작성할 예시:

```text
abi <abi/4.0>,
include <tunables/global>
profile codex_workspace_bwrap "/home/USER/.local/share/codex-control-center/runtime/VERSION-DIGEST/bwrap" flags=(unconfined) {
  userns,
}
```

`USER`, `VERSION-DIGEST`는 설치 결과로 대체해야 하며 그대로 실행할 명령이 아닙니다.
공식 CLI의 보조 실행 파일에도 같은 제한이 확인되면 별도의 정확한 경로로 추가합니다.
파일을 root 소유로 관리하고 `sudo apparmor_parser -r /etc/apparmor.d/codex-workspace-bwrap`로
적용한 뒤 다시 검사합니다. **런타임 업데이트로 경로가 바뀌면 이 규칙도 재검토해야 합니다.**
현재 관리 앱이 이런 OS 정책 파일까지 자동 갱신한다고 가정하지 마세요.

## SSH Claude 작업의 로그인 (수정 121)

- SSH Claude 작업은 호스트의 Claude 로그인을 쓰지 않습니다. Windows가 턴마다 그 프로필의 PC 로그인 토큰(약 8시간)이나,
  계정에 저장한 장기 토큰(`claude setup-token`, 1년)을 빌려 줍니다. 토큰은 실행 중인 러너와 CLI에 소켓으로만 전달되고
  파일·환경 변수·로그에 남지 않습니다.
- 턴 중 토큰이 만료되면 런타임이 새 토큰을 받아 실행 중인 CLI에 넣거나 같은 세션으로 다시 실행합니다.
- 이 동작에는 수정 121 Linux 런타임 묶음과 수정 121 helper가 모두 필요합니다. helper는 준비 때 프로필마다 설치되므로,
  SSH 업데이트로 준비가 다시 실행되지 않은 Claude 바인딩은 직접 다시 준비합니다. 그 전에도 작업은 예전처럼 동작합니다
  (턴 중 갱신·장기 토큰 없음).
- 장기 토큰은 관리창의 Claude 로그인·설정 창에서 저장합니다. 다시 준비한 바인딩에서는 저장하는 즉시 다음 SSH 작업부터 쓰이고,
  창의 SSH 줄이 사용하는 연결 수를 보여 줍니다.
- 같은 Linux 사용자로 실행되는 다른 프로세스(에이전트가 실행한 도구 포함)는 작업 중 토큰을 읽을 수 있습니다.
  그 Linux 계정을 신뢰하지 않는 사용자·프로그램과 함께 쓰지 마세요. `kernel.yama.ptrace_scope`가 0인 호스트는 같은 사용자의
  프로세스 메모리를 읽기 쉬우므로, 관리자가 1 이상으로 두는 것을 권장합니다.

## Claude 계정이 있는 프로필의 core 파일 제한

수정 121부터 SSH 관리 런타임은 Windows에서 빌려 온 Claude 로그인 토큰과, 계정에 저장된 장기 토큰을
Claude 실행기에 전달합니다. 같은 Linux 사용자의 다른 프로세스(에이전트가 실행한 도구 포함)가 core 파일을
만들게 해서 토큰을 읽지 못하도록 다음 제한을 둡니다.

- 준비된 계정 중에 Claude 계정(주 계정이나 프리셋 역할)이 있으면, 런타임 데몬이 core 파일 크기
  hard 제한 0으로 시작합니다. 장기 토큰을 저장하지 않았어도 적용됩니다. 빌린 토큰도 같은 경로로 전달되고,
  장기 토큰은 다시 준비하지 않아도 저장하는 즉시 쓰이기 때문입니다.
- Claude 실행기도 턴마다 같은 제한을 둡니다.

이 제한은 런타임이 시작하는 모든 프로세스가 물려받고, 일반 사용자는 다시 올릴 수 없습니다.
그래서 이런 프로필에서는 다음과 같이 동작합니다.

- 에이전트가 실행하는 명령(같은 프로필의 GPT 턴 포함)에서 `ulimit -c unlimited`가
  `Operation not permitted`로 실패합니다. `set -e` 스크립트는 그 줄에서 멈춥니다.
- 충돌한 프로그램이 core 파일을 남기지 않습니다.

core 파일이 필요한 디버깅은 관리 프로필 밖의 일반 SSH 세션에서 실행하세요. 수정 121 helper로 준비한
프로필은 다음 런타임 시작부터 이 제한이 적용됩니다.

## 세션 복원과 검증 범위

`catalog_legacy.discover()`는 기본 Codex home인 `$HOME/.codex`를 검색하고, 호스트의
`~/.local/share/codex-control-center/catalog-mixed-sources.json`에 이미 등록된 기존 home을 폴더가
남아 있는 동안 유지합니다. 계정 레지스트리는 읽지 않습니다. 관리 프로필들은 별도의 공통 source 목록으로
검색합니다. 기본 home 디렉터리는 관리 런타임을 시작하기 전에 만들어 두면 빈 상태에서도 목록에 포함할 수 있습니다.
다른 임의 경로의 백업을 자동으로 찾아내는 기능은 아닙니다.

수정 121 이전 릴리스는 `~/.config/llm-usage/config.json`에 있는 Codex home도 같은 파일에 등록했습니다.

- SSH 대화 목록은 항상 현재 릴리스의 helper로 읽습니다. 관리 앱이 `llm_usage` 발견 오류를 허용하는 이유는
  두 가지입니다. 이미 저장된 SSH 목록 캐시에 그 코드가 남아 있을 수 있고, 현재 helper는 위 파일의 기존 등록을
  읽지 못할 때 그 코드를 보고합니다.
- 프로필마다 준비 때 설치된 `helpers/<digest>`의 helper는 SSH 관리 프로필을 시작할 때마다 실행됩니다.
  수정 121 이전에 설치된 helper는 시작할 때마다 이 레지스트리를 계속 읽습니다. helper 해시는 설정 지문에
  들어가지 않으므로, 일반 사용만으로는 바뀌지 않습니다. 직접 다시 준비하거나 설정·런타임 변경으로 준비가
  다시 실행될 때만 바뀝니다.
- 그 helper는 레지스트리를 읽지 못하면 이전 목록을 보존하려고 시작을 실패시킵니다
  (`legacy source discovery unavailable`). 호스트에서 llm-usage를 정리할 때는 `~/.config/llm-usage` 폴더를
  통째로 지우거나(파일이 없으면 정상입니다) 그 호스트의 SSH 바인딩을 먼저 다시 준비하세요. 파일을 비우거나
  편집하지 마세요.
- 위 파일이 손상되면 수정 121 이후 helper도 같은 이유로 시작을 거부합니다. 이때는 그 파일만 지우면
  다음 시작이 기본 home과 관리 프로필로 다시 만듭니다.

일반 CLI 기록은 원래 `sessions/`, `archived_sessions/`의 구조와 관련 인덱스를 보존해 복원합니다.
SQLite 파일은 해당 기록의 작성 프로세스를 중단하거나 일관된 백업을 만들어 옮깁니다.
JSONL만 보유한 경우 인덱스 재구성·발견 여부를 별도로 확인하세요.
새 서버에서 프로젝트 경로가 바뀌면 기록의 `cwd`, rollout 경로, 프로젝트 연결도 점검해야 합니다.

이전 **관리 프로필**의 기록은 원래 출처/profile ID와 연결된 `codex/` 경로를 확인해 복원합니다.
임의로 일반 `.codex`와 합치거나 동일 작업을 여러 home에 복제하지 마세요.
이전 `native-instance.json`, PID/socket/lock, 실행 정의, 로그인 비밀값을 새 서버의 활성 상태로
덮어쓰지 않습니다. Windows 작업 메모와 바로가기의 이전 host ID 연결은 별도 검증 대상입니다.

최소 확인 항목:

- CLI `--version`, 실제 설치 경로, SSH host identity 일치.
- 각 프로필의 초기 연결·계정 연결·`thread/list`·읽기 전용 `command/exec` 성공.
- 관리 source 수와 일반 `.codex` 발견, catalog 오류 없음.
- 재실행한 프로필의 네이티브 SSH 메뉴에 연결 하나가 보이고 연결됨.
- 복원 이후 대표 작업의 제목·본문·프로젝트 경로가 맞으며 다른 프로필에서도 보임.
- 이어 쓰기는 실제 모델 호출이므로 사용자가 복원을 확인한 뒤 별도로 시험.

빈 카탈로그의 조회 성공은 **복원 성공이나 모든 프로필의 화면 표시 성공을 뜻하지 않습니다.**
토큰 만료에 따른 `login_needed`는 서버 설치와 구분하고 해당 Windows 프로필의 로그인으로 해결합니다.
