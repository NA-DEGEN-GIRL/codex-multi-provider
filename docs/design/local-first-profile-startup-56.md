# Local windows and background SSH preparation

Profile selection previously entered the complete local-and-remote restart
workflow whenever saved SSH settings were outdated. Remote inspection,
preparation and listener startup therefore ran before a local window appeared.
An unavailable host or a failed remote launch prevented local work altogether.

The local-first path opens the account window using local preparation only.
Its durable SSH-only maintenance record fences new SSH commands while a
background worker inspects the previous listeners, preserves busy work,
prepares the intended configuration and publishes verified bindings. Local
window selection and local work remain available when this worker fails.
Retrying SSH does not close or relaunch that local window.

An interrupted, profile-scoped restart can be adopted only after its previous
local process identities are proven dead. Global updates and unresolved local
shutdowns keep their original admission requirements. Generation and policy
checks prevent late network results from updating another profile execution.
Remote start remains strict about differing live revisions.

SSH children wait before routing, then reload the published binding manifest.
Enrollment checks the SSH gate again to cover a concurrent maintenance request.
The wait has a deadline and cancels on removal or generation changes. Failed
preparation produces a connection error without holding up the local window.

At manager startup, valid signed-in profiles are opened serially in the
background. A selected queued profile moves to the front of the remaining
queue. Already running profiles are reused without an Electron second-instance
launch. The coordinator runs once, skips login-required accounts, cancels pending
launches on shutdown and reports per-profile preparation state. It does not
reopen accounts repeatedly after the user closes them.

Closing the manager blocks all new local launches, cancels the warmup queue,
waits for admitted launches to finish, and then reads a fresh process inventory before shutting down
preloaded windows that were never selected. Process cleanup uses the backend's
authenticated broker and a pinned profile generation. Service retirement also
checks whether the warmup worker or a local launch is still active. A disconnected
client reconnects for shutdown without running startup hooks; failure retains
the manager instead of abandoning hidden windows.

Windows hidden startup alone does not suppress Electron's later `show` and
`focus` calls. A manager-scoped startup flag suppresses those calls until a valid
window lease hands display control to the manager. Renderer loading continues.

The reported remote configuration failure came from orphaned generated agent
roles whose ownership manifest no longer listed them. Recovery accepts only
exact file hashes from validated historical definitions, archives those bytes,
and preserves unknown user edits. Early child exit reports a bounded error code
instead of waiting for the entire listener startup deadline.

Validation covered 964 manager Python tests (one environment-dependent skip),
32 control-center/remote-launcher tests, 15 Rust service tests, the desktop host
Node fixtures, and the packaged Windows self-tests. These checks use isolated
profiles and native fixture windows. Live accounts and remote listeners were
not restarted for validation; live reconnection remains a post-reopen check.
