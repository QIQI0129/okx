import sqlite3
import json
import time
from typing import Any, Optional, Dict

class SQLiteStore:
    def __init__(self, path: str):
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL;")
        self._init_schema()

    def _init_schema(self):
        self.conn.execute("""
        CREATE TABLE IF NOT EXISTS kv (
            k TEXT PRIMARY KEY,
            v TEXT NOT NULL,
            updated_at REAL NOT NULL
        );
        """)
        self.conn.execute("""
        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL NOT NULL,
            inst_id TEXT NOT NULL,
            cl_ord_id TEXT,
            ord_id TEXT,
            side TEXT,
            pos_side TEXT,
            sz TEXT,
            tp_trigger TEXT,
            sl_trigger TEXT,
            raw_json TEXT NOT NULL,
            note TEXT
        );
        """)
        self.conn.commit()

    def set_kv(self, k: str, v: str):
        now = time.time()
        self.conn.execute(
            "INSERT INTO kv(k,v,updated_at) VALUES(?,?,?) "
            "ON CONFLICT(k) DO UPDATE SET v=excluded.v, updated_at=excluded.updated_at",
            (k, v, now),
        )
        self.conn.commit()

    def del_kv(self, k: str):
        self.conn.execute("DELETE FROM kv WHERE k=?", (k,))
        self.conn.commit()

    def get_kv(self, k: str) -> Optional[str]:
        cur = self.conn.execute("SELECT v FROM kv WHERE k=?", (k,))
        row = cur.fetchone()
        return row[0] if row else None

    def get_kv_float(self, k: str) -> Optional[float]:
        v = self.get_kv(k)
        if v is None:
            return None
        try:
            return float(v)
        except:
            return None

    def save_order(
        self,
        inst_id: str,
        side: str,
        pos_side: str,
        sz: str,
        tp_trigger: str,
        sl_trigger: str,
        resp_json: Dict[str, Any],
        note: str = "",
        cl_ord_id: str = "",
    ):
        ord_id = ""
        cl_from_resp = ""
        try:
            data0 = (resp_json.get("data") or [{}])[0]
            ord_id = data0.get("ordId", "") or ""
            cl_from_resp = data0.get("clOrdId", "") or ""
        except:
            pass

        raw = json.dumps(resp_json, ensure_ascii=False)
        cl_final = cl_ord_id or cl_from_resp

        self.conn.execute(
            "INSERT INTO orders(ts,inst_id,cl_ord_id,ord_id,side,pos_side,sz,tp_trigger,sl_trigger,raw_json,note) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (time.time(), inst_id, cl_final, ord_id, side, pos_side, sz, tp_trigger, sl_trigger, raw, note),
        )
        self.conn.commit()
