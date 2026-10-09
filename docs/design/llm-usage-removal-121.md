# 수정 121 — llm-usage 연동 정리

## 1. 배경

- 초기에는 별도 로컬 도구 llm-usage와 연동했다.
  - 계정·별칭 가져오기, Codex 사용량 수집, 30초마다 계정 동기화.
  - SSH 별칭 동기화, SSH 기록 목록용 계정 레지스트리 검색, "llm-usage 계정 연결" 창.
- 사용자는 llm-usage를 더 이상 쓰지 않는다. 그런데도 다음이 계속 돌았다.
  - 관리 상태 조회가 30초마다 저장소 옆 `llm-usage/src`를 `sys.path`에 넣고 계정 목록을 읽었다.
  - SSH 대화 목록 읽기(5초마다)가 호스트마다 `~/.config/llm-usage/config.json`을 확인했다.

## 2. 없앤 것

새 릴리스를 활성화한 뒤부터 적용된다. 실행 중인 이전 릴리스는 그때까지 그대로 동작한다.

- **계정 동기화:** 상태 조회의 동기화와 `sys.path` 추가를 없앴다.
  `manager_core/accounts.py`, `manager_core/remote_accounts.py`, `remote_helpers/account_alias.py`를 지웠다.
- **관리 명령:** `profile.bind`, `accounts.list`를 Python 처리기, Rust 서비스 허용 목록, 빌드하지 않는 Supervisor 허용 목록에서 지웠다.
  호출하면 "지원하지 않는 관리 명령입니다."를 돌려준다.
- **↻(`accounts.refresh`):** Windows 로그인(native/source) 프로필만 확인한다.
  확인할 프로필이 없으면 "새로 확인할 Windows 로그인 계정이 없습니다."를 보여 준다.
- **관리창:** 쓰지 않던 "llm-usage 계정 연결" 창을 지웠다. 상태 문구는 "기존 연결 계정"이다.
- **프로필 추가:** `Store.add_profile`은 가져온 계정 인자(`usage_account_id`, `source_home`)를 받지 않는다.
  `usage:` source도 새로 만들지 않는다.
- **SSH 기록 검색:** `catalog_legacy.discover()`는 계정 레지스트리를 읽지 않는다.

## 3. 남긴 호환 경로

`state.json`은 다시 쓰지 않는다. 기존 프로필의 home, 기록, 이름, `usage:` source는 그대로다.

- **가져온 프로필:**
  - `usage_account_id`, `source_home`, `account_missing`은 남은 데이터다.
  - native가 아닌 가져온 프로필은 계속 `source_home`을 인증 원본으로 쓴다.
  - 관리창에서 별칭을 바꿀 수 있다.
  - "계정 없음" 표시는 더 이상 자동으로 지워지지 않는다.
- **`alias_authority`:** 이제 읽는 코드가 없다. native 로그인으로 바꿀 때만 계속 기록한다.
  수정 121 이전 릴리스로 되돌리면, 이 값이 없는 `usage_account_id` 프로필은 이름을 바꿀 수 없기 때문이다.
- **사용량:**
  - 백그라운드 조회는 `account_fingerprint`가 있는 프로필만 한다.
  - 런타임이 로그인 연결을 확인한 적 없는 가져온 프로필에는 지문이 없다. 이런 프로필은 마지막으로 가져온 값이 남는다.
    관리창은 그 값을 관찰 후 10분이 지나면 "이전 값"으로 표시한다.
  - 그 프로필을 한 번 실행하면 지문이 저장되고 조회 대상이 된다.
- **전체 기록 보기 프로필:** native 부모의 보기 프로필은 부모 자신의 home을 인증 원본으로 쓴다.
  이전 릴리스가 만든 보기 프로필도 다음에 열 때 바뀐다.
- **SSH 기록 목록:**
  - 호스트의 `catalog-mixed-sources.json`에 이미 등록된 home은 폴더가 남아 있는 동안 유지된다.
  - 한 호스트에서 서로 다른 릴리스가 준비한 프로필이 이 파일을 번갈아 써도, 이전 helper가 넣은 home을 새 helper가 지우지 않는다.
  - 관리창 목록도 같은 파일을 읽어 런타임과 같은 home을 보여 준다.
  - `llm_usage` 발견 오류 코드는 두 경우에 쓰인다. 하나는 이 파일의 기존 등록을 읽지 못한 경우이고,
    다른 하나는 저장된 SSH 목록 캐시에 이전 코드가 남은 경우다.

## 4. 저장소 밖에 남은 것과 정리 순서

1. 새 릴리스를 활성화하기 전에는 저장소 옆 `llm-usage` 체크아웃과 `%LOCALAPPDATA%\llm-usage`를 지우지 않는다.
   이전 릴리스가 30초마다 읽는다.
2. 사용자의 Claude Code 설정(`statusLine`)이 `llm-usage _claude-statusline …`을 실행한다.
   저장소 밖이라 바꾸지 않았다. llm-usage를 지우기 전에 사용자가 바꾸거나 지운다.
3. **SSH helper:**
   - 수정 121 이전에 준비한 SSH 프로필의 helper(`helpers/<digest>`)는 시작할 때마다 레지스트리를 계속 읽는다.
   - helper 해시는 설정 지문에 들어가지 않는다. 직접 다시 준비하거나 설정·런타임 변경으로 준비가 다시 실행될 때만 바뀐다.
   - 이전 helper는 레지스트리를 읽지 못하면 시작을 실패시킨다(`legacy source discovery unavailable`).
   - 호스트에서는 `~/.config/llm-usage` 폴더를 통째로 지우거나, 바인딩을 먼저 다시 준비한다. 파일을 비우거나 편집하지 않는다.
4. 가져온 Codex home의 `sessions` 링크는 `~/.codex`를 가리킬 수 있다. 링크만 지우고 대상은 지우지 않는다.
5. 호스트에 남은 `~/.local/bin/llm-usage`는 손으로 지워도 된다.
6. **런타임 문서(완료):** `runtime/codex-rs/app-server/README.md`의 "llm-usage metadata" 검색 문장을 "기본 `~/.codex`와
   `catalog-mixed-sources.json`에 이미 등록된 home"으로 고쳤다. 이전 helper가 다시 준비할 때까지 레지스트리를 읽을 수 있다는 점도 적었다.
   패치와 `patches/runtime-source.json`을 다시 만들었고(트리 `0511f3be…`), 빌드 서버의 `restore_runtime.py`가 같은 트리를 재현했다.
   런타임 코드의 llm-usage 주석 두 곳은 기존 프로필 home 설명이라 그대로 두었다.

## 5. 검증

- 단위 시험:
  - 없앤 명령이 상태를 바꾸지 않는지.
  - 가져온 프로필의 이름 변경.
  - 지문 없는 가져온 사용량이 "이전 값"으로 보이는지.
  - 이전 보기 프로필의 인증 원본 교체.
  - 레지스트리 무시, 등록된 home 유지·삭제, 손상된 등록 파일 보존.
  - 관리창 목록이 등록된 home의 대화를 보여 주는지.
- Python 전체 시험(`test_launchers` 제외): 2,270개, 실패 0, 건너뜀 71. 알려진 `test_manager_ssh_pump` setUpClass 오류 1건은 그대로다.
- Rust 서비스 `cargo test --features process-fixture`: 68개 통과.
  - process-fixture 시험 두 개(`owned_process_launch…`, `stop_sweeps_past_a_reused…`)가 다섯 번 중 두 번 따로 실패했다.
    자식 PID 파일 읽기 경합으로 보이며, 이 수정과는 관계없다. 다시 실행하면 통과한다.
  - `cargo fmt --check`는 이 수정과 관계없는 기존 줄(`main.rs`·`tests.rs`)의 서식 차이를 보고한다.
- Shell과 Supervisor를 임시 출력 폴더로 빌드했다. 경고 0, 오류 0.
