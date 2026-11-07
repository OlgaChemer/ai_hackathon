"""Изолированный стенд UI: фиктивная модель и контейнер, настоящие auth/API/SQLite."""
import argparse
import os
from pathlib import Path
import tempfile


def setup():
    from backend import main,db,auth
    from backend.db.models import User,Chat
    from backend.agent.profile import merge_profile
    from backend.workshop import service
    from tests.test_workshop import ScopeLLM
    from tests.test_llm_and_agent import listing
    from tests.e2e.flexible_server import patch
    patch()
    main.phone.configured=lambda:True
    service.get_provider=lambda provider:ScopeLLM()
    async def run(job_id,snapshot,llm,progress):
        import asyncio
        progress('Выполняю расчёт и готовлю Excel.');await asyncio.sleep(.1)
        root=Path(service.settings.workshop_root)/job_id/'artifacts';root.mkdir(parents=True)
        (root/'analysis.csv').write_text('Квартира,Стоимость\nТестовая,11725000\n')
        (root/'analysis.xlsx').write_bytes(b'UI fixture only - not a real generated workbook')
        text=('## Вывод по квартирам\n\n**Сравнил варианты** по стоимости.\n\n'
              '| Показатель | Значение |\n| --- | --- |\n| Цена метра | 200 000 ₽ |\n\n'
              '- Данные сохранены.\n- Условия не менялись.\n\n'
              '<img src=x onerror="window.workshopXss=true"><script>window.workshopXss=true</script>\n\n'
              '[опасная ссылка](javascript:alert(1))')
        if snapshot.get('parent_job_id'):
            assert snapshot.get('conversation')
            text='### Ответ по сохранённой выборке\n\n**Учитываю предыдущий ответ.** Вопрос: '+snapshot['prompt']
        return {'text':text,
            'facts':snapshot['facts'],'artifacts':['analysis.csv','analysis.xlsx'],'code_runs':1,
            'source_time':snapshot['source_time']}
    service.sandbox.run=run
    async def seed():
        with db.session() as s:
            for email,name in [('darkyhat@mail.ru','Премиум'),('other@example.ru','Обычный')]:
                user=User(email=email,name=name,password_hash=auth.hash_password('workshop-ui-only-123'),premium=True)
                s.add(user);s.flush()
                if email!='darkyhat@mail.ru':continue
                profile=merge_profile({'city':'Москва','rooms':2,'budget_max':20_000_000})
                rows=[listing(id='ui-a',city='Москва',area=50,price=10_000_000),listing(id='ui-b',city='Москва',area=70,price=12_000_000)]
                s.add(Chat(id='ui-workshop',user_id=user.id,title='Москва · понравившиеся',profile=profile,
                    state={'format':1,'data':{'profile':profile,'prefs':{'liked':['ui-a','ui-b']},'shown':rows,
                        'search':{'listings':rows},'profile_revision':1}}))
    main.app.router.on_startup.append(seed)
    return main.app


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--port',type=int,default=8798);args=parser.parse_args()
    root=Path(tempfile.mkdtemp(prefix='workshop-ui-'))
    os.environ['DATABASE_URL']='sqlite:///'+str(root/'app.db')
    os.environ['WORKSHOP_ROOT']=str(root/'files');os.environ['COOKIE_SECURE']='false';os.environ['REFRESH_AT']=''
    import uvicorn
    uvicorn.run(setup(),host='127.0.0.1',port=args.port,log_level='warning')
