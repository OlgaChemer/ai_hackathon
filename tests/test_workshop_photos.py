import asyncio
import io
import json
import unittest
from copy import deepcopy
from dataclasses import replace
from unittest.mock import AsyncMock,patch

import httpx
from PIL import Image
from backend import phone
from backend.config import settings
from backend.llm.base import LLMResponse,VisionImage
from backend.llm.deepseek import DeepSeekClient
from backend.llm.gigachat import GigaChatClient
from backend.workshop import photos,sandbox


def picture():
    stream=io.BytesIO();Image.new('RGB',(300,200),'red').save(stream,format='PNG');return stream.getvalue()


def verdict():
    return {'visible_state':'cosmetic','image_kind':'interior','observations':['На стене видна потёртость.'],
            'issues':[{'photo':1,'observation':'Потёртость на стене.','certainty':'visible'}],
            'limitations':['Скрытая проводка по фото не проверяется.']}


class PhotoTests(unittest.IsolatedAsyncioTestCase):
    def test_only_trusted_image_hosts_and_valid_image_content(self):
        for url in ('http://cdn-p.cian.site/photo.jpg','https://127.0.0.1/x','https://cian.ru.evil.test/x',
                    'https://cdn-p.cian.site:8443/x','https://user@cdn-p.cian.site/x','file:///etc/passwd'):
            self.assertFalse(photos.image_url(url),url)
        self.assertTrue(photos.image_url('https://images.cdn-cian.ru/photo.jpg'))
        self.assertTrue(photos.prepare_image(picture()).startswith(b'\xff\xd8'))
        with self.assertRaises(Exception):photos.prepare_image(b'<html>not an image</html>')

    async def test_gallery_model_evidence_cached_and_not_interior_is_unknown(self):
        row={'id':'a','address':'Тестовая, 1','url':'https://www.cian.ru/sale/flat/123456/'}
        snapshot={'listings':[row]};model=type('Model',(),{'model':'vision-test','analyze_images':AsyncMock(return_value=LLMResponse(json.dumps(verdict())))})()
        gallery={'offer':{'photos':[{'fullUrl':f'https://cdn-p.cian.site/{i}.jpg'} for i in range(12)]}}
        with patch.object(photos.cian,'check_listing_page',AsyncMock(return_value=gallery)) as fetch,patch.object(photos,'download',AsyncMock(return_value=picture())),patch.object(photos,'get_vision_provider',return_value=model):
            inspector=photos.Inspector(snapshot,'deepseek',lambda t:None)
            result=(await inspector.inspect(['a']))[0]
            self.assertEqual(result['status'],'analyzed');self.assertEqual(len(result['photos']),4)
            self.assertEqual(result['gallery_count'],12);self.assertEqual(result['issues'][0]['photo'],1)
            await inspector.inspect(['a']);self.assertEqual(model.analyze_images.await_count,1)
            restored=photos.Inspector({'listings':[row],'visual_analysis':[result]},'deepseek',lambda t:None)
            await restored.inspect(['a']);self.assertEqual(fetch.await_count,1)
            with self.assertRaises(ValueError):await inspector.inspect(['foreign'])
            value=verdict();value['image_kind']='not_interior'
            model.analyze_images.return_value=LLMResponse(json.dumps(value))
            result=(await photos.Inspector(snapshot,'deepseek',lambda t:None).inspect(['a']))[0]
            self.assertEqual(result['visible_state'],'unknown')
            value['image_kind']='render';model.analyze_images.return_value=LLMResponse(json.dumps(value))
            result=(await photos.Inspector(snapshot,'deepseek',lambda t:None).inspect(['a']))[0]
            self.assertEqual(result['visible_state'],'unknown')

    async def test_no_photos_and_invalid_model_response_do_not_invent_condition(self):
        model=type('Model',(),{'model':'vision-test','analyze_images':AsyncMock(return_value=LLMResponse('{"bad":"schema"}'))})()
        with patch.object(photos,'get_vision_provider',return_value=model),patch.object(photos,'download',AsyncMock(return_value=picture())):
            missing=(await photos.Inspector({'listings':[{'id':'empty'}]},'deepseek',lambda t:None).inspect(['empty']))[0]
            self.assertEqual(missing['status'],'unavailable');model.analyze_images.assert_not_awaited()
            row={'id':'a','photos':['https://cdn-p.cian.site/a.jpg']}
            result=(await photos.Inspector({'listings':[row]},'deepseek',lambda t:None).inspect(['a']))[0]
            self.assertEqual(result['status'],'unavailable');self.assertNotIn('visible_state',result)
            model.analyze_images.reset_mock()
            model.analyze_images.side_effect=[LLMResponse('{"bad":"schema"}'),LLMResponse(json.dumps(verdict()))]
            result=(await photos.Inspector({'listings':[row]},'deepseek',lambda t:None).inspect(['a']))[0]
            self.assertEqual(result['status'],'analyzed');self.assertEqual(model.analyze_images.await_count,2)

    async def test_bridge_limits_photo_payload_and_checks_known_ids(self):
        inspector=photos.Inspector({'listings':[{'id':'a'}]},'deepseek',lambda t:None)
        bridge=sandbox.Bridge(None,'Оценка квартиры',lambda t:None,inspector)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=bridge.app),base_url='http://bridge') as client:
            self.assertEqual((await client.post('/inspect-photos',json={'listing_ids':['foreign']})).status_code,422)
            self.assertEqual((await client.post('/inspect-photos',content=b'x'*2001)).status_code,413)
            self.assertEqual((await client.post('/inspect-photos',json={'url':'http://localhost'})).status_code,422)
            result=await client.post('/inspect-photos',json={'listing_ids':['a']})
            self.assertEqual(result.status_code,200);self.assertEqual(result.json()['listings'][0]['status'],'unavailable')

    async def test_deepseek_images_are_multimodal_and_gigachat_files_are_deleted(self):
        image=VisionImage(picture(),'image/png');requests=[]
        def deep(request):
            body=json.loads(request.content);self.assertEqual(body['messages'][0]['content'][1]['type'],'image_url')
            self.assertEqual(body['thinking'],{'type':'disabled'})
            self.assertTrue(body['messages'][0]['content'][1]['image_url']['url'].startswith('data:image/png;base64,'))
            return httpx.Response(200,json={'choices':[{'message':{'content':json.dumps(verdict())}}]})
        d=DeepSeekClient('fixture-key','https://model','vision')
        await d._http.aclose();d._http=httpx.AsyncClient(transport=httpx.MockTransport(deep),base_url='https://model')
        await d.analyze_images('Ответь JSON',[image]);await d._http.aclose()
        def giga(request):
            requests.append(request.url.path)
            if request.url.path=='/files':return httpx.Response(200,json={'id':'test-file'})
            if request.url.path.endswith('/delete'):return httpx.Response(200,json={})
            body=json.loads(request.content);self.assertEqual(body['messages'][1]['attachments'],['test-file'])
            return httpx.Response(200,json={'choices':[{'message':{'content':json.dumps(verdict())}}]})
        g=GigaChatClient('fixture','scope','vision','https://auth','https://model',False)
        await g._http.aclose();g._http=httpx.AsyncClient(transport=httpx.MockTransport(giga),base_url='https://model')
        g._get_token=AsyncMock(return_value='fixture-token')
        await g.analyze_images('Ответь JSON',[image]);self.assertEqual(requests[-1],'/files/test-file/delete');await g._http.aclose()

    def test_phone_requires_the_premium_operator_or_valid_pin(self):
        with patch.object(phone,'settings',replace(settings,phone_pin='',exolve_api_key='fixture')):
            operator={'email':'darkyhat@mail.ru','premium':True}
            self.assertTrue(phone.allowed(None,operator));self.assertTrue(phone.status(operator)['ready'])
            for user in (None,{'email':'other@example.ru','premium':True},{'email':'darkyhat@mail.ru','premium':False}):
                self.assertFalse(phone.allowed(None,user));self.assertFalse(phone.status(user)['ready'])
