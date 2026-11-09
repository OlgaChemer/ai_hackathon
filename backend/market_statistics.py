"""Одинаковые ценовые метрики для агента и мастерской по одной выборке."""
import math
import statistics


def valid_number(value):
    return type(value) in (int,float) and math.isfinite(value) and value>0


def prices(rows):
    rows=[x for x in rows if valid_number(x.get('price'))]
    values=sorted(x['price'] for x in rows)
    meters=[x['price']/x['area'] for x in rows if valid_number(x.get('area'))]
    if not values:return {'count':0,'min':None,'max':None,'median':None,'mean':None,'p25':None,'p75':None,'ppm2':None}
    q1,median,q3=statistics.quantiles(values,n=4) if len(values)>1 else [values[0]]*3
    return {'count':len(values),'min':values[0],'max':values[-1],
        'median':round(median),'mean':round(statistics.mean(values)),
        'p25':round(q1),'p75':round(q3),'ppm2':round(statistics.median(meters)) if meters else None}
