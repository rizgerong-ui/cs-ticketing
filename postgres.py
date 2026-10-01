"""PostgreSQL connection adapter for the app's small, shared SQL vocabulary."""
import re

LOCK_ID = 763524091

class Row(dict):
    def __getitem__(self, key):
        return list(self.values())[key] if isinstance(key, int) else super().__getitem__(key)

def row_factory(cursor):
    names = [c.name for c in (cursor.description or [])]
    return lambda values: Row(zip(names, values))

def sql_for_postgres(sql):
    sql = sql.strip()
    if sql == 'BEGIN IMMEDIATE':
        return f'SELECT pg_advisory_xact_lock({LOCK_ID})'
    sql = re.sub(r'\bevent_id INTEGER PRIMARY KEY\b', 'event_id BIGSERIAL PRIMARY KEY', sql)
    if 'CREATE TABLE IF NOT EXISTS validation_audit' in sql:
        sql = sql.replace('id INTEGER PRIMARY KEY', 'id BIGSERIAL PRIMARY KEY')
    sql = sql.replace('ORDER BY rowid', 'ORDER BY insertion_order')
    if 'CREATE TABLE IF NOT EXISTS tickets(' in sql:
        sql = sql.replace('tickets(', 'tickets(insertion_order BIGSERIAL UNIQUE, ', 1)
    # Parameters in this app use qmark style; SQL literals never contain question marks.
    return sql.replace('?', '%s')

class Connection:
    def __init__(self, url):
        import psycopg
        self.raw = psycopg.connect(url, row_factory=row_factory, connect_timeout=15)
    def __enter__(self):
        return self
    def __exit__(self, kind, value, tb):
        try:
            self.raw.rollback() if kind else self.raw.commit()
        finally:
            self.raw.close()
    def execute(self, sql, params=()):
        return self.raw.execute(sql_for_postgres(sql), params)
    def executescript(self, sql):
        for statement in sql.split(';'):
            if statement.strip() and not statement.strip().startswith('PRAGMA'):
                self.execute(statement)
    def close(self):
        self.raw.close()
