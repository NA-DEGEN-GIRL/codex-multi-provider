const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const source=fs.readFileSync(require('node:path').join(__dirname,'../scripts/manager_core/desktop_network_policy.cjs'),'utf8');
function apply(switches){
 const context={process:{platform:'win32'},require:name=>{assert.equal(name,'electron');return {app:{commandLine:{
  getSwitchValue:key=>switches.get(key)||'',appendSwitch:(key,value)=>switches.set(key,value)
 }}}}};
 vm.runInNewContext(source,context);vm.runInNewContext(source,context);
}
const switches=new Map([['disable-features','ExistingFeature']]);apply(switches);
assert.equal(switches.get('disable-features'),'ExistingFeature,MediaRouter,DialMediaRouteProvider');
assert.equal(switches.get('force-webrtc-ip-handling-policy'),'default_public_interface_only');
assert.equal(switches.get('webrtc-ip-handling-policy'),'default_public_interface_only');
const restricted=new Map([['webrtc-ip-handling-policy','disable_non_proxied_udp']]);apply(restricted);
assert.equal(restricted.get('force-webrtc-ip-handling-policy'),'disable_non_proxied_udp');
assert.equal(restricted.get('webrtc-ip-handling-policy'),'disable_non_proxied_udp');
vm.runInNewContext(source,{process:{platform:'linux'},require(){assert.fail('Other platforms must be untouched')}});
console.log('PASS: no multicast discovery, public-route WebRTC retained, explicit stricter policy preserved, Windows only');
