// Private desktop copies do not need Chromecast/DIAL discovery. Leaving the
// Chromium media router enabled opens multicast listeners and triggers a new
// Windows firewall prompt for every immutable executable path after an update.
// WebRTC also creates mDNS listeners for private-interface ICE candidates.
// Use the public route (STUN/TURN/UDP remain available), without local multicast
// discovery. LAN-only peer candidates are not advertised. No firewall rules or
// installed-app files are changed. Owl uses Chromium switches; it does not
// implement Electron's setWebRTCIPHandlingPolicy API.
(() => {
  if (process.platform !== 'win32') return;
  const { app } = require('electron');
  const flags = new Set((app.commandLine.getSwitchValue('disable-features') || '').split(',').filter(Boolean));
  flags.add('MediaRouter');
  flags.add('DialMediaRouteProvider');
  app.commandLine.appendSwitch('disable-features', [...flags].join(','));
  const force = 'force-webrtc-ip-handling-policy', normal = 'webrtc-ip-handling-policy';
  const configured = app.commandLine.getSwitchValue(force) || app.commandLine.getSwitchValue(normal);
  const policy = configured || 'default_public_interface_only';
  // Preserve an explicit, possibly stricter policy supplied by the caller.
  if (!app.commandLine.getSwitchValue(force)) app.commandLine.appendSwitch(force, policy);
  if (!app.commandLine.getSwitchValue(normal)) app.commandLine.appendSwitch(normal, policy);
})();
