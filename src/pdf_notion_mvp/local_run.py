"""SQLite checkpoints for bounded local page work; no provider or publisher access."""
import json
import os
import sqlite3
from pathlib import Path

from .study_bundle import encode,sha,private_destination

IDENTITY='pdf-notion-local-pages-v1'


class LocalRunStore:
    def __init__(self,path):
        self.path=Path(path)
        private_destination(self.path.parent,[])
        if (self.path.suffix!='.sqlite' or self.path.is_symlink() or
                self.path.exists() and (not self.path.is_file() or self.path.stat().st_nlink!=1)):
            raise ValueError('private single-link SQLite checkpoint required')
        self.path.parent.mkdir(parents=True,exist_ok=True)
        new=False
        try:
            fd=os.open(self.path,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600);os.close(fd);new=True
        except FileExistsError:pass
        db=sqlite3.connect(self.path,timeout=10)
        try:
            db.execute('BEGIN IMMEDIATE')
            if new:
                db.execute('CREATE TABLE identity(value TEXT PRIMARY KEY)');db.execute('INSERT INTO identity VALUES(?)',(IDENTITY,))
                db.execute('CREATE TABLE pages(run_key TEXT,page INTEGER,status TEXT,payload TEXT,digest TEXT,error_code TEXT,PRIMARY KEY(run_key,page))')
            if db.execute('SELECT value FROM identity').fetchall()!=[(IDENTITY,)]:raise ValueError('unknown checkpoint database')
            db.commit()
        except (sqlite3.Error,ValueError):
            db.rollback();raise ValueError('unknown checkpoint database') from None
        finally:db.close()
        self.file_identity=(self.path.stat().st_dev,self.path.stat().st_ino)

    def validate_path(self):
        private_destination(self.path.parent,[])
        if (self.path.is_symlink() or not self.path.is_file() or self.path.stat().st_nlink!=1 or
                (self.path.stat().st_dev,self.path.stat().st_ino)!=self.file_identity):
            raise ValueError('checkpoint file changed')

    def process(self,run_key,pages,processor):
        if (not isinstance(run_key,str) or len(run_key)!=64 or any(c not in '0123456789abcdef' for c in run_key) or
                not pages or any(type(p) is not int or p<1 for p in pages) or len(set(pages))!=len(pages)):
            raise ValueError('source/config fingerprint and unique positive pages required')
        results=[]
        # A transaction per page preserves successful pages across crashes/restarts.
        for page in pages:
            self.validate_path()
            db=sqlite3.connect(self.path.absolute().as_uri()+'?mode=rw',uri=True,timeout=10)
            try:
                db.execute('BEGIN IMMEDIATE')
                self.validate_path()
                if db.execute('SELECT value FROM identity').fetchall()!=[(IDENTITY,)]:
                    raise ValueError('checkpoint database identity changed')
                row=db.execute('SELECT status,payload,digest FROM pages WHERE run_key=? AND page=?',(run_key,page)).fetchone()
                if row and row[0]=='completed':
                    payload=json.loads(row[1])
                    if sha(encode(payload))!=row[2]:raise ValueError('saved page checkpoint changed')
                    db.commit();results.append(payload);continue
                try:
                    payload=processor(page)
                    json.dumps(payload,allow_nan=False)
                    data=encode(payload)
                    payload=json.loads(data)  # Freeze the exact JSON snapshot used by restart.
                    if len(data)>500000:raise ValueError('page payload limit exceeded')
                    self.validate_path()
                except Exception:
                    self.validate_path()
                    db.execute('INSERT OR REPLACE INTO pages VALUES(?,?,?,?,?,?)',(run_key,page,'failed',None,None,'local_processor_error'))
                    db.commit();raise RuntimeError('local page failed; prior successes preserved') from None
                db.execute('INSERT OR REPLACE INTO pages VALUES(?,?,?,?,?,?)',(run_key,page,'completed',data.decode(),sha(data),None))
                db.commit();results.append(payload)
            except BaseException:
                if db.in_transaction:db.rollback()
                raise
            finally:db.close()
        return results
