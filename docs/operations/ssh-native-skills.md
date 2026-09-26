# Native Ubuntu skills and Windows plugin publication

Recorded 2026-09-25. This is the native execution setup for one managed SSH
host, written below as `<ssh-host>`.
It supplements [SSH host onboarding](ssh-host-onboarding.md); it does not move
account identity, task ownership, conversation history, or the Windows UI.

## Installed layout

| Component | Linux location / purpose |
|---|---|
| 3D source, virtual environments, Blender and TRELLIS runners | `<3d-project-root>` |
| Audio source, virtual environments and runners | `<audio-project-root>` |
| Model storage | `<model-store>`; project-specific model links point here |
| Temporary fixtures and downloads | `<scratch-dir>` |
| Skill registrations | `~/.agents/skills/{3d-assets,game-audio,game-vfx}` |
| Native project selection | `~/.config/codex-skill-runtimes/{3d-assets,game-audio}.json` |
| Managed profile configuration | `~/.local/share/codex-control-center/profiles/<profile-id>/codex` |
| Direct terminal Codex configuration | `~/.codex` |
| Published portable plugins | `~/.local/share/codex-control-center/shared-plugins/codex-manager-shared` |
| Official Linux document runtime | `~/.cache/codex-runtimes/codex-primary-runtime` |
| Read-only runtime discovery MCP | `~/.local/share/codex-control-center/tools/workspace_dependencies.py` |

Mounts were checked before installation. Follow the server's own storage
layout notes: a missing data mount must be repaired before restoring files. Windows virtual environments and executable binaries
are not Linux installations. The two source trees were transferred with their
current source changes; Windows credentials, virtual environments and runtime
binaries were excluded. Model files are reusable only after content checks.

The source skill definitions are in these project directories:

```text
<3d-project-root>/.agents/skills/3d-assets
<3d-project-root>/.agents/skills/game-vfx
<audio-project-root>/.agents/skills/game-audio
```

The global skill links point at those source directories. Both project wrappers
have a registered native root; `--execution auto` prefers the registered local
runtime. They do not need to route a normal Linux request back to Windows.
Use `--execution local` when the request must remain on this server.

## Re-registering the skills after a source restore

Install each project's documented Linux prerequisites first. Preserve existing
user skill directories: inspect a conflicting registration rather than replacing
it. Once the source paths above exist, create the three symlinks under
`~/.agents/skills`, then run:

```bash
python3 <3d-project-root>/.agents/skills/3d-assets/scripts/assetctl.py \
  runtime-configure --root <3d-project-root> --execution auto
python3 <audio-project-root>/.agents/skills/game-audio/scripts/audioctl.py \
  runtime-configure --root <audio-project-root> --execution auto
```

See each project's own install and native Ubuntu notes for environment, model
and provider details. These notes are also maintained in the corresponding Windows source
projects. VFX uses the 3D project's Blender helpers and the
target renderer's code; it is not a separate model service. Do not interpret
the presence of Tripo or another provider key as permission for paid generation.

## Portable plugins

Follow [SSH plugin publication](ssh-plugin-publication.md). The explicit
publisher adds the already installed portable Windows plugin packages to each
registered managed SSH home, preserving native versions and disable choices.
Seven portable packages were published in this setup: ElevenLabs, DevSpace,
GitHub, OpenAI Templates, Plugin Management, Sites and Tripo.

This is a one-time publication command, not a new background synchronization
service. Rerun it after a Windows plugin installation/update or after adding a
managed SSH profile. A running assistant turn can retain its previous tool list;
refresh the native skill/MCP configuration and start the next turn to use new
tools. No full workspace or remote job shutdown is required for publication.

Service grants remain attached to the selected ChatGPT account. The existing
auth proxy supplies its access to remote `codex_apps`; plugin file copies do not
copy OAuth grants. An API-only profile can discover portable plugin skills but
does not inherit another profile's ChatGPT connector authorization. The skill
CLIs' separately configured provider keys remain available through their native
local runners.

### Windows browser exception

The current desktop app only provisions its in-app browser tool connection for
the local Windows host. Its SSH synchronization explicitly includes visualizing
content, but not the browser's privileged UI/RPC connection. Copying the browser
package does not make it work on Ubuntu. Browser control remains in a Windows
task; adding authenticated remote browser delegation is separate development.
The published packages are not a claim of equivalent Windows-only UI behavior.

## Native document, PDF, spreadsheet and presentation runtime

The official Linux bundle supplies Node, Python, artifact-tool, LibreOffice and
Poppler. This setup uses bundle **26.923.10815**, Node **24.19.0**, Python
**3.12.14**, artifact-tool **2.8.76**. Its official archive is:

```text
https://persistent.oaistatic.com/codex-primary-runtime/26.923.10815/codex-primary-runtime-linux-x64-26.923.10815.tar.xz
SHA256: 3a3f77f4f40811f9221ed79da598a376f02fad123a28a1acd8b0a0a0b4498af1
Size: 383881884 bytes
```

Use `scripts/install_linux_workspace_runtime.py` on Linux with Python 3.12+
to install that pinned release. It verifies the archive before extraction and
does not overwrite an existing runtime. `--archive` allows an already downloaded
archive. Downloads use the official runtime installer's User-Agent.

This release contains Python symlinks referring to its original build machine.
The installer rewrites only the recognized build prefix into internal relative
links and applies the standard safe tar filter. Unknown absolute link targets
and escaping members are rejected. Do not bypass extraction safety globally.

After the runtime exists, register and install the five packages for the desired
`CODEX_HOME`. The tested stock CLI is version 0.155.1 (`plugin add`, not
`plugin install`):

```bash
codex plugin marketplace add \
  "$HOME/.cache/codex-runtimes/codex-primary-runtime/plugins/openai-primary-runtime" --json
codex plugin add documents@openai-primary-runtime --json
codex plugin add pdf@openai-primary-runtime --json
codex plugin add spreadsheets@openai-primary-runtime --json
codex plugin add presentations@openai-primary-runtime --json
codex plugin add template-creator@openai-primary-runtime --json
```

The managed native app-server also supports `plugin/install`. Its
`marketplacePath` must name the actual
`plugins/openai-primary-runtime/.agents/plugins/marketplace.json` file, not the
containing directory. Configuration backups must be private and concurrent
configuration changes must be serialized.

Copy `scripts/remote_helpers/workspace_dependencies.py` to the tools location
above with owner-only write access, then add a dedicated MCP section in the
desired homes. Expand the user's home to an **absolute** path in the TOML args:

```toml
[mcp_servers.workspace_dependencies]
command = "/usr/bin/python3"
args = ["/absolute/user/home/.local/share/codex-control-center/tools/workspace_dependencies.py"]
```

`load_workspace_dependencies` reports paths from the verified native bundle.
It cannot install software, execute commands, read credentials, select another
root from tool arguments, or access the network. It validates platform, path
containment, directory types and executable permissions. Use its returned paths
instead of the Windows desktop bundle paths. Reload MCP configuration through
the authenticated native connection; the lifecycle admin pipe is not a general
MCP API.

## Credentials

Needed provider secrets were transferred over SSH stdin directly into private
server files. Secret values were not passed as command-line arguments, committed
to Git, or printed. Project secret directories are mode `0700`; key files are
`0600`. Existing valid server Hugging Face credentials were checked and retained.
Windows-only sealed application login files are not portable API keys.

ElevenLabs and Tripo credentials belong in the projects' ignored `.secrets`
directories, according to their configuration readers. New credentials should
be provisioned through the same private channel. A copied API key can authenticate
the native skill runner, but does not constitute a service login for a Codex
plugin on another account. Direct terminal Codex also needs its own account
login for account-bound connectors.

## Verified scope

- All eight managed profiles: native SSH `skills/list` returned all three skills
  enabled; `plugin/installed` returned the five official document packages.
- All seven ChatGPT profiles: `codex_apps` returned their account-specific tools
  (162–257 tools at inspection). The API-only profile had no `codex_apps` account
  connection. All eight discovered the dependency MCP tool.
- The direct terminal home also installed the seven portable and five native
  document packages. Connector authentication there is separate.
- Native document smoke test: DOCX, PDF, XLSX and PPTX creation; artifact-tool
  import; LibreOffice DOCX-to-PDF conversion; Poppler PDF-to-PNG rendering.
  The server also has Noto CJK fonts available through fontconfig.
- Plugin publisher: 11 fixtures passed on Linux. Dependency loader: seven
  fixtures passed on Linux, including corrupt metadata and executable checks.
  Pinned runtime installer: nine Linux fixtures passed, plus read-only validation
  of all 29,305 official archive members and a no-change check of the live bundle.
- 3D/VFX and audio skills: the transferred model files passed SHA-256
  verification, and bounded offline CUDA generation completed through the
  registered skill wrappers, including a call from an unrelated working
  directory. A Linux virtualenv interpreter-link bug in the project runners was
  fixed so the child keeps the configured environment instead of the system
  Python. Per-project test counts and outputs stay in each project's own notes;
  successful execution does not approve any generated asset or sound quality.
- Provider validation used read-only authentication/catalog requests. No paid
  generation or production deployment was used for these checks.

The GPU skill execution results are recorded in each project's native Ubuntu
notes. Plugin discovery alone is not evidence that every plugin workflow has
been executed; no live site publication, repository mutation through a connector,
or paid media generation was part of installation validation.
