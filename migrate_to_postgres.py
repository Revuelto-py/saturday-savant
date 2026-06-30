import sqlite3
import os
import time
import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

load_dotenv()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATABASE_URL = os.getenv('DATABASE_URL')

TYPE_MAP = {
    'INTEGER': 'INTEGER',
    'TEXT':    'TEXT',
    'REAL':    'DOUBLE PRECISION',
    'BLOB':    'BYTEA',
    'NUMERIC': 'NUMERIC',
    '':        'TEXT',
}

def pg_connect():
    for attempt in range(5):
        try:
            return psycopg2.connect(
                DATABASE_URL,
                keepalives=1,
                keepalives_idle=30,
                keepalives_interval=10,
                keepalives_count=5,
                connect_timeout=30,
            )
        except Exception as e:
            print(f"  Connect attempt {attempt+1} failed: {e}", flush=True)
            time.sleep(3)
    raise RuntimeError("Could not connect to Postgres after 5 attempts")

sqlite_conn = sqlite3.connect(os.path.join(BASE_DIR, 'cfb_data.db'))
sqlite_conn.row_factory = sqlite3.Row
sqlite_cursor = sqlite_conn.cursor()

sqlite_cursor.execute(
    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
)
tables = [row[0] for row in sqlite_cursor.fetchall()]
print(f"Found {len(tables)} tables: {tables}\n", flush=True)

for table in tables:
    print(f"--- Migrating {table} ---", flush=True)

    sqlite_cursor.execute(f'PRAGMA table_info("{table}")')
    columns_info = sqlite_cursor.fetchall()

    col_defs = []
    pk_col = None
    for col in columns_info:
        col_name = col['name']
        raw_type = col['type'].upper().split('(')[0].strip()
        col_type = TYPE_MAP.get(raw_type, 'TEXT')
        if col['pk'] == 1 and col_type == 'INTEGER':
            col_defs.append(f'"{col_name}" INTEGER PRIMARY KEY')
            pk_col = col_name
        else:
            col_defs.append(f'"{col_name}" {col_type}')

    pg_conn = pg_connect()
    pg_cursor = pg_conn.cursor()

    try:
        pg_cursor.execute(f'DROP TABLE IF EXISTS "{table}"')
        pg_conn.commit()
        pg_cursor.execute(f'CREATE TABLE "{table}" ({", ".join(col_defs)})')
        pg_conn.commit()
        print(f"  Created table (pk_col={pk_col})", flush=True)
    except Exception as e:
        try: pg_conn.rollback()
        except: pass
        print(f"  Error setting up {table}: {e}", flush=True)
        pg_conn.close()
        continue

    sqlite_cursor.execute(f'SELECT * FROM "{table}"')
    rows = sqlite_cursor.fetchall()

    if not rows:
        print(f"  No data — skipping", flush=True)
        pg_conn.close()
        continue

    col_names = [col['name'] for col in columns_info]
    quoted_cols = ', '.join(f'"{c}"' for c in col_names)
    insert_sql = f'INSERT INTO "{table}" ({quoted_cols}) VALUES %s'

    batch_size = 100
    inserted = 0
    i = 0
    while i < len(rows):
        batch = [tuple(row) for row in rows[i:i + batch_size]]

        if pg_conn.closed:
            print(f"  Reconnecting at row {i}...", flush=True)
            pg_conn = pg_connect()
            pg_cursor = pg_conn.cursor()

        try:
            psycopg2.extras.execute_values(pg_cursor, insert_sql, batch)
            pg_conn.commit()
            inserted += len(batch)
            i += batch_size
            if inserted % 10000 == 0:
                print(f"  ... {inserted}/{len(rows)} rows", flush=True)
        except psycopg2.OperationalError as e:
            # Connection dropped — reconnect and retry this batch
            print(f"  Connection lost at row {i}, reconnecting: {e}", flush=True)
            try: pg_conn.close()
            except: pass
            time.sleep(2)
            pg_conn = pg_connect()
            pg_cursor = pg_conn.cursor()
            # don't advance i — retry the same batch
        except Exception as e:
            try: pg_conn.rollback()
            except: pass
            print(f"  Error in batch at row {i}: {e}", flush=True)
            i += batch_size  # skip bad batch and continue

    print(f"  Inserted {inserted}/{len(rows)} rows", flush=True)
    pg_conn.close()

sqlite_conn.close()
print("\nMigration complete", flush=True)
