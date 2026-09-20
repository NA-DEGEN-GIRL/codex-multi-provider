# Native and Windows skill execution

Both supported skills now keep their native runtime registration separately from
the manager's SSH projection. The skill wrappers implement `runtime-configure`,
`runtime-status` and a leading `--execution local|windows|auto` option. Details and
OS-specific setup instructions live in each skill's `references/execution-setup.md`
and paired Korean guide, maintained in the corresponding skill repository.

Auto prefers an explicitly registered native runtime. Without registration, SSH
keeps its existing Windows bridge behavior. This selects the installed runtime;
the agent still checks doctor/plan to choose the host that supports the requested
operation. It pins that host for submission, polling, resume and later edits.
A failed or uncertain operation is never automatically retried on the other host.

Registration contains only runtime root/default execution and lives outside
CODEX_HOME, under the user's `.config/codex-skill-runtimes` (or XDG_CONFIG_HOME).
Thus profiles share the registration, and projection updates cannot replace
native environments, outputs or credentials. Native runtime source upgrades are
separate from instruction synchronization; preserve local data during upgrades.

The existing projection already distributes the wrappers and Markdown guides;
no manager service restart, native window operation or new SSH tunnel type is
needed for this change. Existing skill enable/disable controls remain in effect.

Validation: Windows skill tests and Linux routing tests passed. A CPU-only Linux
host ran real Blender 4.5.13 LTS procedural fixture/export/material edit and five
CPU preview renders, plus WAV import/edit/analysis/hash verification. Its bundled
Blender lacked OpenImageDenoiser; the runtime's pinned official portable Blender
resolved that without replacing the OS package. Both projected wrappers also
selected the registered native runtime in auto mode and the existing Windows
runtime when requested explicitly. These were setup/transport tests, not model
inference, paid API generation, listening review or final asset quality approval.
