from __future__ import annotations

import argparse
import asyncio
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))

from octo_agent.session.sqlite import SQLiteSessionDatabase
from octo_coding.paths import OctoPaths
from octo_memory import MemoryStore
from octo_memory.mirror import MemoryMirror


def main() -> None:
    parser=argparse.ArgumentParser(description='Rebuild memory.md from SQLite without calling a model')
    parser.add_argument('--home',type=Path,help='Octo state directory; defaults to OCTO_HOME')
    args=parser.parse_args()
    paths=OctoPaths(home=args.home.resolve()) if args.home is not None else OctoPaths()
    if not paths.database_path.is_file():
        parser.error(f'Session database not found: {paths.database_path}')
    mirror=MemoryMirror(MemoryStore(SQLiteSessionDatabase(paths.database_path)),paths.home/'memory.md')
    changed=asyncio.run(mirror.export())
    print(f'{"Updated" if changed else "Unchanged"}: {mirror.path}')


if __name__=='__main__':
    main()
