"""The shared notebook: every fact and finding is saved with a date and its source."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .models import Fact, Finding

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    client TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL DEFAULT 'running',
    config TEXT NOT NULL DEFAULT '{}',
    score INTEGER,
    scoring INTEGER,
    key TEXT
);
CREATE TABLE IF NOT EXISTS facts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES runs(id),
    client TEXT NOT NULL,
    collector TEXT NOT NULL,
    kind TEXT NOT NULL,
    subject TEXT NOT NULL,
    data TEXT NOT NULL,
    collected_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS findings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES runs(id),
    client TEXT NOT NULL,
    analyzer TEXT NOT NULL,
    key TEXT NOT NULL,
    title TEXT NOT NULL,
    detail TEXT NOT NULL,
    how_to_fix TEXT NOT NULL,
    evidence TEXT NOT NULL,
    impact INTEGER NOT NULL,
    effort INTEGER NOT NULL,
    score INTEGER NOT NULL,
    owner TEXT NOT NULL DEFAULT '',
    tech TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    affected INTEGER NOT NULL DEFAULT 0,
    total INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS failures (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES runs(id),
    part TEXT NOT NULL,
    error TEXT NOT NULL
);
"""
# Columns added after the first notebooks were written: (table, column, definition).
ADDED = [
    ("runs", "score", "INTEGER"),        # the score the report showed, saved when the run ended
    ("runs", "scoring", "INTEGER"),      # which version of the formula gave that score
    ("runs", "key", "TEXT"),             # the owner's secret: whoever has it may delete the run
    ("findings", "affected", "INTEGER NOT NULL DEFAULT 0"),
    ("findings", "total", "INTEGER NOT NULL DEFAULT 0"),
]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, path: str | Path = "bvm.sqlite3"):
        self.path = str(path)
        self.db = sqlite3.connect(self.path, timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        for table, column, definition in ADDED:      # bring an older notebook up to date
            if column not in {row["name"] for row in self.db.execute(f"PRAGMA table_info({table})")}:
                self.db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
        self.db.commit()

    def start_run(self, client: str, config: dict | None = None, key: str | None = None) -> int:
        cur = self.db.execute(
            "INSERT INTO runs (client, started_at, config, key) VALUES (?, ?, ?, ?)",
            (client, _now(), json.dumps(config or {}, ensure_ascii=False), key),
        )
        self.db.commit()
        return cur.lastrowid

    def finish_run(self, run_id: int, status: str = "done", score: int | None = None,
                   scoring: int | None = None) -> None:
        self.db.execute(
            "UPDATE runs SET finished_at = ?, status = ?, score = ?, scoring = ? WHERE id = ?",
            (_now(), status, score, scoring, run_id))
        self.db.commit()

    def previous_run(self, client: str, before: int, scoring: int | None = None) -> dict | None:
        """The last finished check of the same business that has a score to compare with."""
        row = self.db.execute(
            "SELECT id, started_at, score FROM runs WHERE client = ? AND id < ? AND status = 'done'"
            " AND score IS NOT NULL AND (? IS NULL OR scoring = ?) ORDER BY id DESC LIMIT 1",
            (client, before, scoring, scoring)).fetchone()
        return dict(row) if row else None

    def delete_run(self, run_id: int) -> bool:
        """Removes a check and everything collected for it. False when there is no such check."""
        for table in ("facts", "findings", "failures"):
            self.db.execute(f"DELETE FROM {table} WHERE run_id = ?", (run_id,))
        removed = self.db.execute("DELETE FROM runs WHERE id = ?", (run_id,)).rowcount
        self.db.commit()
        return bool(removed)

    def get_run(self, run_id: int) -> dict | None:
        row = self.db.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        if row is None:
            return None
        run = dict(row)
        run["config"] = json.loads(run["config"])
        return run

    def list_runs(self, limit: int = 20) -> list[dict]:
        rows = self.db.execute(
            "SELECT r.id, r.client, r.started_at, r.status, r.config,"
            " (SELECT COUNT(*) FROM findings f WHERE f.run_id = r.id) AS findings"
            " FROM runs r ORDER BY r.id DESC LIMIT ?", (limit,))
        runs = []
        for row in rows:
            run = dict(row)
            run["config"] = json.loads(run["config"])
            runs.append(run)
        return runs

    def load_findings(self, run_id: int) -> list[Finding]:
        rows = self.db.execute(
            "SELECT analyzer, key, title, detail, how_to_fix, evidence, impact, effort, owner, tech,"
            " affected, total FROM findings WHERE run_id = ? ORDER BY id", (run_id,))
        return [Finding(r["analyzer"], r["key"], r["title"], r["detail"], r["how_to_fix"],
                        json.loads(r["evidence"]), r["impact"], r["effort"], r["owner"], r["tech"],
                        r["affected"], r["total"])
                for r in rows]

    def save_facts(self, run_id: int, client: str, facts: list[Fact]) -> None:
        now = _now()
        self.db.executemany(
            "INSERT INTO facts (run_id, client, collector, kind, subject, data, collected_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                (run_id, client, f.collector, f.kind, f.subject,
                 json.dumps(f.data, ensure_ascii=False), now)
                for f in facts
            ],
        )
        self.db.commit()

    def load_facts(self, run_id: int) -> list[Fact]:
        rows = self.db.execute(
            "SELECT collector, kind, subject, data FROM facts WHERE run_id = ? ORDER BY id",
            (run_id,),
        )
        return [Fact(r["collector"], r["kind"], r["subject"], json.loads(r["data"])) for r in rows]

    def save_findings(self, run_id: int, client: str, findings: list[Finding]) -> None:
        now = _now()
        self.db.executemany(
            "INSERT INTO findings (run_id, client, analyzer, key, title, detail, how_to_fix,"
            " evidence, impact, effort, score, owner, tech, created_at, affected, total)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                (run_id, client, f.analyzer, f.key, f.title, f.detail, f.how_to_fix,
                 json.dumps(f.evidence, ensure_ascii=False), f.impact, f.effort, f.score, f.owner, f.tech, now,
                 f.affected, f.of)
                for f in findings
            ],
        )
        self.db.commit()

    def save_failure(self, run_id: int, part: str, error: str) -> None:
        self.db.execute(
            "INSERT INTO failures (run_id, part, error) VALUES (?, ?, ?)", (run_id, part, error)
        )
        self.db.commit()

    def load_failures(self, run_id: int) -> list[tuple[str, str]]:
        rows = self.db.execute(
            "SELECT part, error FROM failures WHERE run_id = ? ORDER BY id", (run_id,)
        )
        return [(r["part"], r["error"]) for r in rows]

    def close(self) -> None:
        self.db.close()
