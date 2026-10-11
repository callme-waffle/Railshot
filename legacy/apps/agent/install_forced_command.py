#!/usr/bin/env python3
"""Render one restricted authorized_keys line; never edits the SSH configuration."""
import argparse
import base64
import ipaddress
from pathlib import Path
import re


def authorized_key_line(public_key,source_ip,python_path,repo_path,config_dir):
    # Only an Ed25519 public key, never a private key or user-supplied key options.
    parts=public_key.strip().split()
    if len(parts) not in (2,3) or parts[0]!='ssh-ed25519':
        raise ValueError('One Ed25519 public key required')
    try:
        blob=base64.b64decode(parts[1],validate=True)
    except ValueError:
        raise ValueError('Invalid public key') from None
    prefix=b'\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20'
    if not blob.startswith(prefix) or len(blob)!=len(prefix)+32:
        raise ValueError('Invalid Ed25519 public key encoding')
    source=ipaddress.ip_address(source_ip)
    cidr=str(source)+('/32' if source.version==4 else '/128')
    paths=[]
    for path in (python_path,repo_path,config_dir):
        text=str(path)
        if not re.fullmatch(r'/[A-Za-z0-9_./-]+',text) or '..' in Path(text).parts:
            raise ValueError('Fixed absolute paths without shell metacharacters required')
        paths.append(text.rstrip('/'))
    python_path,repo_path,config_dir=paths
    command=f'{python_path} -I {repo_path}/apps/agent/runner.py --repo {repo_path} --config-dir {config_dir}'
    # -I omits script directory: runner adds its trusted package location explicitly.
    return f'restrict,from="{cidr}",command="{command}" ssh-ed25519 {parts[1]} jasmin-job-v1'


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--public-key',type=Path,required=True)
    parser.add_argument('--source-ip',required=True)
    parser.add_argument('--python',required=True)
    parser.add_argument('--repo',required=True)
    parser.add_argument('--config-dir',default='/etc/jasmin')
    args=parser.parse_args()
    print(authorized_key_line(args.public_key.read_text(),args.source_ip,args.python,args.repo,args.config_dir))


if __name__=='__main__':
    main()
