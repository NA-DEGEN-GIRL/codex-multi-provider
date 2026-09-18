# Profile presentation recovery and local discovery (39)

## Observed incident

The fix38 trace `shell-20260917-232907-89588.performance.jsonl` shows cached
profile selection completing in 0–31 ms and native HWND probes responding in
1–3 ms. At 23:32:20 the selected 02 renderer's health report was 53 seconds old;
at 23:32:22 it was fresh again. The old health helper deliberately skips hidden
leases, so this does not by itself prove a blocked JavaScript thread. There is
no recorded long dispatcher stall during that episode. Root processes were
reused, not repeatedly launched. These samples do not establish a memory leak.

## Reproduced failure and correction

NativeWindowLease cached visibility and bounds before publishing its atomic
file replacement. If any reader temporarily denied FILE_SHARE_DELETE, that
replacement failed. The same visibility/bounds were then considered already
applied forever. A resize changed the cache and incidentally republished the
lost visible state. The error was caught without a diagnostic at the host.

The lease now stays dirty until publication succeeds. A normal layout retry
retries the same desired state; it does not need resizing or restarting.
Failures and recovery are reported once per transition. Renderer acknowledgments
identify the presentation epoch, so an earlier successful show is not accepted
for a new selection. Cross-profile health/acknowledgment file changes no longer
invoke every process's native layout handler.

The original incident did not log the actual file exception, so attribution of
every reported freeze to this failure is not proven. New performance samples
include the published visibility/epoch and native visible/enabled flags.
Diagnostic readers allow atomic replacement instead of competing with writers.

## Repeated Windows network permission

The current 03 Chromium NetworkService had an mDNS UDP 5353 listener despite
MediaRouter/DIAL already being disabled. WebRTC private-interface discovery is
another source of multicast listeners. The private desktop copies now use
`default_public_interface_only` via Chromium's switches. Existing explicit
policies are preserved. Public-route UDP/STUN/TURN remain available; private
LAN ICE candidates are no longer advertised. No Windows firewall policy, login
state, or installed application is modified.

This build's Owl compatibility API does not implement Electron's
`setWebRTCIPHandlingPolicy`, so the supported Chromium command-line path is
used and verified against the actual packaged binary. References:
[Chromium switch introduction](https://chromium.googlesource.com/chromium/src/+/3d6bf17ccb45f22a9f86ce2dfc12559ea39dac74),
[policy semantics](https://www.electronjs.org/docs/latest/api/web-contents#contentssetwebrtciphandlingpolicypolicy).

## Evidence

- `artifacts/results/lease-retry-before/report.json`: old implementation fails
  the real Windows sharing-violation/reselect case.
- `artifacts/results/lease-retry-after/report.json`: unchanged visibility and
  geometry recover after releasing the same reader.
- `artifacts/results/desktop-render-87dd7c07/wpf-host.json`: real Codex renderer,
  empty home and off-screen production WPF host. Two lost show commands recover
  at unchanged size, with current-epoch acknowledgments and populated captures.
  Cold hidden start, geometry drift, hide/show, minimize/restore and capture
  controls also pass. No live user window was selected or focused.
- `artifacts/results/desktop-network-0dabb03d/report.json`: actual packaged
  Chromium with the production network helper, isolated empty profile, loopback
  STUN responder. Zero UDP 5353 listeners, successful `srflx` candidate over
  UDP. This is not an end-to-end voice call or a claim that every possible
  Windows security prompt has been eliminated.
- Node host/network policy tests pass; 19 desktop archive, original-sync and
  frozen-release tests pass. All build-manager self-tests pass, including real
  sharing-violation retry and stale-ack rejection.
- Published release: `artifacts/manager/releases/20260917-145310-440`.
  Private desktop: `26.911.7940.0-1cca4b1b3984b440`.

The release is selected by the existing launcher. Running user apps retain
their current code until normal close/reopen; tests do not terminate them.
