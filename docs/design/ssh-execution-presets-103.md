# 수정 103 — SSH 실행 프리셋과 원격 교차 하위 에이전트

수정 102의 Windows 실행 프리셋을 SSH로 확장한다. 설치·선택과 실제 적용은
별개이며, 기존 실행기를 새 기능이 있는 것으로 간주하지 않는다.
아래 검증·배포 상태를 확인한 뒤 사용한다.

## 사용 방법과 적용 시점

프로필의 **실행 프리셋**에서 조합을 저장하고, SSH 작업 상단에서 선택한다.
이미 준비된 역할의 조합은 현재 답변을 중단하지 않고 다음 실행부터 바뀐다.
새 계정·모델 역할 추가에는 원격 실행 준비가 필요하다. 이 경우 선택을 보관하고
준비 필요 상태를 표시한다. 새 작업 기본값은 기존 작업에 소급하지 않는다.
작업에서 기본 설정을 명시적으로 선택한 기록도 보존한다.

프리셋 저장·삭제·기본값 변경은 연결된 SSH 호스트에 게시한다. 연결이 없으면
다음 연결 전에 게시한다. 이전 런타임이 이 기능을 지원하지 않으면 준비 필요로
표시하며, 기존 작업을 종료하거나 다른 설정으로 몰래 실행하지 않는다.

## 불변 실행 정의와 변경 가능한 선택

- `execution_preset_render.py`는 Windows와 SSH의 경로·인증 차이를 분리한다.
- SSH 정의의 `manager-execution-authority.json`은 승인된 역할과 계정 식별만
  담는다. 실행 정의 revision이 바뀌지 않는 동안 이 권한 목록은 불변이다.
- 원격 `execution-presets/<definition_revision>.json`과 선택 journal은 현재
  기본값·작업 연결을 담는다. source revision으로 오래된 쓰기를 거절하고,
  동일 revision 재전송은 멱등 처리한다. 실행 중인 이전 정의 파일은 새 설치가
  덮어쓰지 않는다.
- `execution_preset_remote_service.py`는 현재 프로필 generation과 실제 실행
  binding revision을 검사하고, 선택한 SSH 연결에만 설정 RPC를 보낸다.
- `execution_preset_reconnect.py`는 새 SSH transport를 시작하기 전에 보류된
  선택을 게시한다. 실패한 새 연결 이외의 기존 작업에는 영향을 주지 않는다.
- `execution_preset_replay.py`는 재연결 전부터 원격에 열려 있던 작업의 선택도
  다음 `turn/start` 전에 확인한다. 명시적인 작업 선택만 복원하며 새 작업 기본값을
  기존 작업에 덮어쓰지 않는다. 설정 요청의 성공 응답과 일치하는 적용 알림을
  모두 받은 뒤 원래 메시지를 전송한다. 순서가 뒤바뀌어도 처리한다.
- 이 확인 과정은 통신 루프에서 네트워크 호출이나 파일 잠금 대기를 하지 않는다.
  최대 8개 작업·8MiB·15초로 제한하고, 중간에 계정·호스트·선택이 바뀌거나 연결이
  끊기면 보류한 메시지를 실행하지 않은 상태로 오류를 돌려준다. 해당 작업 이외의
  통신은 계속하며, 유지보수 요청도 이 보류 상태를 관측한다.
- `manager/executionPresets/status`는 실제 initialize 응답의 기능 버전과
  관측한 적용 ID/revision만 반환한다. 토큰·대화 본문·임의 설정은 반환하지 않는다.

배포 파일의 사전 판정에는 실제로 읽는 `CODEX_MANAGER_EXECUTION_PRESETS`,
개인 인증 요청 `account/executionPresetAuthTokens/read`,
`CODEX_MANAGER_CLAUDE_AUTH` 세 표시를 모두 요구한다. 릴리스 최적화에서 사라지는
내부 심볼 이름으로 판단하던 오류도 수정했다. 파일 검사만으로 적용을 완료하지
않으며, 실제 연결의 기능 버전·소유 프로필 확인을 계속 요구한다.

빈 설정 RPC 응답은 접수 확인이다. 적용 알림을 받기 전에는 완료로 표시하지 않는다.
프리셋 이력·역할 수·직렬화 크기 제한과 프로필별 작업 소유 설정은 수정 102와 같다.

## 원격 계정 인증

원격 Codex는 Windows의 인증 파일 경로를 열지 않는다. 승인된 원격 역할은
`auth_source="manager_proxy"`로 등록하며, 실제 initialize를 수행한 연결에
고정된 `account/executionPresetAuthTokens/read` 요청으로 접근 토큰을 빌린다.
다른 연결은 그 응답을 주입하거나 요청을 가로챌 수 없다.

Windows의 `execution_preset_auth.py`는 승인된 소유 프로필·호스트·실행 revision,
선택된 계정 fingerprint/identity를 검사한다. 접근 토큰을 읽은 뒤에도 연결과 계정을
다시 확인한다. 다른 계정 또는 부모 계정으로 대체하지 않는다. 프로필 인증 파일,
refresh token, Windows 개인 경로는 원격 실행 묶음에 포함하지 않는다.

Claude에는 해당 계정의 유효한 접근 토큰만 실행 환경으로 전달한다. 원격 CLI가
별도 refresh token을 보관하거나 계정을 독립 갱신하는 구조가 아니다. 토큰 만료 시
등록 계정의 로그인을 갱신한 후 다음 실행에서 다시 빌린다. 대화 UUID와 기존 원장은
계속 사용하며, 토큰 때문에 대화를 새로 만드는 방식은 아니다.

Codex→Claude와 Claude→Codex는 같은 네이티브 작업 그래프의 생성·추가 지시·대기·
중단 경로를 사용한다. leaf 역할의 추가 위임은 막되 완료·후속 답변은 부모에게
전달하고, 중단은 작업 상태로 확인한다. 수정 103은 도구를 비활성화한 leaf의 완료
알림까지 차단하던 조건을 실행 actor의 그래프 버전 기준으로 수정한다. 부모 메시지가
다음 실행을 깨울 때도 해당 제공자의 실행 경로를 선택하도록 Claude 분기를 보완한다.

## Linux Claude CLI 설치

SSH 호스트에서 다음 명령으로 명시한 버전을 관리 도구 경로에 설치한다.
로그인이나 모델 호출은 수행하지 않으며 시스템 PATH/기존 CLI를 변경하지 않는다.

```sh
python3 scripts/install_managed_claude_linux.py --version 2.1.282
```

설치 위치는 `~/.local/share/codex-control-center/tools/claude/<version>/claude`다.
같은 디렉터리의 관리용 `claude` 링크를 원격 준비가 우선 발견한다. Linux x64/arm64,
Python 3, GnuPG와 HTTPS 접근이 필요하다. 공식 공개 키 fingerprint와 manifest 서명,
플랫폼별 SHA-256, 실제 `--version`을 확인한다. 기존 파일이 예상과 다르면 보존하고
실패한다. 설치 증거는 해당 버전 디렉터리의 `installation.json`에 보관한다.
검증 절차 근거: [Anthropic 설치 문서](https://code.claude.com/docs/en/setup#verify-the-manifest-signature).

## Windows 긴 저장 경로 보완

최종 관리 앱 검사 중, 프로필 아래의 긴 임시 경로에서는 메모 저장 시험이 실패하고
동일 바이너리의 짧은 임시 경로에서는 49개 검사가 통과하는 것을 확인했다.
기존 `manager/service/src/windows.rs::atomic`이 임시 파일을 만들 때 경로가
260자를 넘으면 일반 경로를 받은 `MoveFileExW`가 저장에 실패하는 문제였다.

기존 부모 디렉터리를 canonicalize해 Windows의 절대 확장 경로로 만든 후,
임시 파일과 최종 파일 모두에 적용한다. UNC 형태와 유니코드 파일명을 보존하고
손실 없는 UTF-16을 Win32에 전달한다. `sync_all`, 원자적 교체와 write-through는
유지한다. 실제 긴 유니코드 경로에 생성·교체하고 임시 파일 잔여물이 없는지 검사하는
회귀 시험도 추가했다. 시험별 실행 여부는 아래 표에서 구분한다.

## 검증 및 배포 상태

2026-10-01 검증은 합성 계정, 로컬 mock 제공자와 가짜 CLI로 수행했다. 실제 모델
호출과 유료 생성은 0회다. 실행 중 프로필/원격 서비스를 재시작하지 않았다.

| 항목 | 확인 결과 |
|---|---|
| Python 관련 회귀 | 341개 실행, 340개 통과·플랫폼 제외 1개 |
| 원격 패키지 빌드 캐시 | 3개 통과, 기존 캐시/패키지 보존과 companion hash 전달 확인 |
| 최종 패키지 지원 판정 수정 | 원격 프리셋 19개·패키징 3개·원격 Claude 9개 통과. 실제 릴리스 표시 사용과 필요한 표시 하나라도 없는 파일 거절 확인 |
| Windows 네이티브 | 다섯 실행 파일 빌드 및 CLI/app-server 버전 실행 성공 |
| Windows 교차 하위 에이전트 | 4개 통과: 양방향, 계정 분리, 후속 지시, 중단 상태, 프리셋 변경/재개 |
| Windows 기존 Claude 기능 | 10개 통과: 모델/effort 변경, 계정 왕복, 문맥, 중단, 승인, plan, 압축 |
| 동일 Windows 바이너리 기록 보존 | 공통 편집 15개·canonical storage 16개 확인 통과 |
| Linux 핵심 실행 검사 | core/app-server 범위의 28개 통과: 인증 연결 소유권·연결 종료, 계정 분리, 프리셋 변경·복원, 완료·후속 지시·중단 |
| Linux API 프로토콜 | 298개 통과·일반 실행 제외 1개. 안정·실험 TypeScript/JSON 스키마와 생성 코드 일치 확인 |
| Linux 교차 위임·Claude 회귀 | 최종 app-server에서 16개 통과(실행 시나리오 14개·설정 검사 2개). 실제 native stdio와 운영 SSH 인증 프록시를 통과한 양방향 위임, 추가 지시·중단, 세션 재사용·토큰 미기록 확인 |
| 최종 Linux 배포 파일 | 교차 위임·원격 인증 6개, 기존 기록 14개·페이지 기록 15개·문맥 재사용 6개 통과. 세 기능 표시·실제 artifact 판정·companion 해시 및 복사 후 파일 해시 확인 |
| 최종 관리 앱 자체 시험 | `-StageOnly -SelfTest`의 10종 모두 통과. 원래 긴 TEMP에서 실제 Rust 서비스와 WPF 메모 저장 49개 통과; 창 배치·프로필 순서·개인 스킬·SSH 업데이트 화면 포함 |
| 최종 프리셋 관리 화면 | 최종 후보의 숨겨진 WPF fixture 13개 확인 통과; 실제 계정/서비스 호출 없음 |
| 관리 서비스 단위 검사 | 실행 0개. Windows Application Control이 의존성 build script(`zmij`, 최종 긴 경로 수정 후에는 `serde`)를 OS 오류 4551로 차단해 시험 바이너리 생성 전 중단. 새 긴 경로 단위 시험도 미실행 |
| Rust 정적 검사 | core/protocol/app-server-protocol/app-server와 테스트의 범위 제한 Clippy 통과. 마지막 mailbox 분기는 이후 빌드와 실제 실행 검사로 확인 |
| 스키마/잠금 | 안정·실험 API 스키마와 config schema 재생성 성공. Linux `just bazel-lock-update` 성공, lock 내용 변경 없음 |

Windows 실행기 후보는 `artifacts/manager-runtime/releases/20261001-072058-ac1f2c/candidate.json`,
관리 앱 후보는 `artifacts/manager/releases/20261001-080642-677/candidate.json`이다.
공통 기록 증거는 `artifacts/results/shared-editing-a419c13d/report.json` 및
`artifacts/results/canonical-store-a08f7a37/report.json`이며 같은 후보 `codex.exe`의
SHA-256을 검증한다. 요약 증거는 Git 밖 `work/native-validation-103-windows.json`에 있다.
Linux 실행 증거는 `work/cross-harness-linux-103-fresh-proof.json`과
`work/cross-harness-linux-103-fresh.log`에 있다. 검사한 app-server SHA-256은
`a33c73c23a92366aa8e67e4a50062a84d00284829f354064df555d9c722b6b36`이다.

비활성 Linux 후보는 `work/candidates/remote103/linux-x86_64/manifest.json`이고,
증거는 같은 `remote103/proof/`의 `packaging-proof.json`, `gates-summary.json`,
`post-copy-verification.json`과 개별 로그에 있다. 이 묶음의 Codex SHA-256은
`d765eb778ea7a609e574f18709cc1cd464943914e715d9d0c7958dec0ebe1b17`이다.
실제 배포 파일에서 기능 판정까지 통과했으며, 활성 `artifacts/remote`로 옮기지 않았다.
이 Linux 빌드는 **glibc 2.39 이상**이 필요하다(bwrap은 2.38 이상).
검사 서버에서 실행을 확인했으며, 이전 glibc를 쓰는 다른 서버에는 호환되는
환경에서 다시 빌드해야 한다. Linux canonical migration 검사를 통과한 것으로
간주하지 않는다. 해당 기록 방식의 합격 증거는 위 Windows 검사다.

누적 런타임 패치는 별도 복원 디렉터리에서 트리
`1b201dcd91d9cedf8515ac8427f7064292d04823`으로 재현됐다. 패치 SHA-256은
`858db000295f38fe382faec28c8a0b593397e3a27e37b59ef53e5a6e31c405a1`이다.
원래 런타임 worktree의 index는 변경하지 않았다.

**현재 활성 배포는 교체하지 않았다.** 초기 후보의 서비스 실행은 Code Integrity
3077로 차단됐지만, 고정 Rust 1.95로 빌드한 최종 후보에서는 실제 서비스 실행과
10종 UI 시험을 통과했다. 보안 정책은 변경하지 않았다. 그 과정에서 드러난 긴 경로
메모 저장 문제를 수정했다. 창 배치 시험에는 현재 작업 정보를 빠뜨린 오래된 시험
데이터가 있어 실제 상태 기반 선택을 검증하도록 보완했고, 기존 스크롤·선택 유지
검사는 그대로 통과했다. 로그는 `work/manager-stage-103-final-ui.log`, 프리셋
결과는 `work/presets-ui-103-final.json`이다.

관리 서비스의 별도 `cargo test --locked`는 의존성 build script의 정책 차단으로
실행하지 못했다. `work/manager-service-tests-103-long-path.log`에 마지막 실패를
보존했다. 실제 메모 저장 시험 통과와 서비스 단위 시험 미실행을 혼동하지 않는다.
최종 후보들의 파일 해시·활성 포인터 유지 확인은 `work/final-103-proof.json`에 있다.

새 역할을 처음 준비하는 프로필은 작업을 마친 뒤 준비한다. 실제 데스크톱 SSH GUI와
실제 제공자의 과금 추론은 별도 확인 범위다. 전체 Rust workspace 검사를 수행한 것은
아니다. 기존 수정 98–102와 다른 작업자의 변경을 보존했으며 커밋·푸시는 하지 않았다.

## 이어서 적용할 때

1. 위 후보와 증거 파일의 해시를 확인한다. `work/`와 `artifacts/`는 Git 밖이므로
   다른 PC에서는 문서의 결과만 믿고 바이너리를 적용하지 말고 다시 빌드·검사한다.
2. 남은 관리 서비스 단위 시험은 빌드 도구 실행이 허용되는 환경에서
   `manager/service`의 `cargo test --locked`로 수행한다. 다시 빌드하는 경우
   `scripts/build-manager.ps1 -StageOnly -SelfTest`로 서비스 의존 UI 검사까지
   재확인한다. 이 빌드 명령은 현재 관리 앱 포인터를 바꾸지 않는다.
   정책 차단을 파일명 변경이나 다른 실행 경로로 우회하지 않는다.
3. 작업을 마친 유지보수 시점에 검증된 관리 앱과 네이티브 실행기를 함께 적용한다.
   네이티브 활성화는 `scripts/activate_manager_runtime.py`의 `--candidate`,
   `--evidence`, `--canonical-evidence`에 위 동일 바이너리의 증거를 전달하는
   기존 절차를 따른다. 소스나 바이너리가 바뀌면 이전 증거를 재사용하지 않는다.
4. SSH 묶음과 해당 프로필의 새 실행 정의를 준비한 뒤, 한 작업에서 프리셋 선택,
   다른 작업 유지, 연결 재수립, 양방향 하위 에이전트 호출을 확인한다.
   실제 계정으로 응답을 생성하는 마지막 단계는 사용량을 소비한다.
