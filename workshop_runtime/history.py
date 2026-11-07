"""Сжатие вычислительной истории без повторного угадывания условий задачи."""
import json


def summary(data,messages,successful_runs):
    observations=[str(m.get('content') or '')[-1500:] for m in messages if m.get('role')=='tool'][-3:]
    return json.dumps({'task':data['prompt'],'kind':data['kind'],'facts':data['facts'],
        'successful_python_runs':successful_runs,'last_observations':observations,
        'files':'Исходные данные: /input/data.json. Расчётные скрипты и отчёты сохранены в /workspace.',
        'next':'Не повторяй уже выполненные проверки. Дай вывод по задаче или выполни только недостающий расчёт.'},ensure_ascii=False)
