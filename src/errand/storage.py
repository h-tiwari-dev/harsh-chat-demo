from __future__ import annotations
import json, sqlite3


class Storage:
    """Durable KV (SQLite). Namespaced keys; JSON values. Survives process restart."""
    def __init__(self, path: str = ":memory:"):
        self.path = path
        self._db = sqlite3.connect(path)
        self._db.execute("CREATE TABLE IF NOT EXISTS kv (ns TEXT, k TEXT, v TEXT, PRIMARY KEY(ns,k))")
        self._db.commit()
    def kv_get(self, ns: str, k: str, default=None):
        r = self._db.execute("SELECT v FROM kv WHERE ns=? AND k=?", (ns, k)).fetchone()
        return json.loads(r[0]) if r else default
    def kv_set(self, ns: str, k: str, v) -> None:
        self._db.execute("INSERT OR REPLACE INTO kv VALUES (?,?,?)", (ns, k, json.dumps(v, sort_keys=True)))
        self._db.commit()
    def kv_delete(self, ns: str, k: str) -> None:
        self._db.execute("DELETE FROM kv WHERE ns=? AND k=?", (ns, k)); self._db.commit()
    def kv_items(self, ns: str) -> dict:
        return {k: json.loads(v) for k, v in self._db.execute("SELECT k,v FROM kv WHERE ns=?", (ns,))}
    def close(self): self._db.close()
