"""Sessions, messages, file changes and search, in one SQLite file (~/.talos/talos.db)."""
from __future__ import annotations

import html
import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path

from . import util

SCHEMA = """
create table if not exists sessions (
    id text primary key, title text, agent text, model text, cwd text, parent text,
    source text default 'cli', created real, updated real,
    tokens_in integer default 0, tokens_out integer default 0, cost real default 0
);
create table if not exists messages (
    id integer primary key autoincrement, session text, seq integer, role text,
    body text, created real, reverted integer default 0
);
create index if not exists messages_session on messages (session, seq);
create table if not exists changes (
    id integer primary key autoincrement, session text, seq integer, path text,
    before blob, after blob, existed integer
);
"""


class Store:
    def __init__(self, path=None):
        self.path = Path(path) if path else util.home() / "talos.db"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(self.path), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        with self.lock:
            self.db.executescript(SCHEMA)
            try:
                self.db.execute(
                    "create virtual table if not exists search using fts5("
                    "text, session unindexed, role unindexed)"
                )
                self.fts = True
            except sqlite3.OperationalError:
                self.fts = False
            self.db.commit()

    def close(self):
        with self.lock:
            self.db.close()

    # ------------------------------------------------------------ sessions
    def new_session(self, cwd, agent="build", model="", title="", parent="", source="cli") -> str:
        session = "ses_" + uuid.uuid4().hex[:12]
        now = time.time()
        with self.lock:
            self.db.execute(
                "insert into sessions (id, title, agent, model, cwd, parent, source, created, updated)"
                " values (?,?,?,?,?,?,?,?,?)",
                (session, title, agent, model, str(cwd), parent, source, now, now),
            )
            self.db.commit()
        return session

    def session(self, session):
        with self.lock:
            row = self.db.execute("select * from sessions where id = ?", (session,)).fetchone()
        return dict(row) if row else None

    def find_session(self, prefix: str):
        """Accept a full id, a unique prefix, or 'last'."""
        with self.lock:
            if prefix in ("last", ""):
                row = self.db.execute(
                    "select id from sessions where parent = '' order by updated desc limit 1"
                ).fetchone()
            else:
                row = self.db.execute(
                    "select id from sessions where id like ? order by updated desc limit 1",
                    (prefix + "%",),
                ).fetchone()
        return row["id"] if row else None

    def sessions(self, limit=50, include_children=False):
        where = "" if include_children else "where parent = ''"
        with self.lock:
            rows = self.db.execute(
                f"select * from sessions {where} order by updated desc limit ?", (limit,)
            ).fetchall()
        return [dict(row) for row in rows]

    def update_session(self, session, **fields):
        fields["updated"] = time.time()
        columns = ", ".join(f"{name} = ?" for name in fields)
        with self.lock:
            self.db.execute(
                f"update sessions set {columns} where id = ?", (*fields.values(), session)
            )
            self.db.commit()

    def add_usage(self, session, tokens_in, tokens_out, cost):
        with self.lock:
            self.db.execute(
                "update sessions set tokens_in = tokens_in + ?, tokens_out = tokens_out + ?,"
                " cost = cost + ?, updated = ? where id = ?",
                (tokens_in, tokens_out, cost, time.time(), session),
            )
            self.db.commit()

    def delete_session(self, session):
        with self.lock:
            for table, column in (("messages", "session"), ("changes", "session"), ("sessions", "id")):
                self.db.execute(f"delete from {table} where {column} = ?", (session,))
            if self.fts:
                self.db.execute("delete from search where session = ?", (session,))
            self.db.commit()

    # ------------------------------------------------------------ messages
    def add_message(self, session, message: dict) -> int:
        with self.lock:
            # a new message after an undo makes the undone turns permanent
            self.db.execute("delete from messages where session = ? and reverted = 1", (session,))
            row = self.db.execute(
                "select coalesce(max(seq), 0) + 1 as seq from messages where session = ?", (session,)
            ).fetchone()
            seq = row["seq"]
            self.db.execute(
                "insert into messages (session, seq, role, body, created) values (?,?,?,?,?)",
                (session, seq, message["role"], json.dumps(message, ensure_ascii=False), time.time()),
            )
            content = message.get("content")
            if self.fts and isinstance(content, str) and content.strip():
                if message["role"] in ("user", "assistant"):
                    self.db.execute(
                        "insert into search (text, session, role) values (?,?,?)",
                        (content, session, message["role"]),
                    )
            self.db.execute("update sessions set updated = ? where id = ?", (time.time(), session))
            self.db.commit()
        return seq

    def messages(self, session, with_seq=False):
        with self.lock:
            rows = self.db.execute(
                "select seq, body from messages where session = ? and reverted = 0 order by seq",
                (session,),
            ).fetchall()
        if with_seq:
            return [(row["seq"], json.loads(row["body"])) for row in rows]
        return [json.loads(row["body"]) for row in rows]

    def replace_messages(self, session, messages):
        """Used by compaction: swap the whole history for a shorter one."""
        with self.lock:
            self.db.execute("delete from messages where session = ?", (session,))
            self.db.execute("delete from changes where session = ?", (session,))
            for seq, message in enumerate(messages, 1):
                self.db.execute(
                    "insert into messages (session, seq, role, body, created) values (?,?,?,?,?)",
                    (session, seq, message["role"], json.dumps(message, ensure_ascii=False), time.time()),
                )
            self.db.commit()

    def search(self, query: str, limit=8, skip_session=""):
        """Full-text search over what was said in every session."""
        words = [word for word in "".join(
            char if char.isalnum() else " " for char in query
        ).split() if word]
        if not words:
            return []
        with self.lock:
            if self.fts:
                rows = self.db.execute(
                    "select session, role, snippet(search, 0, '[', ']', ' ... ', 24) as hit"
                    " from search where search match ? and session != ? order by rank limit ?",
                    (" OR ".join(f'"{word}"' for word in words), skip_session, limit),
                ).fetchall()
            else:
                rows = self.db.execute(
                    "select session, role, substr(body, 1, 300) as hit from messages"
                    " where body like ? and session != ? limit ?",
                    (f"%{words[0]}%", skip_session, limit),
                ).fetchall()
        result = []
        for row in rows:
            info = self.session(row["session"]) or {}
            result.append({
                "session": row["session"], "title": info.get("title", ""),
                "when": time.strftime("%Y-%m-%d", time.localtime(info.get("updated", 0))),
                "role": row["role"], "hit": row["hit"],
            })
        return result

    # ------------------------------------------------------------ undo / redo
    def record_change(self, session, path, before, existed: bool):
        """Remember a file's content before a tool changes it (once per turn and path)."""
        with self.lock:
            seq = self.db.execute(
                "select coalesce(max(seq), 0) as seq from messages where session = ? and role = 'user'"
                " and reverted = 0", (session,),
            ).fetchone()["seq"]
            seen = self.db.execute(
                "select 1 from changes where session = ? and seq = ? and path = ?",
                (session, seq, str(path)),
            ).fetchone()
            if not seen:
                self.db.execute(
                    "insert into changes (session, seq, path, before, existed) values (?,?,?,?,?)",
                    (session, seq, str(path), before, 1 if existed else 0),
                )
                self.db.commit()

    def turns(self, session):
        """User turns that can be undone, newest first: (seq, text, changed files)."""
        result = []
        for seq, message in reversed(self.messages(session, with_seq=True)):
            if message["role"] != "user" or message.get("internal"):
                continue
            with self.lock:
                paths = [row["path"] for row in self.db.execute(
                    "select path from changes where session = ? and seq = ?", (session, seq)
                ).fetchall()]
            text = message.get("content")
            result.append((seq, text if isinstance(text, str) else "(attachment)", paths))
        return result

    def undo(self, session):
        """Take back the last user turn: restore its files, hide its messages."""
        turns = self.turns(session)
        if not turns:
            return None
        seq, text, _ = turns[0]
        restored = []
        with self.lock:
            rows = self.db.execute(
                "select * from changes where session = ? and seq >= ? order by id desc", (session, seq)
            ).fetchall()
            for row in rows:
                path = Path(row["path"])
                after = path.read_bytes() if path.exists() else None
                self.db.execute("update changes set after = ? where id = ?", (after, row["id"]))
                if row["existed"]:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(row["before"] or b"")
                elif path.exists():
                    path.unlink()
                restored.append(str(path))
            self.db.execute(
                "update messages set reverted = 1 where session = ? and seq >= ?", (session, seq)
            )
            self.db.commit()
        return {"text": text, "files": restored}

    def redo(self, session):
        """Bring back the turn that the last undo removed."""
        with self.lock:
            rows = self.db.execute(
                "select seq, body from messages where session = ? and reverted = 1 and role = 'user'"
                " order by seq", (session,),
            ).fetchall()
            real = [row["seq"] for row in rows if not json.loads(row["body"]).get("internal")]
            if not real:
                return None
            seq = real[0]
            upper = real[1] if len(real) > 1 else 10 ** 12
            files = []
            for change in self.db.execute(
                "select * from changes where session = ? and seq >= ? and seq < ? order by id",
                (session, seq, upper),
            ).fetchall():
                path = Path(change["path"])
                if change["after"] is None:
                    if path.exists():
                        path.unlink()
                else:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(change["after"])
                files.append(str(path))
            self.db.execute(
                "update messages set reverted = 0 where session = ? and seq >= ? and seq < ?",
                (session, seq, upper),
            )
            self.db.commit()
        return {"files": files}

    # ------------------------------------------------------------ export / import / share
    def export(self, session) -> dict:
        return {
            "talos": 1,
            "session": self.session(session),
            "messages": self.messages(session),
        }

    def import_(self, data: dict) -> str:
        info = data.get("session") or {}
        session = self.new_session(
            info.get("cwd", "."), info.get("agent", "build"), info.get("model", ""),
            info.get("title", "imported"), source="import",
        )
        for message in data.get("messages") or []:
            self.add_message(session, message)
        return session

    def share_html(self, session) -> str:
        """One self-contained page of the session, with secrets blanked out."""
        info = self.session(session) or {}
        parts = []
        for message in self.messages(session):
            role = message["role"]
            if role == "system":
                continue
            content = message.get("content")
            text = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
            for call in message.get("tool_calls") or []:
                text += f"\n[tool] {call['name']} {json.dumps(call['arguments'], ensure_ascii=False)}"
            label = message.get("name") or role
            parts.append(
                f'<section class="{role}"><h2>{html.escape(label)}</h2>'
                f"<pre>{html.escape(util.redact(text))}</pre></section>"
            )
        title = html.escape(info.get("title") or session)
        return (
            "<!doctype html><html lang=\"en\"><meta charset=\"utf-8\">"
            f"<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\"><title>{title}</title>"
            "<style>body{font:15px/1.55 system-ui,sans-serif;max-width:820px;margin:2rem auto;"
            "padding:0 1rem;background:#faf7f0;color:#1d1b16}h1{font-size:1.3rem}"
            "h2{font-size:.72rem;letter-spacing:.08em;text-transform:uppercase;color:#7a6f5c;margin:0 0 .3rem}"
            "section{border-top:1px solid #e3dccb;padding:.9rem 0}pre{white-space:pre-wrap;"
            "word-break:break-word;margin:0;font:inherit}.tool pre{font:13px/1.45 ui-monospace,monospace;"
            "color:#4d4636}.user pre{font-weight:600}</style>"
            f"<h1>{title}</h1><p>{html.escape(info.get('model') or '')} - exported from Talos</p>"
            + "".join(parts) + "</html>"
        )

    def stats(self) -> dict:
        with self.lock:
            total = self.db.execute(
                "select count(*) as sessions, coalesce(sum(tokens_in), 0) as tokens_in,"
                " coalesce(sum(tokens_out), 0) as tokens_out, coalesce(sum(cost), 0) as cost"
                " from sessions"
            ).fetchone()
            by_model = self.db.execute(
                "select model, count(*) as sessions, sum(tokens_in) as tokens_in,"
                " sum(tokens_out) as tokens_out, sum(cost) as cost from sessions"
                " group by model order by sessions desc"
            ).fetchall()
            messages = self.db.execute("select count(*) as n from messages").fetchone()["n"]
        return {
            **dict(total), "messages": messages, "by_model": [dict(row) for row in by_model],
        }
