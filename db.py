import sqlite3
import os
from datetime import datetime, timezone

DB_PATH = os.getenv("DB_PATH", os.path.join(os.path.dirname(os.path.abspath(__file__)), "trades.db"))

def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS trades (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp   TEXT    NOT NULL,
            symbol      TEXT    NOT NULL DEFAULT 'BTCUSDT',
            side        TEXT    NOT NULL,
            entry_price REAL    NOT NULL,
            exit_price  REAL    NOT NULL,
            quantity    REAL    NOT NULL,
            pnl         REAL    NOT NULL,
            reason      TEXT    NOT NULL
        )
    """)
    # Ha mar letezik a tabla de nincs symbol oszlop, adjuk hozza
    try:
        conn.execute("ALTER TABLE trades ADD COLUMN symbol TEXT NOT NULL DEFAULT 'BTCUSDT'")
        conn.commit()
    except Exception:
        pass
    conn.commit()
    conn.close()

def log_trade(side, entry_price, exit_price, quantity, pnl, reason, symbol='BTCUSDT'):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        INSERT INTO trades (timestamp, symbol, side, entry_price, exit_price, quantity, pnl, reason)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (datetime.now(timezone.utc).isoformat(), symbol, side, entry_price, exit_price, quantity, pnl, reason))
    conn.commit()
    conn.close()

def get_trades(limit=100):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.execute("SELECT * FROM trades ORDER BY id DESC LIMIT ?", (limit,))
    rows = [dict(row) for row in cur.fetchall()]
    conn.close()
    return rows

def get_stats():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    total = conn.execute("SELECT COUNT(*) as cnt, SUM(pnl) as pnl FROM trades").fetchone()
    wins  = conn.execute("SELECT COUNT(*) as cnt FROM trades WHERE pnl > 0").fetchone()
    daily = conn.execute("SELECT SUM(pnl) as pnl FROM trades WHERE timestamp LIKE ?", (today+'%',)).fetchone()
    conn.close()
    total_cnt = total['cnt'] or 0
    total_pnl = round(total['pnl'] or 0, 2)
    win_cnt   = wins['cnt'] or 0
    win_rate  = round(win_cnt / total_cnt * 100, 1) if total_cnt > 0 else 0
    daily_pnl = round(daily['pnl'] or 0, 2)
    return {
        'total_trades': total_cnt,
        'total_pnl':    total_pnl,
        'win_rate':     win_rate,
        'daily_pnl':    daily_pnl
    }
