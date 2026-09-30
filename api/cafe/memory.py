"""少量固定玩家的 SQLite 记忆；每个显式修正立即提交。"""

import json
from pathlib import Path
import sqlite3
from uuid import uuid4


class MemoryStore:
    def __init__(self, path: str, character_id: str = "owner_01"):
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.character_id = character_id
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS player_profiles (
            character_id TEXT, player_id TEXT, display_name TEXT,
            preferences TEXT NOT NULL DEFAULT '{}', visit_count INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY(character_id, player_id)
        );
        CREATE TABLE IF NOT EXISTS topic_progress (
            character_id TEXT, player_id TEXT, topic_id TEXT, stage TEXT,
            summary TEXT, next_step TEXT, status TEXT,
            PRIMARY KEY(character_id, player_id, topic_id)
        );
        CREATE TABLE IF NOT EXISTS commitments (
            id TEXT PRIMARY KEY, character_id TEXT, player_id TEXT, kind TEXT,
            topic_id TEXT, trigger TEXT, created_visit_id TEXT,
            last_offered_visit_id TEXT, status TEXT
        );
        CREATE TABLE IF NOT EXISTS episodes (
            id TEXT PRIMARY KEY, character_id TEXT, player_id TEXT, topic_id TEXT,
            summary TEXT, resume_hint TEXT, visit_id TEXT, source TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        """)
        self.db.commit()

    def ensure_player(self, player_id: str, display_name: str | None = None) -> None:
        with self.db:
            self.db.execute(
                "INSERT OR IGNORE INTO player_profiles(character_id,player_id,display_name) VALUES(?,?,?)",
                (self.character_id, player_id, display_name or player_id),
            )
            if display_name:
                self.db.execute("UPDATE player_profiles SET display_name=? WHERE character_id=? AND player_id=?",
                                (display_name, self.character_id, player_id))

    def visit(self, player_id: str, display_name: str) -> dict:
        self.ensure_player(player_id, display_name)
        with self.db:
            self.db.execute("UPDATE player_profiles SET visit_count=visit_count+1 WHERE character_id=? AND player_id=?",
                            (self.character_id, player_id))
        return self.view(player_id)

    def view(self, player_id: str) -> dict:
        self.ensure_player(player_id)
        pair = (self.character_id, player_id)
        profile = dict(self.db.execute("SELECT * FROM player_profiles WHERE character_id=? AND player_id=?", pair).fetchone())
        profile["preferences"] = json.loads(profile["preferences"])
        return {
            "type": "memory_view", "character_id": self.character_id, "player_id": player_id,
            "profile": profile,
            "topics": [dict(row) for row in self.db.execute("SELECT * FROM topic_progress WHERE character_id=? AND player_id=?", pair)],
            "commitments": [dict(row) for row in self.db.execute("SELECT * FROM commitments WHERE character_id=? AND player_id=?", pair)],
            "episodes": [dict(row) for row in self.db.execute("SELECT * FROM episodes WHERE character_id=? AND player_id=? ORDER BY rowid DESC LIMIT 5", pair)],
        }

    def preference(self, player_id: str, key: str, value: str) -> None:
        prefs = self.view(player_id)["profile"]["preferences"]
        prefs[key] = value
        with self.db:
            self.db.execute("UPDATE player_profiles SET preferences=? WHERE character_id=? AND player_id=?",
                            (json.dumps(prefs, ensure_ascii=False), self.character_id, player_id))

    def save_topic(self, player_id: str, topic_id: str, stage: str, summary: str, next_step: str, status: str) -> None:
        with self.db:
            self.db.execute("""INSERT INTO topic_progress VALUES(?,?,?,?,?,?,?)
                ON CONFLICT(character_id,player_id,topic_id) DO UPDATE SET
                stage=excluded.stage,summary=excluded.summary,next_step=excluded.next_step,status=excluded.status""",
                (self.character_id, player_id, topic_id, stage, summary, next_step, status))

    def promise(self, player_id: str, topic_id: str, visit_id: str) -> None:
        with self.db:
            self.db.execute("DELETE FROM commitments WHERE character_id=? AND player_id=? AND topic_id=?",
                            (self.character_id, player_id, topic_id))
            self.db.execute("INSERT INTO commitments VALUES(?,?,?,?,?,?,?,?,?)",
                            ("promise_" + uuid4().hex[:10], self.character_id, player_id,
                             "offer_topic_resume", topic_id, "on_next_visit", visit_id, "", "pending"))

    def offered(self, commitment_id: str, visit_id: str) -> None:
        with self.db:
            self.db.execute("UPDATE commitments SET last_offered_visit_id=? WHERE id=? AND character_id=?",
                            (visit_id, commitment_id, self.character_id))

    def close_promises(self, player_id: str, topic_id: str, status: str = "completed") -> None:
        with self.db:
            self.db.execute("UPDATE commitments SET status=? WHERE character_id=? AND player_id=? AND topic_id=?",
                            (status, self.character_id, player_id, topic_id))

    def episode(self, player_id: str, topic_id: str, summary: str, visit_id: str, source: str, resume_hint: str = "") -> None:
        with self.db:
            self.db.execute("INSERT INTO episodes(id,character_id,player_id,topic_id,summary,resume_hint,visit_id,source) VALUES(?,?,?,?,?,?,?,?)",
                            ("ep_" + uuid4().hex[:10], self.character_id, player_id, topic_id, summary, resume_hint, visit_id, source))

    def forget_topic(self, player_id: str, topic_id: str) -> None:
        with self.db:
            for table in ("episodes", "topic_progress", "commitments"):
                self.db.execute(f"DELETE FROM {table} WHERE character_id=? AND player_id=? AND topic_id=?",
                                (self.character_id, player_id, topic_id))

    def close(self) -> None:
        self.db.close()
