from __future__ import annotations

from dataclasses import dataclass
import json
import sqlite3
from uuid import uuid4

from octo_agent.session.entries import CustomMessageEntry, MessageEntry
from octo_agent.session.jsonl import entry_from_json_line
from octo_agent.session.sqlite import SQLiteSessionStorage, _encode


@dataclass(frozen=True, slots=True)
class TopicRequest:
    id: str
    topic_id: str
    path: tuple[str, ...]
    continuation_entry_id: str | None = None

    @property
    def kind(self) -> str:
        return 'trunk' if self.topic_id == 'main' else 'branch'


class SessionTopics:
    def __init__(self, storage: SQLiteSessionStorage) -> None:
        self.storage = storage
        self.session_id = storage.session_id

    def _root(self, connection: sqlite3.Connection) -> None:
        self.storage._append_in_transaction(connection, ())
        connection.execute('INSERT OR IGNORE INTO topics(session_id, topic_id, title) VALUES (?, ?, ?)',
                           (self.session_id, 'main', '主线'))
        connection.execute('INSERT OR IGNORE INTO topic_selection VALUES (?, ?)', (self.session_id, 'main'))

    @property
    def selected_id(self) -> str:
        with self.storage.database.connection() as connection:
            row = connection.execute('SELECT topic_id FROM topic_selection WHERE session_id=?',
                                     (self.session_id,)).fetchone()
        return row[0] if row else 'main'

    def topics(self) -> list[dict]:
        with self.storage.database.connection() as connection:
            rows = connection.execute('SELECT topic_id, parent_topic_id, anchor_entry_id, title FROM topics '
                                      'WHERE session_id=? ORDER BY rowid', (self.session_id,)).fetchall()
        topics = [dict(id=r[0], parent_id=r[1], anchor_entry_id=r[2], title=r[3]) for r in rows] or [
            dict(id='main', parent_id=None, anchor_entry_id=None, title='主线')]
        labels = {'main': '主干'}
        numbers: dict[str, str] = {}
        siblings: dict[str, int] = {}
        for topic in topics:
            parent = topic['parent_id']
            if parent is None:
                topic.update(kind='trunk', branch_label='主干', parent_label=None)
                continue
            siblings[parent] = siblings.get(parent, 0) + 1
            number = (numbers[parent] + '.' if parent in numbers else '') + str(siblings[parent])
            numbers[topic['id']] = number
            labels[topic['id']] = '分支 ' + number
            topic.update(kind='branch', branch_label=labels[topic['id']], parent_label=labels[parent])
        return topics

    def capture(self, topic_id: str | None = None) -> TopicRequest:
        selected = topic_id or self.selected_id
        by_id = {topic['id']: topic for topic in self.topics()}
        path: list[str] = []
        current = selected
        while current is not None:
            if current not in by_id:
                raise ValueError('Unknown topic')
            topic = by_id[current]
            path.append(topic['title'])
            current = topic['parent_id']
        return TopicRequest(uuid4().hex, selected, tuple(reversed(path)), self.latest_entry_id(selected))

    def latest_entry_id(self, topic_id: str) -> str | None:
        with self.storage.database.connection() as connection:
            row = connection.execute("""SELECT e.id FROM session_entries e
                LEFT JOIN entry_requests a ON a.session_id=e.session_id AND a.entry_id=e.id
                LEFT JOIN topic_requests r ON r.session_id=a.session_id AND r.request_id=a.request_id
                WHERE e.session_id=? AND COALESCE(r.topic_id, 'main')=?
                AND json_extract(e.body_json, '$.type') IN ('message', 'custom_message')
                ORDER BY e.seq DESC LIMIT 1""", (self.session_id, topic_id)).fetchone()
            if row:
                return row[0]
            topic = connection.execute('SELECT anchor_entry_id FROM topics WHERE session_id=? AND topic_id=?',
                                       (self.session_id, topic_id)).fetchone()
        return topic[0] if topic else None

    def create(self, *, parent_id: str, title: str, anchor_entry_id: str | None = None, activate: bool = False) -> str:
        title = self._title(title)
        if anchor_entry_id is None:
            anchor_entry_id = self.latest_entry_id(parent_id)
        topic_id = uuid4().hex
        with self.storage.database.transaction() as connection:
            self._root(connection)
            parent = connection.execute('SELECT 1 FROM topics WHERE session_id=? AND topic_id=?',
                                        (self.session_id, parent_id)).fetchone()
            if not parent:
                raise ValueError('Unknown parent topic')
            if anchor_entry_id is not None:
                anchor = connection.execute('SELECT body_json FROM session_entries WHERE session_id=? AND id=?',
                                            (self.session_id, anchor_entry_id)).fetchone()
                if not anchor or not isinstance(entry_from_json_line(anchor[0]), (MessageEntry, CustomMessageEntry)):
                    raise ValueError('Anchor must be a message in this session')
            connection.execute('INSERT INTO topics VALUES (?, ?, ?, ?, ?)',
                               (self.session_id, topic_id, parent_id, anchor_entry_id, title))
            if activate:
                connection.execute('UPDATE topic_selection SET topic_id=? WHERE session_id=?', (topic_id, self.session_id))
        return topic_id

    def select(self, topic_id: str) -> None:
        with self.storage.database.transaction() as connection:
            self._root(connection)
            if not connection.execute('SELECT 1 FROM topics WHERE session_id=? AND topic_id=?',
                                      (self.session_id, topic_id)).fetchone():
                raise ValueError('Unknown topic')
            connection.execute('UPDATE topic_selection SET topic_id=? WHERE session_id=?',
                               (topic_id, self.session_id))

    def rename(self, topic_id: str, title: str) -> None:
        if topic_id == 'main':
            raise ValueError('主干是会话根，不能作为普通分支重命名')
        with self.storage.database.transaction() as connection:
            self._root(connection)
            result = connection.execute('UPDATE topics SET title=? WHERE session_id=? AND topic_id=?',
                                        (self._title(title), self.session_id, topic_id))
            if not result.rowcount:
                raise ValueError('Unknown topic')

    @staticmethod
    def _title(title: str) -> str:
        title = title.strip()
        if not title or len(title) > 120 or any(ord(c) < 32 for c in title):
            raise ValueError('Topic title must contain 1–120 visible characters')
        return title

    def append_message(self, entry: MessageEntry | CustomMessageEntry, request: TopicRequest) -> None:
        with self.storage.database.transaction() as connection:
            self._root(connection)
            existing = connection.execute('SELECT topic_id FROM topic_requests WHERE session_id=? AND request_id=?',
                                          (self.session_id, request.id)).fetchone()
            if existing and existing[0] != request.topic_id:
                raise ValueError('Request attribution conflict')
            connection.execute('INSERT OR IGNORE INTO topic_requests(session_id, request_id, topic_id, topic_path, continuation_entry_id) VALUES (?, ?, ?, ?, ?)',
                               (self.session_id, request.id, request.topic_id, json.dumps(request.path, ensure_ascii=False), request.continuation_entry_id))
            self.storage._append_in_transaction(connection, ((entry.id, _encode(entry)),))
            existing = connection.execute('SELECT request_id FROM entry_requests WHERE session_id=? AND entry_id=?',
                                          (self.session_id, entry.id)).fetchone()
            if existing and existing[0] != request.id:
                raise ValueError('Message attribution conflict')
            connection.execute('INSERT OR IGNORE INTO entry_requests VALUES (?, ?, ?)',
                               (self.session_id, entry.id, request.id))

    def source_topics(self, entry_ids: list[str]) -> dict[str, dict]:
        with self.storage.database.connection() as connection:
            rows = connection.execute("""SELECT e.id, json_extract(e.body_json, '$.type'),
                COALESCE(r.topic_id, 'main'), COALESCE(r.topic_path, '[]')
                FROM session_entries e
                LEFT JOIN entry_requests a ON a.session_id=e.session_id AND a.entry_id=e.id
                LEFT JOIN topic_requests r ON r.session_id=a.session_id AND r.request_id=a.request_id
                WHERE e.session_id=? AND e.id IN (SELECT value FROM json_each(?))""",
                (self.session_id, json.dumps(entry_ids))).fetchall()
        return {entry_id: {"topic_id": "shared_summary" if kind == "compaction" else topic,
                           "path": json.loads(path)} for entry_id, kind, topic, path in rows}

    def rounds(self) -> list[dict]:
        topics = {topic['id']: topic for topic in self.topics()}
        with self.storage.database.connection() as connection:
            rows = connection.execute("""SELECT e.id, e.seq, COALESCE(r.topic_id, 'main'), r.request_id,
                r.continuation_entry_id, json_extract(e.body_json, '$.message.role'),
                CASE WHEN json_type(e.body_json, '$.message.content')='text'
                     THEN substr(json_extract(e.body_json, '$.message.content'),1,96)
                     ELSE substr(json_extract(e.body_json, '$.message.content[0].text'),1,96) END
                FROM session_entries e
                LEFT JOIN entry_requests a ON a.session_id=e.session_id AND a.entry_id=e.id
                LEFT JOIN topic_requests r ON r.session_id=a.session_id AND r.request_id=a.request_id
                WHERE e.session_id=? AND json_extract(e.body_json, '$.type') IN ('message','custom_message')
                ORDER BY e.seq""", (self.session_id,)).fetchall()
        turns: dict[str, dict] = {}
        entry_turn: dict[str, str] = {}
        previous: dict[str, str] = {}
        legacy: str | None = None
        for entry_id, seq, topic_id, request_id, continuation, role, text in rows:
            if request_id is None:
                if role == 'user' or legacy is None:
                    legacy = 'legacy-' + entry_id
                request_id = legacy
            if request_id not in turns:
                turns[request_id] = dict(id=request_id, topic_id=topic_id, first_entry_id=entry_id,
                    last_entry_id=entry_id, first_seq=seq, title='', message_count=0,
                    parent_turn_id=None, entry_ids=[], continuation=continuation, previous=previous.get(topic_id))
                previous[topic_id] = request_id
            turn = turns[request_id]
            turn['entry_ids'].append(entry_id)
            turn['last_entry_id'] = entry_id
            turn['message_count'] += 1
            if not turn['title'] and role == 'user':
                turn['title'] = (text or '非文本提问').strip().replace('\n', ' ')
            entry_turn[entry_id] = request_id
        numbers: dict[str, int] = {}
        for turn in turns.values():
            topic_id = turn['topic_id']
            anchor = turn.pop('continuation')
            preceding = turn.pop('previous')
            parent = entry_turn.get(anchor) if anchor else None
            if parent == turn['id']:
                parent = None
            if parent is None:
                parent = preceding or entry_turn.get(topics[topic_id]['anchor_entry_id'])
            turn['parent_turn_id'] = parent if parent != turn['id'] else None
            numbers[topic_id] = numbers.get(topic_id, 0) + 1
            turn['number'] = numbers[topic_id]
            turn['title'] = turn['title'] or '对话与工具记录'
        return list(turns.values())

    def snapshot(self, topic_id: str | None = None, before: int = 9223372036854775807, *, after: int | None = None) -> dict:
        topics = self.topics()
        selected = self.selected_id
        viewed = topic_id or selected
        if viewed not in {topic['id'] for topic in topics}:
            raise ValueError('Unknown topic')
        with self.storage.database.connection() as connection:
            forward = after is not None
            comparator, order, cursor = ('>', 'ASC', after) if forward else ('<', 'DESC', before)
            rows = connection.execute(f'''SELECT e.id, e.seq, e.body_json, r.request_id FROM session_entries e
                LEFT JOIN entry_requests a ON a.session_id=e.session_id AND a.entry_id=e.id
                LEFT JOIN topic_requests r ON r.session_id=a.session_id AND r.request_id=a.request_id
                WHERE e.session_id=? AND COALESCE(r.topic_id, 'main')=? AND e.seq{comparator}?
                AND json_extract(e.body_json, '$.type') IN ('message', 'custom_message')
                ORDER BY e.seq {order} LIMIT 101''', (self.session_id, viewed, cursor)).fetchall()
            statistics = {topic: (count, first, last) for topic, count, first, last in connection.execute('''
                SELECT COALESCE(r.topic_id, 'main'), COUNT(*), MIN(e.seq), MAX(e.seq) FROM session_entries e
                LEFT JOIN entry_requests a ON a.session_id=e.session_id AND a.entry_id=e.id
                LEFT JOIN topic_requests r ON r.session_id=a.session_id AND r.request_id=a.request_id
                WHERE e.session_id=? AND json_extract(e.body_json, '$.type') IN ('message', 'custom_message')
                GROUP BY COALESCE(r.topic_id, 'main')''', (self.session_id,)).fetchall()}
        page = rows[:100] if forward else list(reversed(rows[:100]))
        _, first, last = statistics.get(viewed, (0, None, None))
        first_cursor = page[0][1] if page else None
        last_cursor = page[-1][1] if page else after
        messages = []
        for entry_id, seq, body, request_id in page:
            entry = entry_from_json_line(body)
            if isinstance(entry, MessageEntry):
                message = entry.message
                messages.append(dict(id=entry_id, seq=seq, role=message.role, text=message.text,
                                     request_id=request_id, timestamp=entry.timestamp))
            elif isinstance(entry, CustomMessageEntry) and entry.display:
                from octo_agent.messages import content_text
                messages.append(dict(id=entry_id, seq=seq, role='custom', text=content_text(entry.content),
                                     request_id=request_id, timestamp=entry.timestamp))
        return dict(topics=[{**topic, 'message_count': statistics.get(topic['id'], (0, None, None))[0], 'latest_entry_id': self.latest_entry_id(topic['id'])} for topic in topics],
                    selected_topic_id=selected, viewed_topic_id=viewed, messages=messages, turns=self.rounds(),
                    has_more=first is not None and first_cursor is not None and first < first_cursor,
                    before=first_cursor, after=last_cursor,
                    has_newer=last is not None and (last_cursor is None or last > last_cursor))

    def locate_entry(self, entry_id: str) -> dict:
        with self.storage.database.connection() as connection:
            row = connection.execute('''SELECT e.seq, COALESCE(r.topic_id, 'main') FROM session_entries e
                LEFT JOIN entry_requests a ON a.session_id=e.session_id AND a.entry_id=e.id
                LEFT JOIN topic_requests r ON r.session_id=a.session_id AND r.request_id=a.request_id
                WHERE e.session_id=? AND e.id=? AND json_extract(e.body_json, '$.type') IN ('message', 'custom_message')''',
                (self.session_id, entry_id)).fetchone()
        if not row:
            raise ValueError('Unknown message in this session')
        return dict(seq=row[0], topic_id=row[1], entry_id=entry_id)

    def last_request(self) -> TopicRequest | None:
        with self.storage.database.connection() as connection:
            row = connection.execute('''SELECT r.request_id, r.topic_id, r.topic_path, r.continuation_entry_id FROM session_entries e
                JOIN entry_requests a ON a.session_id=e.session_id AND a.entry_id=e.id
                JOIN topic_requests r ON r.session_id=a.session_id AND r.request_id=a.request_id
                WHERE e.session_id=? ORDER BY e.seq DESC LIMIT 1''', (self.session_id,)).fetchone()
        return TopicRequest(row[0], row[1], tuple(json.loads(row[2])), row[3]) if row else None
