"""Результаты — скачиваемые статические документы, без активных Excel-ссылок."""
import csv
import io
import re
import zipfile
from xml.etree import ElementTree


def prepare(name,content):
    if name=='analysis.csv':
        rows=list(csv.reader(io.StringIO(content.decode('utf-8-sig'))))
        output=io.StringIO();writer=csv.writer(output)
        for row in rows:
            writer.writerow([("'"+cell if cell.lstrip().startswith(('=','+','-','@')) and not re.fullmatch(r'-?\d+(?:[.,]\d+)?',cell) else cell) for cell in row])
        return output.getvalue().encode('utf-8-sig')
    if name=='analysis.xlsx':
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            if len(archive.infolist())>200 or sum(i.file_size for i in archive.infolist())>20_000_000:
                raise RuntimeError('Слишком большой Excel-отчёт.')
            if '[Content_Types].xml' not in archive.namelist():raise RuntimeError('Неверный формат Excel.')
            for item in archive.infolist():
                if 'externalLink' in item.filename or 'vbaProject' in item.filename:raise RuntimeError('Активные ссылки и макросы в отчёте запрещены.')
                if item.filename.endswith(('.xml','.rels')):
                    raw=archive.read(item)
                    if b'<!DOCTYPE' in raw or b'<!ENTITY' in raw:raise RuntimeError('Неверный формат Excel.')
                    root=ElementTree.fromstring(raw)
                    for node in root.iter():
                        if node.tag.rsplit('}',1)[-1]=='f' or node.attrib.get('TargetMode')=='External':
                            raise RuntimeError('Excel должен содержать рассчитанные значения, без формул и внешних ссылок.')
        return content
    return content
