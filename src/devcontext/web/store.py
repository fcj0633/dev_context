"""SQLite is the source of truth for events, answers and recovery."""
from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
from threading import RLock
from uuid import uuid4

from .protocol import EVENT_TYPES, TERMINAL, RunError, RunEvent, now


def encode(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


class RunStore:
    def __init__(self, path):
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = RLock()
        with self.connection() as db:
            db.execute("PRAGMA journal_mode=WAL")
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version not in {0, 1}:
                raise RuntimeError("Unsupported Web database version")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS sessions(id TEXT PRIMARY KEY,title TEXT NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY,session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
                  client_request_id TEXT NOT NULL,query TEXT NOT NULL,profile TEXT NOT NULL,status TEXT NOT NULL,
                  created_at TEXT NOT NULL,updated_at TEXT NOT NULL,last_seq INTEGER NOT NULL DEFAULT 0,
                  answer TEXT NOT NULL DEFAULT '',summary TEXT NOT NULL DEFAULT '{}',reason TEXT,error TEXT,
                  worker_active INTEGER NOT NULL DEFAULT 1,UNIQUE(session_id,client_request_id));
                CREATE TABLE IF NOT EXISTS messages(id TEXT PRIMARY KEY,session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
                  run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,role TEXT NOT NULL,content TEXT NOT NULL,created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS run_events(run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,seq INTEGER NOT NULL,
                  type TEXT NOT NULL,timestamp TEXT NOT NULL,data TEXT NOT NULL,PRIMARY KEY(run_id,seq));
                CREATE TABLE IF NOT EXISTS run_sections(run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
                  section_id TEXT NOT NULL,position INTEGER NOT NULL,data TEXT NOT NULL,PRIMARY KEY(run_id,section_id));
                CREATE TABLE IF NOT EXISTS run_evidence(run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
                  label TEXT NOT NULL,data TEXT NOT NULL,PRIMARY KEY(run_id,label));
                CREATE INDEX IF NOT EXISTS messages_session ON messages(session_id,created_at);
                CREATE INDEX IF NOT EXISTS runs_session ON runs(session_id,created_at);
                PRAGMA user_version=1;
            """)

    @contextmanager
    def connection(self):
        with self.lock:
            db = sqlite3.connect(self.path, timeout=5)
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("PRAGMA busy_timeout=5000")
            try:
                with db:
                    yield db
            finally:
                db.close()

    def health(self):
        with self.connection() as db:
            db.execute("SELECT 1").fetchone()

    def _run(self, db, run_id):
        row = db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        if row is None:
            raise RunError("not_found", 404)
        return row

    def _event(self, db, run_id, kind, data):
        if kind not in EVENT_TYPES:
            raise ValueError("Unknown RunEvent type")
        seq = self._run(db, run_id)["last_seq"] + 1
        timestamp = now()
        db.execute("INSERT INTO run_events VALUES(?,?,?,?,?)", (run_id, seq, kind, timestamp, encode(data)))
        db.execute("UPDATE runs SET last_seq=?,updated_at=? WHERE id=?", (seq, timestamp, run_id))
        return RunEvent(run_id, seq, kind, timestamp, data)

    def create_session(self, title="新会话"):
        value = {"id": str(uuid4()), "title": title, "created_at": now(), "updated_at": now()}
        with self.connection() as db:
            db.execute("INSERT INTO sessions VALUES(:id,:title,:created_at,:updated_at)", value)
        return value

    def list_sessions(self, limit=50, offset=0):
        with self.connection() as db:
            return [dict(r) for r in db.execute("SELECT * FROM sessions ORDER BY updated_at DESC,id LIMIT ? OFFSET ?", (limit, offset))]

    def session(self, session_id):
        with self.connection() as db:
            row = db.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
            if row is None:
                raise RunError("not_found", 404)
            result = dict(row)
            result["messages"] = [dict(r) for r in db.execute("SELECT * FROM messages WHERE session_id=? ORDER BY created_at,rowid", (session_id,))]
            ids = [r[0] for r in db.execute("SELECT id FROM runs WHERE session_id=? ORDER BY created_at,rowid", (session_id,))]
            result["runs"] = [self.run(r) for r in ids]
            return result

    def rename(self, session_id, title):
        with self.connection() as db:
            if not db.execute("UPDATE sessions SET title=?,updated_at=? WHERE id=?", (title, now(), session_id)).rowcount:
                raise RunError("not_found", 404)
        return self.session(session_id)

    def delete(self, session_id):
        with self.connection() as db:
            if db.execute("SELECT 1 FROM runs WHERE session_id=? AND worker_active=1", (session_id,)).fetchone():
                raise RunError("session_busy")
            if not db.execute("DELETE FROM sessions WHERE id=?", (session_id,)).rowcount:
                raise RunError("not_found", 404)

    def existing(self, session_id, client_request_id):
        with self.connection() as db:
            row = db.execute("SELECT id FROM runs WHERE session_id=? AND client_request_id=?", (session_id, client_request_id)).fetchone()
            return self.run(row[0]) if row else None

    def create_run(self, session_id, query, profile, client_request_id):
        run_id, timestamp = str(uuid4()), now()
        with self.connection() as db:
            session = db.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
            if session is None:
                raise RunError("not_found", 404)
            if db.execute("SELECT 1 FROM runs WHERE session_id=? AND worker_active=1", (session_id,)).fetchone():
                raise RunError("session_busy")
            db.execute("INSERT INTO runs(id,session_id,client_request_id,query,profile,status,created_at,updated_at) VALUES(?,?,?,?,?,'queued',?,?)",
                       (run_id, session_id, client_request_id, query, profile, timestamp, timestamp))
            for role, content in (("user", query), ("assistant", "")):
                db.execute("INSERT INTO messages VALUES(?,?,?,?,?,?)", (str(uuid4()), session_id, run_id, role, content, timestamp))
            title = query[:40] if session["title"] == "新会话" else session["title"]
            db.execute("UPDATE sessions SET title=?,updated_at=? WHERE id=?", (title, timestamp, session_id))
            self._event(db, run_id, "run.created", {"status": "queued", "profile": profile})
        return self.run(run_id)

    def run(self, run_id):
        with self.connection() as db:
            result = dict(self._run(db, run_id))
            result["worker_active"] = bool(result["worker_active"])
            result["summary"] = json.loads(result["summary"])
            result["error"] = json.loads(result["error"]) if result["error"] else None
            result["user_message"] = dict(db.execute("SELECT * FROM messages WHERE run_id=? AND role='user'", (run_id,)).fetchone())
            result["sections"] = [json.loads(r[0]) for r in db.execute("SELECT data FROM run_sections WHERE run_id=? ORDER BY position", (run_id,))]
            result["evidence"] = [json.loads(r[0]) for r in db.execute("SELECT data FROM run_evidence WHERE run_id=? ORDER BY rowid", (run_id,))]
            result["activity"] = [RunEvent(r["run_id"], r["seq"], r["type"], r["timestamp"], json.loads(r["data"])).to_dict()
                                  for r in db.execute("SELECT * FROM run_events WHERE run_id=? AND type LIKE 'retrieval.%' ORDER BY seq", (run_id,))]
            return result

    def events(self, run_id, after=0, limit=100):
        with self.connection() as db:
            self._run(db, run_id)
            return [RunEvent(r["run_id"], r["seq"], r["type"], r["timestamp"], json.loads(r["data"])).to_dict()
                    for r in db.execute("SELECT * FROM run_events WHERE run_id=? AND seq>? ORDER BY seq LIMIT ?", (run_id, after, limit))]

    def emit(self, run_id, kind, data):
        with self.connection() as db:
            if self._run(db, run_id)["status"] in TERMINAL:
                return None
            if kind == "run.started":
                db.execute("UPDATE runs SET status='running' WHERE id=?", (run_id,))
            return self._event(db, run_id, kind, data)

    def evidence(self, run_id, items, *, replace=False):
        with self.connection() as db:
            if self._run(db, run_id)["status"] in TERMINAL:
                return None
            if replace:
                db.execute("DELETE FROM run_evidence WHERE run_id=?", (run_id,))
            for item in items:
                db.execute("INSERT INTO run_evidence VALUES(?,?,?) ON CONFLICT(run_id,label) DO UPDATE SET data=excluded.data",
                           (run_id, item["label"], encode(item)))
            return self._event(db, run_id, "evidence.updated", {"items": items, "replace": replace})

    def _joined(self, db, run_id):
        return "\n\n".join(json.loads(r[0])["markdown"] for r in db.execute("SELECT data FROM run_sections WHERE run_id=? ORDER BY position", (run_id,)))

    def section(self, run_id, data):
        with self.connection() as db:
            if self._run(db, run_id)["status"] in TERMINAL:
                return None
            old = db.execute("SELECT position,data FROM run_sections WHERE run_id=? AND section_id=?", (run_id, data["section_id"])).fetchone()
            comparable = {k: v for k, v in data.items() if k != "position"}
            if old and {k: v for k, v in json.loads(old["data"]).items() if k != "position"} == comparable:
                return None
            position = old["position"] if old else db.execute("SELECT COUNT(*) FROM run_sections WHERE run_id=?", (run_id,)).fetchone()[0]
            data = dict(data, position=position)
            db.execute("INSERT INTO run_sections VALUES(?,?,?,?) ON CONFLICT(run_id,section_id) DO UPDATE SET data=excluded.data",
                       (run_id, data["section_id"], position, encode(data)))
            answer = self._joined(db, run_id)
            db.execute("UPDATE runs SET answer=? WHERE id=?", (answer, run_id))
            db.execute("UPDATE messages SET content=? WHERE run_id=? AND role='assistant'", (answer, run_id))
            return self._event(db, run_id, "answer.section_upsert", data)

    def finish(self, run_id, status, reason=None, error=None, summary=None, final_answer=None):
        if status not in TERMINAL:
            raise ValueError("Invalid terminal status")
        with self.connection() as db:
            row = self._run(db, run_id)
            if row["status"] in TERMINAL:
                return None
            answer = self._joined(db, run_id) if final_answer is None else final_answer
            db.execute("UPDATE runs SET status=?,answer=?,reason=?,error=?,summary=? WHERE id=?",
                       (status, answer, reason, encode(error) if error else None, encode(summary or {}), run_id))
            db.execute("UPDATE messages SET content=? WHERE run_id=? AND role='assistant'", (answer, run_id))
            db.execute("UPDATE sessions SET updated_at=? WHERE id=?", (now(), row["session_id"]))
            if summary is not None:
                self._event(db, run_id, "run.summary", summary)
            return self._event(db, run_id, "run.finished", {"status": status, "reason": reason, "error": error, "answer": answer})

    def release_worker(self, run_id):
        with self.connection() as db:
            db.execute("UPDATE runs SET worker_active=0 WHERE id=?", (run_id,))

    def recover(self):
        with self.connection() as db:
            ids = [r[0] for r in db.execute("SELECT id FROM runs WHERE status IN ('queued','running')")]
            for run_id in ids:
                answer = self._joined(db, run_id)
                self.finish(run_id, "partial" if answer else "failed", "service_interrupted",
                            {"code": "service_interrupted", "message": "服务中断，已保留此前验证的内容。"})
            db.execute("UPDATE runs SET worker_active=0")
