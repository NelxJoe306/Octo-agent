from __future__ import annotations

import asyncio
from contextlib import contextmanager
from datetime import datetime, timezone
from html import escape
import os
from pathlib import Path
import tempfile
from time import monotonic, sleep
from typing import TYPE_CHECKING

from octo_memory.models import MemoryFact

if TYPE_CHECKING:
    from octo_memory.store import MemoryStore


def _inline(value: str) -> str:
    text=escape(' '.join(value.split()),quote=False)
    for character in ('\\','`','*','_','[',']'):
        text=text.replace(character,'\\'+character)
    return text


def _date(timestamp: float) -> str:
    try:
        return datetime.fromtimestamp(timestamp,timezone.utc).date().isoformat()
    except (ValueError,OverflowError,OSError):
        return '未知日期'


def render_memory(facts: tuple[MemoryFact,...]) -> str:
    lines=['# 长期记忆','',
           '> 本文件自动生成，请勿手改。SQLite 是唯一权威；更正请通过正常记忆提交，运行时不会读取本文件。','']
    def line(fact: MemoryFact) -> str:
        sources='; '.join(f'{_date(source.timestamp)} · {_inline(source.session_id)}/{_inline(source.entry_id)} ({_inline(source.role)})'
                          for source in fact.sources)
        state={'active':'有效','superseded':'已更正','retracted':'已撤回'}[fact.state]
        replacement=f' → `{fact.replacement_id}`' if fact.replacement_id else ''
        return f'- `{fact.id}` [{state}]{replacement} {_inline(fact.text)} — 来源：{sources}'
    ordered=sorted(facts,key=lambda fact:fact.id)
    for title,category in (('用户偏好','preference'),('其他事实','fact')):
        lines.extend([f'## {title}',''])
        selected=[fact for fact in ordered if fact.state=='active' and fact.category==category]
        lines.extend([line(fact) for fact in selected] or ['_暂无记录。_'])
        lines.append('')
    history=[fact for fact in ordered if fact.state!='active']
    lines.extend(['<details>','<summary>历史记录（更正与撤回）</summary>',''])
    lines.extend([line(fact) for fact in history] or ['_暂无历史。_'])
    lines.extend(['','</details>',''])
    return '\n'.join(lines)


@contextmanager
def _export_lock(path: Path):
    with path.open('a+b') as handle:
        handle.seek(0,os.SEEK_END)
        if not handle.tell():
            handle.write(b'\0')
            handle.flush()
        handle.seek(0)
        deadline=monotonic()+5
        while True:
            try:
                if os.name=='nt':
                    import msvcrt
                    msvcrt.locking(handle.fileno(),msvcrt.LK_NBLCK,1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
                break
            except OSError:
                if monotonic()>=deadline:
                    raise TimeoutError('Memory mirror export lock is busy')
                sleep(0.05)
        try:
            yield
        finally:
            handle.seek(0)
            if os.name=='nt':
                import msvcrt
                msvcrt.locking(handle.fileno(),msvcrt.LK_UNLCK,1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(),fcntl.LOCK_UN)


class MemoryMirror:
    def __init__(self,store: MemoryStore,path: Path) -> None:
        self.store=store
        self.path=Path(path)

    async def export(self) -> bool:
        return await asyncio.to_thread(self._export)

    def _export(self) -> bool:
        self.path.parent.mkdir(parents=True,exist_ok=True)
        # 导出独占锁覆盖读取快照和发布，避免较旧快照覆盖较新镜像。
        with _export_lock(self.path.with_name(self.path.name+'.lock')):
            content=render_memory(self.store._facts(session_id=None,include_inactive=True,category=None,limit=None)).encode('utf-8')
            if self.path.is_file() and self.path.read_bytes()==content:
                return False
            temporary=None
            try:
                with tempfile.NamedTemporaryFile(dir=self.path.parent,prefix='.'+self.path.name+'.',suffix='.tmp',delete=False) as handle:
                    temporary=Path(handle.name)
                    handle.write(content)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary,self.path)
                return True
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
