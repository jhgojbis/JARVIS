"""Tiny SQLite store: call log, messages for the owner, tasks, scheduler state, token usage, chat history."""
from __future__ import annotations
import json, sqlite3, time
from datetime import date


class Store:
    def __init__(self, path: str = "jarvis.db"):
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS calls(id INTEGER PRIMARY KEY, ts REAL, number TEXT, name TEXT, reason TEXT, outcome TEXT);
        CREATE TABLE IF NOT EXISTS tasks(id INTEGER PRIMARY KEY, ts REAL, text TEXT, due TEXT, done INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, v TEXT);
        CREATE TABLE IF NOT EXISTS usage(day TEXT PRIMARY KEY, tokens INTEGER);
        CREATE TABLE IF NOT EXISTS history(id INTEGER PRIMARY KEY, ts REAL, channel TEXT, role TEXT, text TEXT);
        """)

    # --- kv
    def get(self, k, default=None):
        r = self.db.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
        return json.loads(r["v"]) if r else default

    def set(self, k, v):
        self.db.execute("INSERT OR REPLACE INTO kv VALUES(?,?)", (k, json.dumps(v)))
        self.db.commit()

    # --- calls
    def log_call(self, number, name, reason, outcome):
        self.db.execute("INSERT INTO calls(ts,number,name,reason,outcome) VALUES(?,?,?,?,?)",
                        (time.time(), number, name, reason, outcome))
        self.db.commit()

    def calls_since(self, ts):
        return [dict(r) for r in self.db.execute("SELECT * FROM calls WHERE ts>? ORDER BY ts", (ts,))]

    # --- tasks
    def add_task(self, text, due=""):
        c = self.db.execute("INSERT INTO tasks(ts,text,due) VALUES(?,?,?)", (time.time(), text, due))
        self.db.commit()
        return c.lastrowid

    def open_tasks(self):
        return [dict(r) for r in self.db.execute("SELECT id,text,due FROM tasks WHERE done=0 ORDER BY id")]

    def done_task(self, task_id):
        self.db.execute("UPDATE tasks SET done=1 WHERE id=?", (task_id,))
        self.db.commit()

    # --- token budget
    def add_tokens(self, n):
        d = date.today().isoformat()
        self.db.execute("INSERT INTO usage VALUES(?,?) ON CONFLICT(day) DO UPDATE SET tokens=tokens+?", (d, n, n))
        self.db.commit()

    def tokens_today(self):
        r = self.db.execute("SELECT tokens FROM usage WHERE day=?", (date.today().isoformat(),)).fetchone()
        return r["tokens"] if r else 0

    # --- chat history (last few turns only, to keep prompts small)
    def add_history(self, channel, role, text):
        self.db.execute("INSERT INTO history(ts,channel,role,text) VALUES(?,?,?,?)", (time.time(), channel, role, text))
        self.db.commit()

    def recent_history(self, channel, n=6):
        rows = self.db.execute("SELECT role,text FROM history WHERE channel=? ORDER BY id DESC LIMIT ?", (channel, n)).fetchall()
        return [{"role": r["role"], "content": r["text"]} for r in reversed(rows)]
