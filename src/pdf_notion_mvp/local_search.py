"""Deterministic BM25 word/character search, with explicit OCR review states. No embeddings."""
import math
import re
import unicodedata
from collections import Counter

from .review import apply_review
from .study_bundle import encode,sha


def terms(text):
    words=re.findall(r'[\w가-힣]+',unicodedata.normalize('NFKC',text).casefold())
    result=[]
    for word in words:
        result.append('w:'+word)
        result.extend('c:'+word[i:i+2] for i in range(len(word)-1))
    return result


def prepare_search(files):
    reviewed=apply_review(files.source,files.hierarchy,files.review,files.asset_root)
    corrections={c.block_id:c for c in reviewed.layer.corrections}
    fragments={i for f in reviewed.layer.fragments if f.kind=='code' or f.status=='candidate' for i in f.source_block_ids}
    records=[]
    for section in reviewed.sections:
        for block in section.source_blocks:
            if block.kind!='text':continue
            c=corrections.get(block.block_id)
            confirmed=block.role=='body' and block.block_id not in fragments and not (c and c.status=='candidate') and (
                files.source.kind=='synthetic_ir' or c is not None and c.status=='confirmed')
            records.append(dict(record_id=block.block_id,section_id=section.section_id,source=block.source.model_dump(mode='json'),
                text=section.effective_text[block.block_id],original_text=block.text,
                status='confirmed_transcription' if confirmed and c else 'authored_synthetic' if confirmed else 'unreviewed_candidate',
                correction_id=c.correction_id if c else None,eligible_for_answer=confirmed))
    if len(records)>20000:raise ValueError('local review index limit exceeded')
    snapshot=dict(schema_version='1',mode='bm25_word_char_local',source_digest=reviewed.source_digest,
        hierarchy=reviewed.hierarchy.model_dump(mode='json'),review=reviewed.layer.model_dump(mode='json'),records=records,
        embedding_calls=0,actual_embeddings_used=False,semantic_search_implemented=False)
    snapshot['index_digest']=sha(encode(snapshot))
    return snapshot


def search(files, question, *, include_unreviewed=False, limit=5, saved_index=None):
    if not isinstance(question,str) or not question.strip() or len(question)>1000:raise ValueError('bounded nonempty query required')
    if type(include_unreviewed) is not bool or type(limit) is not int or not 1<=limit<=5:raise ValueError('explicit review scope and one-to-five results required')
    index=prepare_search(files)
    if saved_index is not None and saved_index!=index:raise ValueError('saved local index is stale or changed')
    query=set(terms(question));records=[r for r in index['records'] if include_unreviewed or r['eligible_for_answer']]
    bags=[Counter(terms(r['text'])) for r in records];n=len(bags);avg=sum(sum(b.values()) for b in bags)/n if n else 1
    frequencies=Counter(t for b in bags for t in b)
    ranked=[]
    for i,bag in enumerate(bags):
        score=0.
        for term in query&bag.keys():
            f=bag[term];idf=math.log(1+(n-frequencies[term]+.5)/(frequencies[term]+.5))
            score+=idf*f*2.2/(f+1.2*(.25+.75*sum(bag.values())/avg))
        if score>0:ranked.append((score,i))
    ranked.sort(key=lambda x:(-x[0],x[1]))
    matches=[dict(records[i],score=round(score,8),quote=records[i]['text']) for score,i in ranked[:limit]]
    return dict(mode=index['mode'],index_digest=index['index_digest'],question=question,
        include_unreviewed=include_unreviewed,matches=matches,status='matches_for_review' if matches else 'unknown',
        answer=None,human_review_required=True,semantic_correctness_verified=False,
        model_calls=0,key_reads=0,embedding_calls=0,notion_calls=0)
