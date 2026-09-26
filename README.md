# Codex 작업 공간

여러 Codex 계정과 외부 API 프로필에서 작업 기록·프로젝트·개인 스킬을 함께 사용하는 Windows 관리 앱입니다. 로그인과 공급자 키는 프로필별로 분리합니다. 하위 에이전트에 외부 모델을 허용하는 설정과, 주 대화를 외부 모델로 실행하는 프로필은 별개입니다.

## 실행

빌드된 환경에서는 저장소 루트의 다음 파일을 사용합니다.

| 파일 | 용도 |
|---|---|
| `Open-Control-Center.cmd` | 작업 공간 앱 실행 |
| `Open-Control-Center-Admin.cmd` | 관리자 권한으로 실행 요청 (수정 85 이상) |
| `Open-Feedback-Test.cmd` | 시험판 작업 공간 앱 실행 |
| `Open-Synced-Codex.cmd` | 공통 기록을 사용하는 본앱 실행 |
| `Stop-All-Codex.cmd` | 남아 있는 Codex 관련 프로세스 종료. 진행 중인 작업도 중단됩니다. |

프로필 `＋`에서 Codex 로그인 또는 외부 API 프로필을 추가합니다. 프로필을 우클릭하면 모델 기본값·하위 에이전트·로그인 설정을 변경할 수 있습니다. API 키는 각 사용자가 등록해야 합니다.

공통 저장소의 최초 통합은 기존 Codex 작업을 마치고 앱을 정상 종료한 상태에서 진행합니다. 이후에는 저장된 기록을 공유하며 실행 중 화면 갱신은 별도의 알림 경로를 사용합니다. 저장소 공유만으로 실시간 화면 갱신이 보장되는 것은 아닙니다. 실제 화면·입력·SSH 동작의 검증 범위는 각 수정 문서를 확인하세요.

## 개발

Windows x64, Python 3, .NET 10 SDK와 Rust 빌드 환경이 필요합니다. 실행 파일·로그·계정 설정·API 키·대화 기록은 이 저장소에 포함하지 않습니다.

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\build-manager.ps1 -SelfTest
```

Rust 변경 사항은 공식 기반 커밋에 적용할 수 있는 누적 패치와 트리 확인값으로 제공합니다. [복원 안내](patches/README.md)를 먼저 확인하세요. 기존 `runtime/` 작업 폴더에 패치를 다시 적용하지 마세요.

- [관리 앱 구조와 사용법](manager/README.md)
- [개발 인수인계와 다음 작업](docs/HANDOFF.md)
- [복원 합격 기준](docs/RESTORE-CHECKLIST.md)
- [Ubuntu에서 3D·오디오·VFX 스킬과 플러그인 사용](docs/operations/ssh-native-skills.md)
- [Windows 플러그인을 SSH 프로필에 게시](docs/operations/ssh-plugin-publication.md)
- [설계 및 수정 기록](docs/design/)
- [설정 팝업과 프로필 설정 수정](docs/design/settings-backdrop-52.md)
- [개인정보 검사 범위](docs/privacy-review.md)
- [초기 다중 모델 실험실 기술 문서](docs/technical-reference.md)

`Open-Lab.cmd`와 `Open-Experimental-Codex.cmd`는 초기 실험실 실행기입니다. 날짜가 붙은 초기 검증 기록은 당시 결과이며, 현재 앱 전체의 검증 완료를 의미하지 않습니다. 공개 문서의 계정·호스트·작업 식별자는 예시로 치환되어 있습니다.
