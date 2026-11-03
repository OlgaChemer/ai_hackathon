"""Обновить версии CSS и графа ES-модулей перед публикацией изменений интерфейса.

Запуск: python scripts/version_frontend.py. Не требует сборщика или зависимостей.
"""
import hashlib
import json
from pathlib import Path
import re

ROOT=Path(__file__).resolve().parents[1]/'frontend'


def version(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()[:12]


def render():
    path=ROOT/'index.html'
    text=path.read_text()
    for css in ROOT.glob('*.css'):
        text=re.sub(r'href="'+re.escape(css.name)+r'(?:\?[^"\s]*)?"',
                    f'href="{css.name}?v={version(css)}"',text)
    imports={'/'+p.relative_to(ROOT).as_posix():'/'+p.relative_to(ROOT).as_posix()+'?v='+version(p)
             for p in sorted((ROOT/'js').glob('*.js'))}
    block='<script type="importmap" id="asset-versions">'+json.dumps({'imports':imports},ensure_ascii=False,separators=(',',':'))+'</script>'
    existing=r'<script type="importmap" id="asset-versions">.*?</script>'
    if re.search(existing,text,flags=re.S):text=re.sub(existing,lambda _:block,text,flags=re.S)
    else:text=text.replace('</head>',block+'\n</head>')
    text=re.sub(r'src="js/app\.js(?:\?[^"\s]*)?"',f'src="js/app.js?v={version(ROOT/"js/app.js")}"',text)
    return text


if __name__=='__main__':
    path=ROOT/'index.html'
    updated=render()
    if updated!=path.read_text():path.write_text(updated)
