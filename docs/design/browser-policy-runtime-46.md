# Browser policy lookup: correct the helper executable

The Browser plugin rejected navigation to the local 3D preview with
`enterprise_policy_unavailable`, not an explicit enterprise deny. Listing tabs
and creating an empty tab worked; origin access required configuration reads.

The native desktop's browser helper resolver reused `CODEX_CLI_PATH`, which is
the workspace RuntimeProxy for the desktop. It persisted that path into
`mcp_servers.node_repl.env.CODEX_CLI_PATH`. The actual runtime deliberately strips
manager launch metadata from its children, so the browser's secondary app-server
could not start this proxy. In the observed installation the missing Python
selection caused exit 2 (`Unknown option: -3`); even a valid Python fallback
would lack the required managed runtime/observer launch context.

Revision 15 of the private desktop changes only the browser helper path resolver
to prefer the already verified `CODEX_MANAGER_REAL_RUNTIME`. The desktop itself
continues to use its account-bound RuntimeProxy. The profile HOME, browser
permissions, administrator requirements, and security checks are unchanged.
Original desktop use without that manager variable retains its existing CLI.
An unknown or ambiguous native helper resolver prevents bundle publication.

Validation:
- Reproduced the same origin-policy failure through the supported Browser API.
- Reproduced the bad helper's startup failure in the sanitized environment.
- The same profile's real runtime returned successful `configRequirements/read`
  and `config/read` responses; the observed requirements were null.
- Evaluated the native desktop resolver after patching: managed helper chooses
  the real runtime, original app selection and Node helper selection are retained.
- 22 desktop bundle, original synchronization, and runtime proxy tests passed.
- Built manager fix 46 and prepared the actual installed desktop's private assets.

Running browser helper processes retain their previous executable selection.
The next normal workspace app start applies the new resolver and regenerates
native helper configuration. Navigation and visual quality in the user's 3D
task still require a check after that update; no successful visual inspection
of the blocked preview is claimed here.
