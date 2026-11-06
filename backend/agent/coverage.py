"""Охват полученного среза и доказательства. Полный рынок не обещается."""
from collections import Counter
from datetime import datetime,timezone
from .criteria import evaluate


def report(s):
    st=s.search or {};rows=st.get('listings') or ([] if s.search is not None else s.shown);sources=st.get('report') or []
    pages=st.get('page_reports') or [{**r,'page':r.get('page',1)} for r in sources if r.get('status')!='db']
    reasons=set(st.get('coverage_reasons') or [])
    if any(p.get('status')=='unavailable' for p in pages):reasons.add('source_unavailable')
    reasons.update(p['reason'] for p in pages if p.get('reason')=='search_budget_exhausted')
    if any(p.get('status') in ('cached','snapshot','db') for p in [*pages,*sources]):reasons.add('cached_data')
    if st.get('pages',1)>=3:reasons.add('page_limit')
    checks=Counter();proofs=Counter();freshness=[]
    for x in rows:
        for c in evaluate(s.profile,x)['checks']:
            checks['confirmed' if c['status'] is True else 'refuted' if c['status'] is False else 'unknown']+=1
            if c['status'] is not None:
                proofs['with_source' if c.get('source') else 'missing_source']+=1
                proofs['with_time' if c.get('checked_at') else 'missing_time']+=1
        stamp=(x.get('provenance') or {}).get('observed_at') or x.get('stored_at')
        if stamp:freshness.append(stamp)
    if checks['unknown']:reasons.add('unchecked_facts')
    unknown=sum((x.get('match') or evaluate(s.profile,x))['status']=='unverified' for x in s.shown)
    return {'scope':'received_listings' if s.search is not None else 'persisted_selection','whole_market':False,'received':len(rows),
            'queries':[{'source':p.get('source'),'city':p.get('city'),'rooms':p.get('rooms'),
                        'new':p.get('new'),'page':p.get('page',1),'status':p.get('status'),
                        'count':p.get('count',0),'observed_at':p.get('fetched_at'),'reason':p.get('note')} for p in pages],
            'checks':dict(checks),'proofs':dict(proofs),'reasons':sorted(reasons),
            'freshness':{'known':len(freshness),'unknown':len(rows)-len(freshness),
                         'oldest':min(freshness) if freshness else None,'latest':max(freshness) if freshness else None},
            'shown_unverified':unknown,'generated_at':datetime.now(timezone.utc).isoformat(timespec='seconds'),
            'summary':f'Отбор из {len(rows)} полученных объявлений. Проверенные страницы и непроверенные условия показаны отдельно.'}
