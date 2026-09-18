# Viewport, record refresh and API profiles - revision 34

User scope: keep Settings/background views rendered, decouple SSH from llm-usage,
refresh Open-Synced-Codex without reopening tasks, distinguish subagent providers
from an explicit primary external API profile sharing the canonical task store.

## Window lifecycle

The native top-level HWND remains unowned and on its independent input queue
(revision 33 inline Korean IME design). The selected viewport stays visible when
another application takes focus. It is demoted below the foreground application
and kept immediately above the manager. Unselected profiles, minimized manager
and manager modal dialogs still hide the viewport. No focus/navigation changes.
Native location events trigger bounded repair. A settled size resets the recovery
budget, so later Settings/layout changes get a fresh repair episode. The desktop
bridge also checks native cached bounds; it does not repeatedly show or activate.

## Shared records

Unpersisted thread IDs previously entered the desktop import coordinator's
unbounded retry queue. A hydration failure also deferred every other pending
thread. Import only after successful hydration and isolate retries per task.
Keep active local streams/drafts intact, prioritize visible tasks, and invalidate
the background metadata lookup cache before reading externally updated history.
Signals contain IDs and sequence numbers, never message text or credentials.

On resume, the desktop explicitly selects this profile's configured provider.
When the stored task uses a different provider, use this profile's default model
and effort. This is required in the original companion too: otherwise a task
last run by an API profile references a provider absent from the original config.
Same-provider task model preferences are retained.

## Profile kinds

- Codex account: existing ChatGPT login, GPT primary, optional external subagents.
- External API: provider/model selected during profile creation, encrypted API key,
  isolated config/user data, no borrowed OAuth login. Shown with `[API]` in the
  list and an explicit API profile/model label in the header.

Both use the existing canonical record store and native task IDs. API execution
is explicit: resuming/starting a task in that profile routes the selected main
model and effort; task context is sent to that provider when the user submits.
The registry and API key storage are shared with the clearly labeled subagent
settings. Primary selection does not enable subagents. Responses-compatible
providers are supported; unsupported protocol registrations remain unavailable.
Remote generation and the SSH message adapter carry the same primary selection.

## SSH

Normal SSH prepare and profile rename no longer invoke remote llm-usage account
alias sync. Remote runtime login uses the selected Codex profile's existing auth
bridge, or the API profile's configured provider credentials. Legacy catalog
imports may read optional llm-usage metadata but are not a connection requirement.

Captured 26.911.7940 native start/proxy commands are tested. A new package hash
alone no longer blocks unchanged SSH: unknown bundles must pass full native
wrapper/marker/body decoding for every command. Unknown layouts/payloads never
fall through to unscoped remote execution. Configuration-only SSH queries remain
read-only passthrough. Old manifests remain conservative until next launch.

## Verification

- 159 focused unit/integration checks: 158 passed, one pre-existing platform skip.
- Native HWND self-test: repeated five repair episodes, background stacking,
  modal/minimized lifecycle, orphan recovery, no parent/input attachment.
- Actual desktop + a second runtime, disposable common store and loopback model:
  warm resumed history receives two updates despite an unavailable ID; the original
  resumes external-provider history using its own provider. No model API charges.
- Generated primary-provider configuration: shared ID and history, API-only
  credentials, original app reads API writes and can continue with its own model.
- SSH read-only inspection: remote-linux, hp, remote-dev reachable/preparable.
  Live user profile reconnects were not forced during ongoing work.

The offscreen desktop fixture confirms HWND geometry, background visibility and
renderer handshakes. Its occluded screenshot does not verify visual Settings
rendering or physical IME input; those live interactions remain to be checked.

Existing processes retain loaded code. Revision 34 applies when each application
next opens; Open-Synced-Codex must be reopened once to load the new desktop adapter.
