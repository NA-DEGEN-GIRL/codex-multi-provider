# Profile sidebar concepts

Generated with the built-in imagegen tool on 2026-09-20. These are synthetic
design references, not screenshots of account balances or runtime state.
Concept A informs the implementation: stronger names, separated state and quota,
thin usage meters, explicit redemption counts, and compact section actions.
Generated labels or balances are illustrative; the shipped UI uses actual data
and never shows Codex quota or redemption counts for an external API profile.

## Implemented sidebar

`implemented.png` is rendered from the actual WPF release using synthetic test
accounts. DeepSeek proposed the card hierarchy and implemented the visual
template; GPT integrated the layout, reviewed the code and validated rendering.

- 304 DIP sidebar, 15 DIP semibold profile names, separate runtime status.
- Weekly remaining quota (or the available window), slim meter, whole-value
  reset timestamps, and explicit zero/positive/unknown redemption counts.
- External API profiles show an API badge and model, without Codex quota.
- Compact section actions, preserved drag/context menus, and single-line
  shortcut titles with secondary account information.
- Actual WPF layout validated at 1440×960 and 1024×840, including live fixture
  refreshes, selection preservation, and long-name ellipsis.

## Concept A prompt

Use case: ui-mockup. Create a high fidelity flat desktop UI design reference, concept A "Structured cards", for a Korean Codex multi-account workspace left sidebar. Portrait image about 900x1500. The sidebar is a calm charcoal Windows productivity app, no device frame, no perspective. Show a polished 304-DIP-wide sidebar scaled large to fill image, generous but efficient spacing. Header "Codex 작업 공간" and small subtitle "프로필과 작업을 한곳에". Section row "프로필  8" with tiny + and refresh and ellipsis buttons right aligned. Cards 01, 02 selected, 03, 04, 05, 06, 07 and "DeepSeek" with API chip. Profile names prominent semibold 17pt. Clear hierarchy: first row name left, small green dot + "실행 중" right; second row muted "주간" left and bold remaining percentage right e.g. "65%"; slim 3px horizontal usage meter; bottom row tiny calendar icon "9/26 20:37 초기화" and separate redemption label "리딤 2회". Make separate lines if needed, absolutely no word breaks or mid-word Korean wraps. 02 selected with subtle blue-violet tinted background and narrow accent left border. Small optional badge "하위 에이전트 · 혼합" under 02, other card "외부 전용". Zero quota uses muted warm amber. Unknown redemption "리딤 확인 중"; external API card instead shows "deepseek-flash" and no fake usage. Drag grip understated at left. Beneath accounts: "작업 바로가기" with compact plus/ellipsis controls and one short sample task. Bottom fixed navigation "전체 기록", "설정 및 관리" and tiny clickable "수정 62 · 버전 복사". Modern crisp typography, neutral background #1c1f26, brighter names #eef2fa, metadata #9ba7ba, restrained periwinkle accent #91a7ff. All text aligned, practical WPF app, minimal decoration, high readability.

## Concept B prompt

Use case: ui-mockup. Create a high fidelity flat desktop UI design reference, concept B "Compact account list", for Korean Codex workspace left sidebar. Portrait about 900x1500, no device frame, no perspective. More compact editorial list design, graphite black #191c22, fine separators, mint accent and pale blue selection. Title "Codex 작업 공간", compact subtitle. Header row "프로필" and + refresh ellipsis icons. Profiles 01 through07 plus one external API profile DeepSeek. Each row large distinctive rounded-square account monogram/number on left, name semibold and small status "실행 중" on right. Below each name, align "주간 65% 남음" and a short tiny usage meter on same row. Next row "초기화 9/26 20:37". Redemption gets visually distinct small outlined pill "리딤 2회" next to reset line or below, no wrapping broken words. Profile02 selected with subtle slate-blue rounded row and clear border. Include small badges "하위 에이전트 · 혼합" and "외부 전용" where relevant. External API row badge "API", model "deepseek-flash", no misleading quota. Grey out zero remaining 03 and04 but retain readable text. Low visual clutter, names clearly separated from small metadata, white space consistent, all rows same horizontal alignment. Drag handles subtle. Beneath profiles a "작업 바로가기" row + and one short task. Fixed bottom "전체 기록", "설정 및 관리", understated version "수정 62". Desktop productive sober premium design, no charts except tiny usage bars, no gradients or glowing decoration.
