#!/usr/bin/env python3
"""Read-only latest detection. Never writes versions.json or selects install versions."""
from concurrent.futures import ThreadPoolExecutor
import json
import re
import subprocess
import sys
try:
    from .bundle import policy
except ImportError:  # Standalone script entry point.
    from bundle import policy


def version_key(tag):
    match=re.fullmatch(r'v?(\d+)\.(\d+)\.(\d+)(?:\+k3s(\d+))?',tag)
    return tuple(int(x or 0) for x in match.groups()) if match else None


def detect(name,repo,approved):
    result={'approved':approved,'latest':None,'update_available':None,'error':None}
    try:
        p=subprocess.run(['curl','--fail','--silent','--show-error','--location','--connect-timeout','3','--max-time','3',
                          f'https://api.github.com/repos/{repo}/releases/latest'],capture_output=True,text=True,timeout=4)
        if p.returncode:raise ValueError(p.stderr.strip()[:256])
        latest=json.loads(p.stdout)['tag_name']
        result['latest']=latest
        old,new=version_key(approved),version_key(latest)
        if old and new:result['update_available']=new>old
        else:result['error']='Unsupported stable version syntax; no automatic selection'
    except (OSError,ValueError,KeyError,subprocess.TimeoutExpired) as exc:
        result['error']=str(exc)[:256]
    return name,result


def main():
    current=policy()['runtime']
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures=[pool.submit(detect,k,repo,current[k]) for k,repo in (('k3s_version','k3s-io/k3s'),('cilium_version','cilium/cilium'),('cilium_cli_version','cilium/cilium-cli'))]
        components=dict(f.result() for f in futures)
    values=[c['update_available'] for c in components.values()]
    available=True if True in values else None if None in values else False
    print(json.dumps({'status':'checked','selection_changed':False,'update_available':available,
                      'requires_compatibility_tests':True,'components':components}))
    return 0


if __name__=='__main__':sys.exit(main())
