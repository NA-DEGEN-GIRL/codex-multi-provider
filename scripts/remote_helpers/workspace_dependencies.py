"""Read-only MCP discovery for an already installed native workspace bundle.

No installation, network requests, credentials or arbitrary filesystem browsing.
This supplies the desktop dependency-discovery capability to SSH skill users.
"""
import json
import os
from pathlib import Path
import sys


def dependencies(root=None):
    root = Path(root) if root else Path.home()/'.cache/codex-runtimes/codex-primary-runtime'
    root = root.resolve(strict=True)
    metadata = json.loads((root/'runtime.json').read_text())
    if not isinstance(metadata, dict) or metadata.get('targetPlatform') != 'linux':
        raise ValueError('The installed bundle is not a Linux runtime.')
    paths = {
        'nodePath': root/'dependencies/node/bin/node',
        'nodeModulesPath': root/'dependencies/node/node_modules',
        'pythonPath': root/'dependencies/python/bin/python3',
        'overrideBinPath': root/'dependencies/bin/override',
        'fallbackBinPath': root/'dependencies/bin/fallback',
    }
    libraries = sorted((root/'dependencies/python/lib').glob('python*/site-packages'))
    if len(libraries) != 1:
        raise ValueError('The native Python library directory is ambiguous or missing.')
    paths['pythonLibrariesPath'] = libraries[0]
    for name, path in paths.items():
        executable = name in ('nodePath', 'pythonPath')
        valid_kind = path.is_file() and os.access(path, os.X_OK) if executable else path.is_dir()
        if not valid_kind or not path.resolve().is_relative_to(root):
            raise ValueError('The native dependency bundle is incomplete.')
    for name, pattern in [('libreOfficePath', '**/soffice'), ('pdftoppmPath', '**/pdftoppm')]:
        candidates = sorted((root/'dependencies/native').glob(pattern))
        for candidate in candidates:
            if candidate.is_file() and candidate.resolve().is_relative_to(root) and os.access(candidate, os.X_OK):
                paths[name] = candidate
                break
    return dict(bundleVersion=metadata.get('bundleVersion'), runtimeRoot=str(root),
                artifactToolVersion=metadata.get('artifactToolVersion'),
                **{name:str(path) for name,path in paths.items()},
                instructions='Use the returned native Node/Python paths and bundled libraries. '
                'Add overrideBinPath and fallbackBinPath to the command PATH when invoking packaged tools. '
                'This is the SSH host runtime, independent of the Windows installation. '
                'For documents use the returned bundled libreOfficePath, never an unrelated desktop installation.')


def reply(request, root=None):
    identity=request.get('id');method=request.get('method')
    if identity is None:
        return None
    result={}
    if method=='initialize':
        protocol=request.get('params',{}).get('protocolVersion','2024-11-05')
        result=dict(protocolVersion=protocol,capabilities={'tools':{}},
                    serverInfo={'name':'workspace_dependencies','version':'1.0.0'})
    elif method=='ping':
        pass
    elif method=='tools/list':
        result={'tools':[{'name':'load_workspace_dependencies',
          'description':'Find installed Linux runtimes and libraries for documents, PDFs, spreadsheets and presentations on this SSH host.',
          'inputSchema':{'type':'object','properties':{},'additionalProperties':False},
          'annotations':{'readOnlyHint':True,'destructiveHint':False,'openWorldHint':False}}]}
    elif method=='tools/call' and request.get('params',{}).get('name')=='load_workspace_dependencies':
        try:
            value=dependencies(root)
            result={'content':[{'type':'text','text':json.dumps(value)}],'structuredContent':value}
        except (OSError,ValueError,KeyError):
            result={'isError':True,'content':[{'type':'text','text':'Linux workspace dependencies are missing or invalid. Inspect the native bundle installation.'}]}
    else:
        return {'jsonrpc':'2.0','id':identity,'error':{'code':-32601,'message':'Unknown read-only discovery method'}}
    return {'jsonrpc':'2.0','id':identity,'result':result}


def main():
    while line:=sys.stdin.buffer.readline(1024*1024+1):
        if len(line)>1024*1024:
            break
        try:
            request=json.loads(line)
            if not isinstance(request,dict):continue
            response=reply(request)
            if response is not None:print(json.dumps(response),flush=True)
        except (ValueError,TypeError,AttributeError):
            print(json.dumps({'jsonrpc':'2.0','id':None,'error':{'code':-32700,'message':'Invalid JSON-RPC request'}}),flush=True)


if __name__=='__main__':main()
