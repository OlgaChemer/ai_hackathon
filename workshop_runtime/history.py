"""Сжатие вычислительной истории без повторного угадывания условий задачи."""
import json


def context_messages(data):
    """Полная история хранится в снимке; модель получает ограниченную выдержку."""
    result=[]
    for message in data.get('conversation',[])[-8:]:
        role=message.get('role')
        if role not in ('user','assistant'):continue
        text=str(message.get('content') or '')
        if role=='assistant' and len(text)>900:
            text=text[:600]+'\n[Ответ сокращён. Полный текст — в conversation файла /input/data.json.]\n'+text[-220:]
        result.append({'role':role,'content':text[:1000]})
    return result


def summary(data,messages,successful_runs,photos=None):
    observations=[str(m.get('content') or '')[-1000:] for m in messages if m.get('role')=='tool'][-3:]
    visual=[]
    for row in list(photos if photos is not None else data.get('visual_analysis',[]))[-3:]:
        visual.append({k:row.get(k) for k in ('listing_id','address','status','visible_state','image_kind','note')})
        visual[-1].update(photos_seen=len(row.get('photos',[])),
            observations=[str(x)[:350] for x in row.get('observations',[])[:4]],
            limitations=[str(x)[:300] for x in row.get('limitations',[])[:2]])
    return json.dumps({'task':data['prompt'],'kind':data['kind'],'facts':data['facts'],
        'conversation':context_messages(data),
        'visual_analysis':visual,
        'photo_results':'Результаты просмотра фото сохранены в /workspace/photo-analysis.json, при необходимости прочитай их.',
        'successful_python_runs':successful_runs,'last_observations':observations,
        'files':'Исходные данные: /input/data.json. Расчётные скрипты и отчёты сохранены в /workspace.',
        'next':'Не повторяй уже выполненные проверки. Дай вывод по задаче или выполни только недостающий расчёт.'},ensure_ascii=False)
