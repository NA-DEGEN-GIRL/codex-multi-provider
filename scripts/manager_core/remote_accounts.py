"""Windows-owned aliases synchronized to identity-matched SSH llm-usage accounts."""
from .release_code import script_path
import json
from pathlib import Path
import shlex
from .store import now


class RemoteAccounts:
    def __init__(self, root, store, remote, login):
        self.root, self.store, self.remote, self.login = Path(root), store, remote, login

    def sync(self, profile_id, host_alias, *, inspect_only=False):
        profile=self.store.profile(profile_id)
        if profile.get('auth_mode') == 'source':
            from .current_account import status as source_status
            status=source_status(self.store, profile_id, verify_server=True, root=self.root)
        else:
            status=self.login.status(profile_id)
        if status.get('state') != 'signed_in' or not status.get('server_verified'):
            raise ValueError('Windows에서 로그인 계정의 서버 확인을 완료한 뒤 SSH 별칭을 연결할 수 있습니다.')
        if status.get('account_fingerprint') != profile.get('account_fingerprint'):
            raise RuntimeError('Windows 계정 연결이 변경되었습니다. SSH 별칭을 변경하지 않았습니다.')
        source=script_path(self.root, 'scripts/remote_helpers/account_alias.py').read_text(encoding='utf-8')
        # Discover the executable in the standard per-user installation without
        # consulting arbitrary shell startup files. Its shebang selects its own venv.
        bootstrap = "from pathlib import Path; import os,shutil,sys; p=Path(shutil.which('llm-usage') or Path.home()/'.local/bin/llm-usage'); s=p.read_text().splitlines()[0]; assert s.startswith('#!/') and ' ' not in s; os.execv(s[2:],[s[2:],'-c',sys.argv[1]])"
        request=dict(action='inspect' if inspect_only else 'sync', alias=profile['alias'],
                     account_fingerprint=status['account_fingerprint'])
        result=self.remote._run(host_alias, shlex.join(['/usr/bin/python3','-c',bootstrap,source]),
                                input=json.dumps(request).encode(),timeout=20)
        try:value=json.loads(result.stdout)
        except ValueError:raise RuntimeError('SSH의 llm-usage 계정 응답을 읽지 못했습니다.') from None
        if result.returncode or value.get('state')=='blocked':
            raise RuntimeError('SSH 계정의 실제 식별 정보 또는 별칭 충돌을 확인하세요.')
        value.update(host_alias=host_alias, profile_id=profile_id, checked_at=now())
        def save(data):
            item=self.store.profile(profile_id,data)
            if item.get('account_fingerprint') != status['account_fingerprint'] or item['alias'] != profile['alias']:
                raise RuntimeError('동기화 중 Windows 계정 설정이 변경되었습니다. 다시 동기화하세요.')
            item.setdefault('ssh_alias_sync',{})[host_alias]=value
        self.store.mutate(save)
        return value
