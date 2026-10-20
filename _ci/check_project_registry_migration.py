"""Verify additive migration on a consistent backup, never update the real DB."""
from datetime import datetime
import hashlib
import json
from pathlib import Path
import sqlite3
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'backend'))
from app import db


def facts(conn, tables):
    return {table:dict(rows=conn.execute('SELECT COUNT(*) FROM "'+table+'"').fetchone()[0],
                       sha256=hashlib.sha256(json.dumps(conn.execute('SELECT * FROM "'+table+'" ORDER BY rowid').fetchall(),ensure_ascii=False,default=str).encode()).hexdigest()) for table in tables}


def main():
    output=ROOT/'_ci_out/project-registry-migration';output.mkdir(exist_ok=True)
    target=output/('migration-copy-'+datetime.now().strftime('%Y%m%d%H%M%S%f')+'.sqlite')
    with sqlite3.connect((ROOT/'data/db.sqlite').as_uri()+'?mode=ro',uri=True) as src, sqlite3.connect(target) as dst:
        src.backup(dst)
    with sqlite3.connect(target) as c:
        tables=[r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
        before=facts(c,tables)
    db.init_db(str(target))
    with sqlite3.connect(target) as c:after=facts(c,tables)
    assert before==after,'Existing facts were modified by migration'
    result=dict(source_read_only=True,copy=str(target),existing_tables_unchanged=True,tables=before)
    (output/'verification.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(dict(existing_tables_unchanged=True,counts={k:v['rows'] for k,v in before.items()}),ensure_ascii=False))


if __name__=='__main__':main()
