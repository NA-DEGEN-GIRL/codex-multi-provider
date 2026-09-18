"""One-time import of existing SSH declarations into the shared event registry."""
import json
from pathlib import Path
from uuid import UUID
from .store import Store, atomic_json
from .updates import UpdateError, _lock_file, _unlock_file

WRITER='00000000-0000-4000-8000-000000000044'


def ensure(root):
    store=Store(root)
    directory=store.directory/'record-signals/workspaces'
    directory.mkdir(parents=True,exist_ok=True)
    if list(directory.glob('*.json')):return
    try:lock=_lock_file(directory/'migration.lock')
    except UpdateError:return  # Another launcher is publishing the same seed.
    try:
        if list(directory.glob('*.json')):return
        sources=[Path.home()/'.codex']+[Path(p['home']) for p in store.read()['profiles']]
        projects={}
        for home in sources:
            path=home/'.codex-global-state.json'
            if not path.is_file() or path.stat().st_size>8*1024*1024:continue
            try:data=json.loads(path.read_text(encoding='utf-8-sig'))
            except (ValueError,OSError):continue
            for p in data.get('remote-projects',[]):
                try:
                    if str(UUID(p['id']))!=p['id'] or not p['hostId'].startswith('remote-ssh-') or not p['remotePath'].startswith('/'):continue
                    if any(not isinstance(p[k],str) or len(p[k])>4096 for k in ('hostId','remotePath','label')):continue
                    projects[p['id']]={k:p[k] for k in ('id','hostId','remotePath','label')}
                except (ValueError,KeyError,TypeError,AttributeError):continue
        atomic_json(directory/(WRITER+'.json'),dict(version=1,projects=[
            [identity,1,WRITER,value] for identity,value in sorted(projects.items())][:4096]))
    finally:_unlock_file(lock)
