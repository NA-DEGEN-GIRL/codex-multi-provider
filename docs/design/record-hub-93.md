# 수정 93: 계정 간 기록 동기화 허브와 시작 부하 개선

## 측정한 증상

- 수정 92 첫 실행에서 01 한 곳이 4분 동안 `thread/read` 1,626건을 보냈다. 기록 동기화
  수정 뒤에도 602건(main 335, 화면 267)이었고 8개 프로필이 같은 일을 동시에 했다.
- 변경 한 건마다 다른 프로필이 main에서 최대 3번 읽고, 성공할 때마다 native catalog가 한
  번 더 읽고, 화면이 다시 읽었다. 스트리밍 중에는 0.7-2 s마다 같은 작업을 다시 읽었다.
- SSH 변경은 8개 프로필이 서로 다시 게시했다(파일 8개에 같은 작업 228개). 프로필마다
  25분 동안 파일 확인이 약 2만 4천 번이었다.
- 시작 샘플러(`startup_profile.py`)에서 가장 큰 항목은 상태 저장소 잠금 대기였다.
  `personal_skills.reconcile`이 2 s마다 저장소 잠금을 쥔 채 규칙×스킬×프로필마다
  `Path.resolve()`를 반복했고, 그동안 프로필 시작(03)·state·원격 목록이 기다렸다.

## 변경

### 시작 부하

- `personal_skills._key`: 잠금을 쥔 한 번의 동기화 안에서 같은 경로는 한 번만 확인한다.
  재현(스킬 60개·프로필 8개): 경로 확인 252,315회 → 1,648회, 46 s → 1.4 s.
- `login_probe.purge_in_child`: 오래된 probe home 삭제를 별도 Python 프로세스에서 한다.
  자식은 background 모드로 CPU·I/O 우선순위를 낮추고, 관리 환경 변수를 받지 않으며,
  stdin이 닫히면(backend 종료) 즉시 끝난다.
- `startup_profile.py`: backend 시작 후 3분 동안 스레드별 실제 CPU 시간(GetThreadTimes)으로
  코드 위치를 집계해 `logs/backend-startup-profile.json`에 남긴다. 사용자 데이터는 쓰지 않고
  `CODEX_MANAGER_STARTUP_PROFILE=0`으로 끈다.

### 1단계: 주입 스크립트의 중복 제거 (`desktop_record_sync.cjs`, `desktop_renderer_record_sync.cjs`)

- 변경은 1.5 s 조용해진 뒤 한 번 읽고, 계속 바뀌는 작업은 최대 5 s마다 읽는다.
  읽은 요약을 검증된 native 형태(26.915/26.917)일 때 `observeThread`로 바로 넘겨 catalog의
  두 번째 읽기를 없앤다. 그 밖에는 native 경로를 쓴다.
- 화면은 열린 작업만 읽는다(보이는 창에서 2 s에 한 번). 열리지 않은 작업은 dirty로 두고
  열 때 읽는다.
- 자기 SSH `turn/*`·`item/*` 변경의 되돌아온 사본은 3 s 동안 건너뛴다. local에는 적용하지
  않는다. 실시간 not-found는 3 s 뒤 한 번 다시 읽는다. 원본 앱 writer는 항상 신뢰하고,
  현재 프로필이 아닌 오래된 writer는 무시한다.

### 2단계: Rust 서비스 허브 (`manager/service/src/records.rs`)

- 서비스가 `record-signals/*.desktop.json`·`*.json`을 250 ms(한가할 때 1 s)마다 확인하고
  `(host, id)`별로 0.75 s 조용해질 때까지(최대 3 s) 모아 하나의 사건으로 만든다.
- 삭제는 최종이고 보관·삭제만 "이미 아는" 보고자를 그 보고자로 바꾼다. 일반 변경·보관
  해제는 모든 보고를 보낸 프로필만 제외한다. 이 규칙으로 다른 프로필의 강한 변경이
  숨겨지지 않는다.
- 사건은 epoch+순서 번호로 `record-journal.json`에 저장된 뒤에만 전달한다(작업당 최신 1개,
  tombstone 30일, 최대 4,096개). 일반 변경만 남았으면 최대 3 s, 삭제·보관·해제는 1 s 안에
  저장한다. 쓰기 실패 중 저장된 journal은 다음 시작에서 새 epoch로 바꾼다.
- `records.poll`(long poll)과 `records.publish`는 broker token 없이 허용하되 내용은 ID·host·
  종류뿐이다. 기록 연결은 별도 pool(48, 첫 요청 전 64)로 세고 `clients`·유휴 종료·완전
  종료 판단에 넣지 않는다. 연결은 처음 밝힌 프로필로 고정되고, publish는 연결별·전체 한도를
  넘거나 대기열이 가득 차면 `records_busy`로 거절한다. tick 패닉이 3번 이어지면 허브가
  닫혀 앱이 파일 방식으로 돌아간다.

### 3단계: 앱의 허브 구독·직접 게시

- `instances.py`는 `CODEX_MANAGER_SERVICE_PIPE`가 관리 pipe 형식일 때만
  `CODEX_MANAGER_RECORDS_PIPE`로 넘긴다(broker token은 넘기지 않음). 원본 동기화 앱은 파일
  방식 그대로다.
- 허브가 정상이면 앱은 다른 프로필 파일을 훑지 않고 20 s long poll로 받는다. 자기 파일은
  계속 쓰고(허브·이전 버전 호환), 같은 항목을 `records.publish`로도 보낸다.
- `{epoch, cursor}`를 `record-signals/status/<profile>.hub.json`에 저장해 다시 켤 때 밀린
  기록을 재생하지 않는다. cursor는 처리하지 않은 사건을 넘지 않는다. reset이면 실패 없이
  끝날 때까지 파일 catch-up을 한다. 대기열이 가득 차면 버리지 않고 파일 읽기를 멈췄다가
  이어서 읽는다.
- 연결 실패·`unknown_command`·`closing`·시간 초과면 즉시 파일 방식으로 돌아가고 30 s부터
  최대 5분 간격으로 다시 연결한다.

## 유지한 규칙

- 신호 파일 형식은 바꾸지 않았다. 이전 어댑터·원본 앱과 섞여도 동작한다.
- 허브가 없거나 멈추면 1단계 파일 방식으로 동작한다. 허브는 변경을 놓치면 반드시
  `reset`을 보낸다.
- 기록 연결은 관리창·backend 연결 수, 완전 종료, 유휴 종료를 막지 않는다.

## 검증

- Rust 서비스 61개(8회 연속), node 24개, Python 전체 1,512개(실패 0, 기존 SshProxy 빌드 없음
  오류 1). 세 번의 반박 검토에서 나온 누락 경로(원본 보관 해제 손실, 같은 스레드 교체,
  동시 보고자 억제, 1회 catch-up, 대기열 제거, 배열 교체 경쟁)는 재현 코드를 회귀 시험으로
  넣었다.
- 26.917 archive에 전체 패치를 적용하고 `node --check`를 통과했다.
- 실제 앱에서는 아직 확인하지 않았다. 새 서비스는 완전 종료 후 다시 열 때 적용된다.

## 적용 후 확인

- `supervisor.status`의 `records`(health ok, record_clients 8)와 `record-signals/status/*.hub.json`.
- 01 데스크톱 로그의 시작 후 `thread/read` 수(수정 92: 602)와 not-found 수.
- `logs/backend-startup-profile.json`의 상위 항목, 프로필이 모두 시작되는 시간.

## 남은 문제

- 일반 변경은 journal 저장 간격 때문에 다른 프로필에 최대 3-6 s 늦게 보일 수 있다.
- reset 뒤 파일 catch-up이 1,024건을 넘으면 대기열이 찰 때까지 나눠 읽는다.
- 런타임 프록시(Python)는 직접 게시하지 않는다. 허브가 그 파일을 250 ms마다 읽는다.
