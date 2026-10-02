"""Approved text-only embedding experiment with the existing cumulative ledger.

Ranks evidence locally. Similarity never certifies an answer or source accuracy.
"""
import hashlib
import json
import math
import os
import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Literal

import httpx
from pydantic import Field

from .contracts import Contract
from .lesson_generation import BUDGET_MICRO_USD, _regular
from .live_run import fingerprint
from .providers import selected_key
from .rag import prepare_index
from .rag_files import load_review_files, read_json_input
from .study_bundle import private_destination

MODEL = "gemini-embedding-2"
ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/"+MODEL+":embedContent"
# ceil(8192 input tokens * $0.20/M * 1M micro dollars). Never refund uncertainty.
REQUEST_COST_MICRO_USD = 1639
SUBCAP_MICRO_USD = 40000


class EmbeddingSnapshot(Contract):
    model: Literal["gemini-embedding-2"] = MODEL
    input_token_capacity: Literal[8192] = 8192
    text_usd_per_million: Literal["0.20"] = "0.20"
    dimensions: Literal[768] = 768
    checked_on: date
    pricing_url: Literal["https://ai.google.dev/gemini-api/docs/pricing"] = "https://ai.google.dev/gemini-api/docs/pricing"
    model_url: Literal["https://ai.google.dev/gemini-api/docs/models/gemini-embedding-2"] = "https://ai.google.dev/gemini-api/docs/models/gemini-embedding-2"


class EmbeddingSpec(Contract):
    input_paths: list[str] = Field(min_length=4,max_length=4)
    input_sha256: list[str] = Field(min_length=4,max_length=4)
    queries: list[str] = Field(min_length=1,max_length=2)
    snapshot: EmbeddingSnapshot
    output_dir: str
    budget_ledger: str
    budget_device: int
    budget_inode: int
    budget_baseline: dict[str,str]
    key_project_root: str
    created_at: datetime
    expires_at: datetime
    request_digest: str
    corpus_digest: str
    max_requests: int = Field(ge=2,le=22,strict=True)
    max_attempts: Literal[1] = 1
    timeout_seconds: Literal[30] = 30
    subcap_micro_usd: Literal[40000] = SUBCAP_MICRO_USD
    cumulative_cap_micro_usd: Literal[10000000] = BUDGET_MICRO_USD
    images_transmitted: Literal[False] = False


class EmbeddingApproval(Contract):
    spec: EmbeddingSpec
    plan_sha256: str
    user_approved: bool = Field(default=False,strict=True)
    data_transfer_confirmed: bool = Field(default=False,strict=True)
    pricing_capabilities_confirmed: bool = Field(default=False,strict=True)


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _existing_budget(path):
    path=Path(path);_regular(path)
    if not path.is_file() or path.suffix!=".sqlite": raise ValueError("existing shared ledger required")
    db=sqlite3.connect("file:"+str(path.resolve())+"?mode=rw",uri=True,timeout=5,isolation_level=None)
    try:
        if (db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()!=[("ledger_identity",),("reservations",)] or
                db.execute("SELECT value FROM ledger_identity").fetchall()!=[("pdf-notion-text-lesson-budget-v1",)]):
            raise ValueError("shared ledger identity mismatch")
        return db
    except BaseException:
        db.close();raise


def _baseline(db):
    return {row[0]:fingerprint(list(row)) for row in db.execute("SELECT * FROM reservations ORDER BY operation")}


def _check_budget(s,db):
    path=Path(s.budget_ledger);_regular(path)
    stat=path.stat()
    if (stat.st_dev,stat.st_ino)!=(s.budget_device,s.budget_inode): raise ValueError("shared ledger replaced")
    rows=_baseline(db)
    if any(rows.get(op)!=value for op,value in s.budget_baseline.items()):
        raise ValueError("existing budget reservations changed or lost")


def _corpus(paths):
    source,outline,review,manifest=map(Path,paths)
    files=load_review_files(source,outline,review,[],source.parent/"unused-embedding-output.json")
    index=prepare_index(files.source,files.hierarchy,files.review,asset_root=files.asset_root)
    raw=read_json_input(manifest,max_bytes=100000)
    if not isinstance(raw,list) or not 1<=len(raw)<=20:
        raise ValueError("one to twenty approved prose records required")
    by_id={e.evidence_id:e for e in index.entries}
    forbidden={b for f in files.review.fragments for b in f.source_block_ids}
    records=[]; entries=[]; seen=set()
    for record in raw:
        if not isinstance(record,dict) or set(record)!={"evidence_id","source_block_id","page","text"}:
            raise ValueError("unexpected manifest fields")
        e=by_id.get(record["evidence_id"])
        if e is None or e.evidence_id in seen or e.source_block_id in forbidden:
            raise ValueError("unconfirmed, repeated or fragment evidence")
        expected=dict(evidence_id=e.evidence_id,source_block_id=e.source_block_id,page=e.source.page,text=e.text)
        if record!=expected:
            raise ValueError("manifest differs from authoritative reviewed evidence")
        if not e.text.strip() or len(e.text.encode())>16000:
            raise ValueError("text size exceeded")
        records.append(expected);entries.append(e.model_dump(mode="json"));seen.add(e.evidence_id)
    return records,entries


def request_bodies(records,queries):
    if len(set(queries))!=len(queries) or any(not q.strip() or len(q.encode())>2000 for q in queries):
        raise ValueError("bounded unique nonempty queries required")
    # Official retrieval prefixes add no outside document content or title.
    texts=["title: none | text: "+r["text"] for r in records]
    texts += ["task: search result | query: "+q for q in queries]
    return [dict(model="models/"+MODEL,content=dict(parts=[dict(text=t)]),outputDimensionality=768) for t in texts]


def create_proposal(paths,queries,output_dir,budget_ledger,key_project_root,*,checked_on,now=None):
    now=now or datetime.now(timezone.utc); paths=[Path(p).resolve() for p in paths]
    records,entries=_corpus(paths); bodies=request_bodies(records,queries)
    root=private_destination(Path(output_dir),paths)
    budget=Path(budget_ledger);_regular(budget)
    if not budget.is_file(): raise ValueError("existing shared ledger required")
    private_destination(budget.parent,paths)
    db=_existing_budget(budget)
    try: baseline=_baseline(db)
    finally: db.close()
    stat=budget.stat()
    key_root=Path(key_project_root)
    if not key_root.is_absolute() or key_root.is_symlink() or any(p.is_symlink() for p in key_root.parents):
        raise ValueError("explicit regular key project root required")
    spec=EmbeddingSpec(input_paths=[str(p) for p in paths],input_sha256=[_sha(p) for p in paths],
        queries=queries,snapshot=EmbeddingSnapshot(checked_on=checked_on),output_dir=str(root),
        budget_ledger=str(budget.resolve()),budget_device=stat.st_dev,budget_inode=stat.st_ino,
        budget_baseline=baseline,key_project_root=str(key_root.resolve()),created_at=now,
        expires_at=now+timedelta(hours=1),request_digest=fingerprint(bodies),corpus_digest=fingerprint(entries),
        max_requests=len(bodies))
    return EmbeddingApproval(spec=spec,plan_sha256=fingerprint(spec.model_dump(mode="json")))


def validate(approval,expected_sha,*,now=None):
    a=EmbeddingApproval.model_validate(approval.model_dump());s=a.spec
    now=now or datetime.now(timezone.utc)
    if expected_sha!=a.plan_sha256 or fingerprint(s.model_dump(mode="json"))!=a.plan_sha256:
        raise ValueError("plan fingerprint mismatch")
    if not all([a.user_approved,a.data_transfer_confirmed,a.pricing_capabilities_confirmed]):
        raise ValueError("explicit approvals required")
    if any(t.tzinfo is None for t in [now,s.created_at,s.expires_at]): raise ValueError("aware times required")
    if not s.created_at<=now<s.expires_at or not timedelta(0)<s.expires_at-s.created_at<=timedelta(hours=1):
        raise ValueError("approval expired")
    if not 0<=(now.date()-s.snapshot.checked_on).days<=1: raise ValueError("current price review required")
    records,entries=_corpus(s.input_paths);bodies=request_bodies(records,s.queries)
    if ([_sha(p) for p in s.input_paths]!=s.input_sha256 or fingerprint(bodies)!=s.request_digest or
            fingerprint(entries)!=s.corpus_digest or len(bodies)!=s.max_requests or
            len(bodies)*REQUEST_COST_MICRO_USD>s.subcap_micro_usd):
        raise ValueError("approved inputs or budget changed")
    private_destination(Path(s.output_dir),list(map(Path,s.input_paths)))
    private_destination(Path(s.budget_ledger).parent,list(map(Path,s.input_paths)))
    return a,records,entries,bodies


def _atomic(path,value):
    _regular(path)
    temporary=path.with_name(path.name+".tmp");_regular(temporary)
    fd=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(fd,"w") as stream:
        json.dump(value,stream,ensure_ascii=False,allow_nan=False,indent=2)
        stream.flush();os.fsync(stream.fileno())
    temporary.replace(path)


def vector(packet):
    values=packet.get("embedding",{}).get("values")
    if not isinstance(values,list) or len(values)!=768 or any(type(v) not in (int,float) or not math.isfinite(v) for v in values):
        raise ValueError("invalid provider embedding")
    norm=math.sqrt(sum(v*v for v in values))
    if not math.isfinite(norm) or norm==0: raise ValueError("invalid vector norm")
    return [v/norm for v in values]


def _redact(value,key):
    if isinstance(value,str): return value.replace(key,"[REDACTED_SELECTED_KEY]")
    if isinstance(value,list): return [_redact(item,key) for item in value]
    if isinstance(value,dict): return {_redact(name,key):_redact(item,key) for name,item in value.items()}
    return value


def execute(approval,expected_sha,*,client_factory=None,key_provider=None,now=None):
    a,records,entries,bodies=validate(approval,expected_sha,now=now);s=a.spec
    _regular(Path(s.key_project_root)/".env")
    root=Path(s.output_dir);root.mkdir(exist_ok=True,parents=True,mode=0o700)
    # Identity ignores approval UUID/time: fresh proposals cannot replay the same requests.
    op=fingerprint(["embedding-validation-v1",s.corpus_digest,s.request_digest,s.snapshot.model])
    operations=["embedding:"+op+":"+str(i) for i in range(len(bodies))]
    paths=[root/f"{op}-{i:02d}.json" for i in range(len(bodies))]
    for path in paths: _regular(path)
    db=_existing_budget(Path(s.budget_ledger));reserved=False;requests=0;packets=[]
    try:
        db.execute("BEGIN IMMEDIATE")
        _check_budget(s,db)
        rows=[db.execute("SELECT status,result_path,result_sha,request_count,cost FROM reservations WHERE operation=?",(o,)).fetchone() for o in operations]
        if any(rows):
            db.execute("COMMIT")
            if not all(rows) or any(r[0]!="completed" or r[1]!=str(p) or r[3]!=1 or r[4]!=REQUEST_COST_MICRO_USD for r,p in zip(rows,paths)):
                raise ValueError("consumed or uncertain requests; automatic retry forbidden")
            for row,path,body in zip(rows,paths,bodies):
                saved=read_json_input(path,max_bytes=1000000)
                if (fingerprint(saved)!=row[2] or saved["request_digest"]!=fingerprint(body) or
                        saved["endpoint"]!=ENDPOINT or saved["execution_mode"]!=("injected" if client_factory or key_provider else "network_unattested")):
                    raise ValueError("saved receipt changed")
                vector(saved["provider_packet"]);packets.append(saved)
        else:
            if any(p.exists() for p in paths): raise ValueError("unreceipted result exists")
            total=db.execute("SELECT coalesce(sum(cost),0) FROM reservations").fetchone()[0]
            cost=len(bodies)*REQUEST_COST_MICRO_USD
            if total+cost>BUDGET_MICRO_USD or cost>SUBCAP_MICRO_USD: raise ValueError("shared budget exhausted")
            for o in operations:
                db.execute("INSERT INTO reservations(operation,run_id,cost,status) VALUES(?,?,?,?)",(o,o,REQUEST_COST_MICRO_USD,"reserved"))
            db.execute("COMMIT");reserved=True
            # Every request slot is durably consumed before the first key read.
            key=key_provider() if key_provider else selected_key("GEMINI_API_KEY",Path(s.key_project_root))
            if not key: raise ValueError("selected key unavailable")
            factory=client_factory or httpx.Client
            for i,(o,path,body) in enumerate(zip(operations,paths,bodies)):
                calls=0
                def guard(request):
                    nonlocal calls,requests
                    if (calls or request.method!="POST" or str(request.url)!=ENDPOINT or
                            fingerprint(json.loads(request.content))!=fingerprint(body) or len(request.content)>20000 or
                            request.headers.get("x-goog-api-key")!=key or
                            request.extensions.get("timeout")!={k:30 for k in ("connect","read","write","pool")}):
                        raise ValueError("final request differs from approval")
                    # Mark dispatch before transport: timeout/crash cannot be retried.
                    db.execute("UPDATE reservations SET request_count=1 WHERE operation=?",(o,))
                    calls+=1;requests+=1
                with factory(timeout=30,trust_env=False,follow_redirects=False,event_hooks={"request":[guard]}) as client:
                    with client.stream("POST",ENDPOINT,headers={"x-goog-api-key":key},json=body) as response:
                        raw=b""
                        for chunk in response.iter_bytes():
                            raw+=chunk
                            if len(raw)>1000000: raise ValueError("provider response too large")
                        if calls!=1: raise ValueError("approved dispatch not observed")
                        status=response.status_code
                        # Preserve a bounded response even when it fails validation.
                        # Unexpected credential echoes are redacted before any file write.
                        decoded=raw.decode("utf-8").replace(key,"[REDACTED_SELECTED_KEY]")
                        try: packet=_redact(json.loads(decoded),key)
                        except ValueError: packet={"unparsed_response":decoded}
                receipt=dict(endpoint=ENDPOINT,model=MODEL,request_digest=fingerprint(body),
                    request_body=body,provider_packet=packet,http_status=status,
                    execution_mode="injected" if client_factory or key_provider else "network_unattested",
                    conditional_cost_micro_usd=REQUEST_COST_MICRO_USD,provider_billed_cost_verified=False)
                _atomic(path,receipt)
                db.execute("UPDATE reservations SET result_path=?,result_sha=? WHERE operation=?",
                           (str(path),fingerprint(receipt),o))
                if status!=200: raise ValueError("provider returned non-success")
                vector(packet)
                db.execute("UPDATE reservations SET status='completed',result_path=?,result_sha=? WHERE operation=?",
                           (str(path),fingerprint(receipt),o));packets.append(receipt)
            key=None
        vectors=[vector(p["provider_packet"]) for p in packets];rankings=[]
        for q,v in zip(s.queries,vectors[len(records):]):
            ranked=sorted([dict(**r,score=sum(x*y for x,y in zip(v,d)),source=entry["source"],
                                correction_id=entry["correction_id"]) for r,entry,d in zip(records,entries,vectors)],
                          key=lambda r:(-r["score"],r["evidence_id"]))
            rankings.append(dict(query=q,results=ranked,answer_generated=False,
                                 similarity_is_not_answer_evidence=True))
        result=dict(status="ranked_for_independent_review",operation_key=op,model=MODEL,
            documents=len(records),queries=len(s.queries),dimensions=768,request_count_this_run=requests,
            conditional_cost_reserved_micro_usd=len(bodies)*REQUEST_COST_MICRO_USD,
            cumulative_reserved_micro_usd=db.execute("SELECT sum(cost) FROM reservations").fetchone()[0],
            source_entries=entries,rankings=rankings,provider_billed_cost_verified=False,
            provider_packet_paths=[str(p) for p in paths],notion_requests=0,generation_requests=0)
        destination=root/(op+"-rankings.json")
        if not destination.exists(): _atomic(destination,result)
        else:
            saved=read_json_input(destination,max_bytes=1000000)
            comparable=dict(result,request_count_this_run=saved["request_count_this_run"],
                            cumulative_reserved_micro_usd=saved["cumulative_reserved_micro_usd"])
            if saved!=comparable: raise ValueError("saved rankings changed")
        return result,"completed" if requests else "unchanged"
    except BaseException as exc:
        if db.in_transaction: db.execute("ROLLBACK")
        if reserved:
            for o in operations:
                db.execute("UPDATE reservations SET status='failed',error_code=? WHERE operation=? AND status!='completed'",(type(exc).__name__,o))
        # Do not include key, request or provider error text in diagnostics.
        raise ValueError("embedding validation failed: "+type(exc).__name__) from None
    finally: db.close()
