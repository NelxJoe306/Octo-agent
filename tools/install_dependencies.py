from __future__ import annotations

import argparse
import os
from pathlib import Path
import shutil
import subprocess

ROOT=Path(__file__).resolve().parents[1]


def link_directory(link: Path,target: Path) -> None:
    if not link.resolve().is_relative_to(ROOT) or not target.resolve().is_relative_to(ROOT):
        raise RuntimeError('Dependency links must stay inside this checkout')
    if link.exists():
        if link.resolve()!=target.resolve():
            raise RuntimeError(f'Existing directory preserved: {link}; move it aside before installing')
        return
    if os.name=='nt':
        quote=lambda value: "'"+str(value).replace("'","''")+"'"
        command=f'New-Item -ItemType Junction -Path {quote(link)} -Target {quote(target)} | Out-Null'
        subprocess.run(['powershell.exe','-NoProfile','-NonInteractive','-Command',command],check=True)
    else:
        link.symlink_to(target,target_is_directory=True)


def main() -> None:
    parser=argparse.ArgumentParser(description='Install dependencies separately from source and tests')
    group=parser.add_mutually_exclusive_group()
    group.add_argument('--python-only',action='store_true')
    group.add_argument('--web-only',action='store_true')
    parser.add_argument('--offline',action='store_true')
    args=parser.parse_args()
    if not args.web_only:
        uv=shutil.which('uv')
        if uv is None:
            parser.error('uv is required for Python installation')
        environment=ROOT/'dependencies/python/.venv'
        interpreter=environment/('Scripts/python.exe' if os.name=='nt' else 'bin/python')
        if not interpreter.exists():
            create=[uv,'venv',str(environment)]
            if args.offline:create.append('--offline')
            subprocess.run(create,cwd=ROOT,check=True)
        command=[uv,'pip','install','--python',str(interpreter),'-e',str(ROOT)]
        if args.offline:command.append('--offline')
        subprocess.run(command,cwd=ROOT,check=True)
        link_directory(ROOT/'.venv',environment)
        print(f'Python dependencies: {environment}')
    if not args.python_only:
        npm=shutil.which('npm')
        if npm is None:
            parser.error('npm is required for Web installation')
        generated=ROOT/'dependencies/web'
        generated.mkdir(parents=True,exist_ok=True)
        for name in ('package.json','package-lock.json'):
            shutil.copyfile(ROOT/'web'/name,generated/name)
        command=[npm,'ci','--prefix',str(generated)]
        if args.offline:command.append('--offline')
        subprocess.run(command,cwd=ROOT,check=True)
        link_directory(ROOT/'web/node_modules',generated/'node_modules')
        print(f'Web dependencies: {generated/"node_modules"}')


if __name__=='__main__':
    main()
