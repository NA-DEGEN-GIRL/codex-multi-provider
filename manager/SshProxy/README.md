# Native SSH bootstrap

This Windows console executable is published as `ssh.exe` into a manager-owned,
process-scoped PATH directory. It is not installed into the system PATH and does
not replace Windows OpenSSH. The Python adapter owns command classification and
the absolute real OpenSSH path; this bootstrap only starts the adapter and owns
its transport process tree.

Required configuration:

- `CODEX_MANAGER_SSH_PYTHON`: absolute Python interpreter `.exe`; falls back to
  `CODEX_MANAGER_PYTHON` only when unset or blank. No PATH search or shell launcher.
- `CODEX_MANAGER_SSH_SCRIPT`: absolute adapter `.py` path.
- `CODEX_MANAGER_SSH_BINDINGS`: inherited unchanged for the adapter to interpret.

Python receives `-u <script> -- <original SSH arguments>`. Windows argv quoting
preserves spaces, quotes, backslashes and Unicode. The bootstrap does not parse
SSH options or log arguments, paths, credentials, or environment values.

Only duplicated stdin/stdout/stderr handles are inherited. On Windows 10+, the
child starts suspended and joins an unnamed non-inheritable Job Object atomically
through `PROC_THREAD_ATTRIBUTE_JOB_LIST`. The job uses
`JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`; the child resumes only after creation succeeds.
There is no interval between child creation and ownership during which killing
the bootstrap could orphan a suspended process holding protocol pipe handles.
Killing this `ssh.exe` therefore kills the Python adapter and real SSH descendants.
The raw inherited handles preserve binary protocol bytes and stdin half-close.
Closing a local transport does not mean the detached remote app-server stopped.

Build and run the headless fixture suite from the repository root:

```powershell
dotnet build manager/SshProxy/Codex.ControlCenter.SshProxy.csproj -c Release
python manager/SshProxy/tests/test_bootstrap.py
```

The fixtures launch only test Python processes. They cover argv, large binary
streams, late output after stdin EOF, exit status, configuration errors and
forced termination of three descendant generations. They do not establish
original Codex GUI routing, SSH authentication, or remote runtime compatibility.
