"""Private, hidden Electron fixture: no account, user window or external server.

Exercises the exact packaged Chromium with a loopback STUN responder. Evidence
contains only fixture socket types and ICE candidate types, never user traffic.
"""
import argparse
import json
import os
from pathlib import Path
import socket
import struct
import subprocess
import threading
import time
from uuid import uuid4

from desktop_launch import find_app
from manager_core import desktop_bundle

ROOT = Path(__file__).resolve().parents[1]


def run(policy):
    installed = Path(desktop_bundle.prepare(ROOT, find_app())['executable']).parent
    desktop_bundle.check_archive_support(installed)
    run_dir = ROOT / 'artifacts/results' / ('desktop-network-' + uuid4().hex[:8])
    program = run_dir / 'app'
    for directory, _, names in os.walk(installed):
        relative = Path(directory).relative_to(installed)
        target = program / relative
        target.mkdir(parents=True, exist_ok=True)
        for name in names:
            if relative == Path('resources') and name == 'app.asar':
                continue
            os.link(desktop_bundle._long_path(Path(directory) / name), desktop_bundle._long_path(target / name))
    udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    udp.bind(('127.0.0.1', 0))
    udp.settimeout(.25)
    stopped = threading.Event()

    def stun():
        while not stopped.is_set():
            try:
                packet, peer = udp.recvfrom(2048)
                if len(packet) < 20 or packet[:2] != b'\x00\x01':
                    continue
                cookie = 0x2112A442
                address = struct.unpack('!I', socket.inet_aton('198.51.100.1'))[0] ^ cookie
                attribute = struct.pack('!HHBBHI', 0x20, 8, 0, 1, peer[1] ^ (cookie >> 16), address)
                udp.sendto(struct.pack('!HH', 0x101, len(attribute)) + packet[4:20] + attribute, peer)
            except socket.timeout:
                pass

    server = threading.Thread(target=stun, daemon=True)
    server.start()
    helper = (ROOT / 'scripts/manager_core/desktop_network_policy.cjs').read_text(encoding='utf-8')
    main = helper + r'''
const {app,BrowserWindow}=require('electron'),fs=require('node:fs'),path=require('node:path');
const root=process.env.CODEX_NETWORK_FIXTURE;let win;
const policy=process.env.CODEX_NETWORK_POLICY;
if(policy!=='production'){
 app.commandLine.appendSwitch('force-webrtc-ip-handling-policy',policy);
 app.commandLine.appendSwitch('webrtc-ip-handling-policy',policy);
}
setTimeout(()=>app.exit(2),22000).unref();
app.whenReady().then(async()=>{
 try {
  win=new BrowserWindow({show:false,width:400,height:300,webPreferences:{backgroundThrottling:false}});
  if(policy!=='production') win.webContents.setWebRTCIPHandlingPolicy?.(policy);
  await win.loadURL('data:text/html,<title>Isolated network fixture</title>');
  const port=Number(process.env.CODEX_NETWORK_STUN_PORT);
  const result=await win.webContents.executeJavaScript(`new Promise(async(resolve,reject)=>{
   const pc=window.fixturePeer=new RTCPeerConnection({iceServers:[{urls:'stun:127.0.0.1:${port}'}]});
   const types=[];pc.onicecandidate=e=>{if(e.candidate)types.push(e.candidate.type)};
   pc.createDataChannel('fixture');await pc.setLocalDescription(await pc.createOffer());
   setTimeout(()=>resolve({types,state:pc.iceGatheringState}),3500);
  })`);
  fs.writeFileSync(path.join(root,'ready.json'),JSON.stringify({policy:win.webContents.getWebRTCIPHandlingPolicy?.() ||
    app.commandLine.getSwitchValue('force-webrtc-ip-handling-policy'),...result,pids:app.getAppMetrics?.().map(x=>x.pid) || [process.pid]}));
  const timer=setInterval(()=>{if(fs.existsSync(path.join(root,'done')))app.quit()},100);timer.unref();
 }catch(error){fs.writeFileSync(path.join(root,'ready.json'),JSON.stringify({error:String(error)}));app.exit(1)}
});
'''
    chunks = [('package.json', b'{"name":"codex-network-fixture","main":"main.cjs"}'), ('main.cjs', main.encode())]
    tree = {'files': {}}
    offset = 0
    for name, data in chunks:
        tree['files'][name] = {'offset': str(offset), 'size': len(data)}
        offset += len(data)
    header = json.dumps(tree).encode()
    payload = struct.pack('<I', len(header)) + header + b'\0' * (-len(header) % 4)
    (program / 'resources/app.asar').write_bytes(struct.pack('<III', 4, len(payload) + 4, len(payload)) + payload + b''.join(data for _, data in chunks))
    env = {k: v for k, v in os.environ.items() if k.upper() in {
        'SYSTEMROOT', 'WINDIR', 'PATH', 'TEMP', 'TMP', 'USERPROFILE', 'LOCALAPPDATA', 'APPDATA'}}
    env.update(CODEX_NETWORK_FIXTURE=str(run_dir), CODEX_NETWORK_POLICY=policy,
        CODEX_NETWORK_STUN_PORT=str(udp.getsockname()[1]))
    startup = subprocess.STARTUPINFO()
    startup.dwFlags = subprocess.STARTF_USESHOWWINDOW
    startup.wShowWindow = subprocess.SW_HIDE
    print(run_dir, flush=True)
    try:
        with (run_dir / 'desktop.log').open('w', encoding='utf-8') as log:
            child = subprocess.Popen([str(program / 'ChatGPT.exe'), '--user-data-dir=' + str(run_dir / 'ui')],
                env=env, startupinfo=startup, creationflags=subprocess.CREATE_NO_WINDOW, stdout=log, stderr=log)
            try:
                deadline = time.monotonic() + 18
                while not (run_dir / 'ready.json').exists() and time.monotonic() < deadline:
                    time.sleep(.1)
                report = json.loads((run_dir / 'ready.json').read_text(encoding='utf-8'))
                assert 'error' not in report, report
                # Interpolate only verified integer PIDs from this disposable app.
                pids = ','.join(str(int(pid)) for pid in report['pids'])
                command = f'$fixtureIds = @({pids}); $children = Get-CimInstance Win32_Process | Where-Object {{ $_.ParentProcessId -in $fixtureIds }}; $fixtureIds += $children.ProcessId; Get-NetUDPEndpoint -ErrorAction SilentlyContinue | Where-Object {{ $_.OwningProcess -in $fixtureIds }} | Select-Object LocalAddress,LocalPort,OwningProcess | ConvertTo-Json -Compress'
                sockets = subprocess.run(['powershell', '-NoProfile', '-Command', command], capture_output=True,
                    text=True, timeout=8, creationflags=subprocess.CREATE_NO_WINDOW, check=True)
                report['udp'] = json.loads(sockets.stdout or '[]')
                if isinstance(report['udp'], dict): report['udp'] = [report['udp']]
                report['mdns_listeners'] = sum(item['LocalPort'] == 5353 for item in report['udp'])
                (run_dir / 'done').touch()
                child.wait(timeout=5)
                (run_dir / 'report.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
                print(json.dumps(report), flush=True)
                assert report['mdns_listeners'] == 0, 'WebRTC opened a local multicast listener.'
                assert 'srflx' in report['types'], 'Public-route UDP/STUN did not remain available.'
            finally:
                if child.poll() is None:
                    child.terminate()  # Only the process handle created above.
                    child.wait(timeout=5)
    finally:
        stopped.set()
        server.join(timeout=1)
        udp.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--policy', default='production', choices=['production', 'default_public_interface_only'])
    run(parser.parse_args().policy)
