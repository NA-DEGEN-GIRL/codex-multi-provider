"""Record the exact local artifact and reject accidental baseline cache reuse."""
import datetime
import hashlib
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
mode = sys.argv[1]
assert mode in ('upstream', 'runtime')
binary = ROOT / 'artifacts' / mode / 'codex.exe'
content = binary.read_bytes()
has_bridge = b'external_agents' in content
if has_bridge != (mode == 'runtime'):
    raise SystemExit('Binary does not match the selected source. Use separate Cargo target directories.')
info = {
    'mode': mode,
    'version': subprocess.check_output([str(binary), '--version'], text=True).strip(),
    'source_base': subprocess.check_output(['git', '-C', str(ROOT / mode), 'rev-parse', 'HEAD'], text=True).strip(),
    'sha256': hashlib.sha256(content).hexdigest(),
    'external_bridge_present': has_bridge,
    'recorded_utc': datetime.datetime.now(datetime.timezone.utc).isoformat(),
}
(binary.parent / 'build-info.json').write_text(json.dumps(info, indent=2), encoding='utf-8')
print(json.dumps(info, indent=2))
