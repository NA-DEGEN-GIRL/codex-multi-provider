"""Open or inspect one explicitly selected independent Windows login profile."""
import argparse,json,sys
from pathlib import Path
from control_center import ControlCenter

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--profile-id',required=True)
    parser.add_argument('--open',action='store_true')
    parser.add_argument('--prepare',action='store_true')
    parser.add_argument('--verify',action='store_true')
    parser.add_argument('--notify',action='store_true')
    args=parser.parse_args();center=ControlCenter()
    try:
        if args.prepare:
            p=center.native_login.prepare(args.profile_id)
            result=dict(alias=p['alias'],profile_id=p['id'],home=p['home'],auth_mode=p['auth_mode'])
        elif args.open:
            result=center.dispatch('profile.login',{'profile_id':args.profile_id})
            p=result.get('profile',{})
            result={k:result[k] for k in ('state','profile_id','message') if k in result}
            result.update(alias=p.get('alias'),process_id=p.get('process_id'),window_handle=p.get('window_handle'))
        elif args.verify:result=center.native_login.verify(args.profile_id)
        else:result=center.native_login.status(args.profile_id)
        if sys.stdout:print(json.dumps(result,ensure_ascii=False))
        return 0
    except (OSError,ValueError,RuntimeError):
        message='로그인 프로필을 준비하지 못했습니다. 관리 앱의 계정 상태를 확인하세요.'
        if args.notify:
            import ctypes
            ctypes.windll.user32.MessageBoxW(None,message,'Codex 계정 로그인',0x10)
        elif sys.stderr:print(message,file=sys.stderr)
        return 1

if __name__=='__main__':
    if sys.stdout:sys.stdout.reconfigure(encoding='utf-8')
    raise SystemExit(main())
