"""Explicit, identical-request free retries for observed HTTP 503 only.

Each root request gets at most two durable child attempts. This never resets a
consumed request, automatically retries, switches projects, or accepts uncertain
timeouts, quota errors or successful-but-invalid content as retryable evidence.
"""
import sqlite3
import re
from datetime import datetime,timedelta,timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

from .free_execution import regular
from .live_run import fingerprint
from .rag_files import read_json_input


def retry_key(base,index):
    return fingerprint([base,'explicit_free_http503_retry_v1',index])


def observation(response,now):
    minimum=now+timedelta(seconds=120)
    headers=response.headers.get_list('retry-after')
    header=headers[0] if len(headers)==1 else None
    packet=dict(observed_at=now.isoformat(),retry_not_before=minimum.isoformat(),
                retry_after_present=header is not None,retry_after_invalid=False)
    if not headers:return packet
    packet['retry_after_present']=True
    if len(headers)!=1:
        packet['retry_after_invalid']=True
        return packet
    try:
        value=header.strip()
        if value.isascii() and value.isdecimal():target=now+timedelta(seconds=int(value))
        else:
            if not re.fullmatch(r'(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun), [0-9]{2} (?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec) [0-9]{4} [0-9]{2}:[0-9]{2}:[0-9]{2} GMT',value):
                raise ValueError('complete HTTP date required')
            target=parsedate_to_datetime(value)
            if target.tzinfo is None:raise ValueError('aware retry date required')
        packet['retry_not_before']=max(minimum,target).isoformat()
    except (ValueError,TypeError,OverflowError):packet['retry_after_invalid']=True
    return packet


def validate_retry(spec,base,now):
    if spec.http503_retry_of is None:return
    root=Path(spec.http503_retry_of);regular(root)
    if spec.billing_mode!='verified_free':raise ValueError('HTTP503 retries require the same verified free policy')
    ledger=Path(spec.free_ledger);regular(ledger)
    db=sqlite3.connect(ledger.as_uri()+'?mode=ro',uri=True)
    try:
        if db.execute('SELECT value FROM ledger_identity').fetchall()!=[('pdf-notion-verified-free-v1',)]:
            raise ValueError('HTTP503 retry ledger identity mismatch')
        def failed_receipt(path,operation):
            regular(path);packet=read_json_input(path,max_bytes=100000)
            row=db.execute('SELECT cost,status,result_path,result_sha,request_count FROM reservations WHERE operation=?',(operation,)).fetchone()
            diagnostic=packet.get('diagnostic',{})
            if 'execution_ledger' in packet:
                stat=ledger.stat()
                if (packet['execution_ledger'],packet.get('ledger_device'),packet.get('ledger_inode')) != (str(ledger.resolve()),stat.st_dev,stat.st_ino):
                    raise ValueError('HTTP503 retry must preserve original ledger identity and path')
            else:
                raise ValueError('historical HTTP503 receipt has no original ledger identity; retry blocked')
            if (row!=(0,'failed',str(path.resolve()),fingerprint(packet),1)
                    or packet.get('status')!='observed_safe_error_diagnostic'
                    or packet.get('operation_key')!=operation or packet.get('request_count')!=1
                    or packet.get('context_digest')!=spec.context_digest
                    or packet.get('request_digest')!=spec.request_digest
                    or diagnostic.get('http_status')!=503 or diagnostic.get('provider_status')!='UNAVAILABLE'):
                raise ValueError('only identical observed HTTP503 failed requests may retry')
            if packet.get('retry_after_invalid'):raise ValueError('Retry-After could not be verified')
            if 'retry_not_before' in packet:
                earliest=datetime.fromisoformat(packet['retry_not_before'])
                observed=datetime.fromisoformat(packet['observed_at'])
                if earliest.tzinfo is None or observed.tzinfo is None or earliest<observed+timedelta(seconds=120):
                    raise ValueError('invalid retry timing evidence')
            else:
                # Historical receipts have no retained response headers. Preserve
                # that uncertainty and enforce at least two minutes from file write.
                earliest=datetime.fromtimestamp(path.stat().st_mtime,timezone.utc)+timedelta(seconds=120)
            if now.tzinfo is None or now<earliest:raise ValueError('HTTP503 retry must wait for its not-before time')
        failed_receipt(root,base)
        if spec.http503_retry_index==2:
            previous=retry_key(base,1)
            row=db.execute('SELECT result_path FROM reservations WHERE operation=?',(previous,)).fetchone()
            if not row or not row[0]:raise ValueError('second retry requires a first observed HTTP503 failure')
            failed_receipt(Path(row[0]),previous)
    finally:db.close()
