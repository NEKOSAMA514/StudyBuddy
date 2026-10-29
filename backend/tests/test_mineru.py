"""隔离目录及 MockTransport，不访问 MinerU 或正式资料。"""
import io
import json
import zipfile
from dataclasses import replace
from threading import Event

import httpx
import pytest

from app.documents import mineru
from app.errors import AppError
from .document_samples import sample


def test_private_settings_failure_does_not_publish(client, app, monkeypatch):
    from app.settings import mineru as settings
    assert client.get('/api/v1/settings/mineru').json() == {'has_token': False}
    assert client.put('/api/v1/settings/mineru', json={'token':'private-mineru-value'}).json() == {'has_token':True}
    assert client.put('/api/v1/settings/mineru', json={}).json() == {'has_token':True}
    service = app.state.settings_service
    assert settings.load(replace(service.current, mineru_token='')).mineru_token == 'private-mineru-value'
    def fail(*args):
        raise OSError('private-mineru-value write failed')
    with monkeypatch.context() as scoped:
        scoped.setattr(settings, 'write_settings', fail)
        response = client.put('/api/v1/settings/mineru', json={'token':'replacement-value'})
    assert response.status_code == 500 and 'private-mineru-value' not in response.text
    assert service.current.mineru_token == 'private-mineru-value'
    assert app.state.diagnostics.redact('private-mineru-value') == '[REDACTED]'
    assert client.put('/api/v1/settings/mineru', json={'clear_token':True}).json() == {'has_token':False}
    assert settings.load(service.current).mineru_token == ''


@pytest.mark.parametrize('url', ['http://mineru.net/a', 'https://localhost/a', 'https://mineru.net.evil.test/a',
                                'https://mineru.net:8000/a', 'https://name:secret@mineru.net/a'])
def test_untrusted_urls(url):
    with pytest.raises(AppError): mineru.trusted_url(url)


def zip_result(blocks=None, extra=None):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, 'w') as archive:
        archive.writestr('full.md', '# Official\n\n$$x=1$$')
        archive.writestr('sample_content_list.json', json.dumps(blocks if blocks is not None else [{'type':'text','text':'First','page_idx':0}]))
        if extra: archive.writestr(extra, 'unsafe')
    raw = stream.getvalue()
    return raw.replace(b'dir/escape.md', b'dir\\escape.md') if extra == 'dir\\escape.md' else raw


@pytest.mark.parametrize('mode', ['mineru_agent', 'mineru_vlm'])
def test_download_retry_reuses_remote_task_and_auth_isolated(tmp_path, monkeypatch, mode):
    calls, fail_download = [], [True]
    def handle(request):
        calls.append(request)
        if request.url.host == 'mineru.net':
            assert bool(request.headers.get('authorization')) == (mode == 'mineru_vlm')
            if request.method == 'POST':
                payload = json.loads(request.content)
                if mode == 'mineru_vlm':
                    assert payload['model_version'] == 'vlm' and payload['files'][0]['is_ocr'] is True
                return httpx.Response(200,json={'code':0,'data':{'task_id':'fixed','file_url':'https://cdn-mineru.openxlab.org.cn/upload',
                    'batch_id':'fixed','file_urls':['https://cdn-mineru.openxlab.org.cn/upload']}})
            state = 'waiting-file' if not any(r.method == 'PUT' for r in calls) else 'done'
            result = {'state':state,'markdown_url':'https://cdn-mineru.openxlab.org.cn/full.md',
                      'full_zip_url':'https://cdn-mineru.openxlab.org.cn/result.zip'}
            return httpx.Response(200,json={'code':0,'data':result if mode == 'mineru_agent' else {'extract_result':[result]}})
        assert 'authorization' not in request.headers
        if request.method == 'PUT': return httpx.Response(200)
        if fail_download[0]:
            fail_download[0] = False
            return httpx.Response(503)
        return httpx.Response(200,content=b'# Official\n\n$$x=1$$' if mode == 'mineru_agent' else zip_result())
    original = httpx.Client
    monkeypatch.setattr(mineru,'Client',lambda **kwargs: original(transport=httpx.MockTransport(handle),**kwargs))
    pdf = tmp_path/'batch.pdf'; pdf.write_bytes(sample('pdf'))
    adapter = mineru.MinerUClient('not-for-storage',lambda:None,lambda _:None,lambda *_:None)
    with pytest.raises(httpx.HTTPStatusError): adapter.parse(pdf,tmp_path/'cache',mode,2)
    text, blocks = adapter.parse(pdf,tmp_path/'cache',mode,2)
    assert text == '# Official\n\n$$x=1$$' and (blocks is None) == (mode == 'mineru_agent')
    assert len([r for r in calls if r.method=='POST']) == len([r for r in calls if r.method=='PUT']) == 1
    count=len(calls); adapter.parse(pdf,tmp_path/'cache',mode,2)
    assert len(calls)==count and not list((tmp_path/'cache').glob('*.part'))


def test_uncertain_submission_never_resubmits(tmp_path, monkeypatch):
    calls=[]
    def request(request):
        calls.append(request); raise httpx.ReadTimeout('unknown',request=request)
    original=httpx.Client
    monkeypatch.setattr(mineru,'Client',lambda **kwargs: original(transport=httpx.MockTransport(request),**kwargs))
    pdf=tmp_path/'batch.pdf'; pdf.write_bytes(sample('pdf'))
    adapter=mineru.MinerUClient('',lambda:None,lambda _:None,lambda *_:None)
    with pytest.raises(httpx.ReadTimeout): adapter.parse(pdf,tmp_path/'cache','mineru_agent',2)
    with pytest.raises(AppError,match='上次提交'): adapter.parse(pdf,tmp_path/'cache','mineru_agent',2)
    assert len(calls)==1


def test_explicit_credential_rejection_can_be_manually_retried(tmp_path,monkeypatch):
    original=httpx.Client
    monkeypatch.setattr(mineru,'Client',lambda **kwargs: original(transport=httpx.MockTransport(lambda request:httpx.Response(401)),**kwargs))
    pdf=tmp_path/'batch.pdf'; pdf.write_bytes(sample('pdf'))
    adapter=mineru.MinerUClient('fake',lambda:None,lambda _:None,lambda *_:None)
    with pytest.raises(httpx.HTTPStatusError): adapter.parse(pdf,tmp_path/'cache','mineru_vlm',2)
    assert mineru.read_json(tmp_path/'cache/remote.json')=={'failed':True}


@pytest.mark.parametrize('extra', ['../escape.md','/absolute.md','dir\\escape.md'])
def test_archive_paths_and_page_indices(tmp_path,extra):
    path=tmp_path/'bad.zip'; path.write_bytes(zip_result(extra=extra))
    with pytest.raises(AppError): mineru.read_archive(path,1)
    path.write_bytes(zip_result([{'type':'text','text':'bad','page_idx':1}]))
    with pytest.raises(AppError): mineru.read_archive(path,1)


def test_precise_keeps_header_formula_and_table():
    formula='$$\n\\frac{a}{b}=1\n$$'
    text=mineru.page_markdown([{'type':'header','text':'误归类的例题'}, {'type':'text','text':'标题','text_level':2},
        {'type':'equation','text':formula}, {'type':'table','table_body':'<table><tr><th>变量</th><th>值</th></tr><tr><td>x</td><td>2</td></tr></table>'}])
    assert '误归类的例题' in text and '## 标题' in text and formula in text
    assert '| x | 2 |' in text and '<table' not in text


def test_merged_table_preserves_row_and_column_positions():
    html = ('<table><tr><th rowspan="2">组别</th><th colspan="2">数据</th></tr>'
            '<tr><th>名称</th><th>数值</th></tr><tr><td rowspan="2">A组</td><td>x</td><td>10</td></tr>'
            '<tr><td>y<br>z|w</td><td>20</td></tr></table>')
    assert mineru.table_markdown(html).splitlines() == [
        '| 组别 | 数据 |  |', '| --- | --- | --- |', '|  | 名称 | 数值 |',
        '| A组 | x | 10 |', r'|  | y z\|w | 20 |']
    assert mineru.table_markdown('<table><tr><td rowspan="2" colspan="2">合并</td><td>a</td></tr>'
                                 '<tr><td>b</td></tr></table>').splitlines() == [
        '| 合并 |  | a |', '| --- | --- | --- |', '|  |  | b |']


@pytest.mark.parametrize('html', [
    '<table><tr><td><table><tr><td>nested</td></tr></table></td></tr></table>',
    *[f'<table><tr><td rowspan="{span}">x</td></tr></table>' for span in ('0', '-1', '1.5', 'two', '', '2')],
    '<table><tr><td colspan="0">x</td></tr></table>',
    '<table><tr><td>a</td><td rowspan="2">b</td></tr><tr><td colspan="2">overlap</td></tr></table>',
    '<table><tr><td>unclosed',
])
def test_unreadable_merged_table_is_an_explicit_error(html):
    with pytest.raises(AppError) as caught:
        mineru.table_markdown(html)
    assert caught.value.code == 'mineru_result'


def test_supplement_is_deduplicated_within_batch_not_globally():
    records=[{'page':p,'image':1,'digest':'shared','text':'图片文字（本地 OCR，识别可能有误）：\n图中信息'} for p in (1,2,21)]
    a=mineru.supplement('# Cloud',records[:2]); b=mineru.supplement('# Next',records[2:])
    assert a.count('图中信息')==b.count('图中信息')==1
    assert '第 1 页' in a and '第 21 页' in b
    assert mineru.supplement('**图中信息**',records)=='**图中信息**'


def test_supplement_only_appends_ocr_lines_missing_from_cloud():
    cloud = '# 化学平衡\n\n**化学反应等温方程式**\n\n[温度说明](https://example.invalid/source)\n\n$$x=2$$'
    records = [{'page': 3, 'image': 2, 'digest': 'diagram',
                'text': '图片文字（本地 OCR，识别可能有误）：\n化学反应 等温方程式\n温度说明\n补充解释\nx=2'}]
    result = mineru.supplement(cloud, records)
    assert result.startswith(cloud + '\n\n### 图片文字补充\n\n')
    assert result[len(cloud):] == '\n\n### 图片文字补充\n\n第 3 页 · 图片2\n\n图片文字（本地 OCR，识别可能有误）：\n补充解释'
    assert records[0]['text'].endswith('补充解释\nx=2')


def test_supplement_deduplicates_text_across_different_images():
    records = [
        {'page': 1, 'image': 1, 'digest': 'first', 'text': '图片文字（本地 OCR，识别可能有误）：\n共同解释\n原图信息'},
        {'page': 2, 'image': 3, 'digest': 'second', 'text': '图片文字（本地 OCR，识别可能有误）：\n共同 解释\n原图信息'},
        {'page': 3, 'image': 2, 'digest': 'third', 'text': '图片文字（本地 OCR，识别可能有误）：\n共同解释\n新增信息'},
    ]
    result = mineru.supplement('# Cloud', records)
    assert result.count('共同解释') == result.count('原图信息') == result.count('新增信息') == 1
    assert '共同 解释' not in result and '第 2 页' not in result
    assert result.index('第 1 页 · 图片1') < result.index('第 3 页 · 图片2')
    assert '共同解释' in mineru.supplement('# Next batch', records[1:2]).replace(' ', '')


def test_supplement_preserves_nonidentical_text_and_does_not_match_urls():
    cloud = '[数据](https://example.invalid/private-path)\n\n公式 x=2，温度升高。'
    records = [{'page': 1, 'image': 1, 'digest': 'first',
                'text': '图片文字（本地 OCR，识别可能有误）：\nprivate-path\nx=3\n温度降低。\n独有 $a_b$'}]
    result = mineru.supplement(cloud, records)
    assert result.startswith(cloud)
    assert 'private-path' in result[len(cloud):]
    assert 'x=3' in result and '温度降低。' in result and r'独有 \$a\_b\$' in result


@pytest.fixture
def configured(app,monkeypatch):
    from app.documents import store as service
    from app.documents import imports as module
    settings=app.state.settings_service
    app.state.current_settings.replace(replace(settings.current,llm_key='fake-for-isolated-import'))
    monkeypatch.setattr(service,'runtime_ready',lambda _:True)
    monkeypatch.setattr(service,'model_ready',lambda *_:True)
    monkeypatch.setattr(module,'runtime_ready',lambda _:True)
    monkeypatch.setattr(module,'model_ready',lambda *_:True)
    return app.state.document_imports


def pending(client,pages=2):
    from .pdf_samples import text_pdf
    raw = text_pdf(['source'] * pages)
    return client.post('/api/v1/documents',data={'defer_extraction':'true'},files={'file':('sample.pdf',raw)}).json()


def drain(imports):
    imports.executor.submit(lambda:None).result(timeout=10)


def test_batches_resume_and_note_creation_is_idempotent(client,app,configured,monkeypatch):
    imports=configured; calls=[]; failed=[False]
    def parse(self,pdf,folder,mode,pages):
        calls.append(folder.name); cached=folder/'full.md'
        if cached.exists(): return cached.read_text(),None
        if folder.name=='21' and not failed[0]:
            failed[0]=True; raise AppError('mineru_failed','模拟第二批失败')
        cached.write_text(f'# Batch {folder.name}\n\n$$x=1$$',encoding='utf-8')
        return cached.read_text(),None
    monkeypatch.setattr(mineru.MinerUClient,'parse',parse)
    records = [{'page':p,'image':1,'digest':'shared','text':'图片文字（本地 OCR，识别可能有误）：\nx=1\n有效图片'} for p in (1,2,21)]
    records.append({'page':2,'image':2,'digest':'other','text':'图片文字（本地 OCR，识别可能有误）：\nx=1\n有效图片'})
    monkeypatch.setattr(imports,'images',lambda *args: (records,['只在警告区']))
    doc=pending(client,21); source=doc['id']; path=f'/api/v1/documents/{source}'
    assert client.post(path+'/extract',json={'parser':'mineru_agent','ocr_option':'rapidocr'}).status_code==202
    drain(imports); first=client.get(path+'/extraction').json()
    assert first['status']=='failed' and '模拟第二批失败' in first['error']['message']
    assert client.get(path+'/original').status_code==200
    assert client.post(path+'/extract',json={'parser':'mineru_agent'}).status_code==202
    drain(imports); final=client.get(path+'/extraction').json()
    assert final['status']=='done',final
    content=client.get(f"/api/v1/documents/{final['document_id']}/content").json()
    assert [u['id'] for u in content['units']]==['D1','D2']
    assert [u['locator'] for u in content['units']]==[{'page_start':1,'page_end':20},{'page_start':21,'page_end':21}]
    assert all(u['text'].count('有效图片')==1 and '只在警告区' not in u['text'] for u in content['units'])
    assert all(u['text'].count('x=1')==1 for u in content['units'])
    from app.db import DocumentTask
    with app.state.sessions() as session:
        tasks=list(session.query(DocumentTask).all()); assert len(tasks)==1 and tasks[0].id==final['task_id']
        config=tasks[0].config
    app.state.current_settings.replace(replace(imports.get_settings(),llm_base_url='https://changed.invalid/v1'))
    imports.run(source)
    assert imports.state(source)['status']=='done'
    with app.state.sessions() as session:
        assert session.query(DocumentTask).count()==1 and session.get(DocumentTask,final['task_id']).config==config
    assert calls==['1','21','1','21']
    assert client.post(path+'/extract',json={'parser':'mineru_agent'}).json()['status']=='done'
    assert imports.state(source)['task_id']==final['task_id']


def test_parallel_close_keeps_original_and_waits_for_safe_point(client,configured,monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    imports=configured; entered=Event(); release=Event(); network=Event()
    def images(*args):
        entered.set(); assert release.wait(10); imports.check(); return [],[]
    def parse(*args):
        assert entered.wait(5); network.set(); return '# Cloud',None
    monkeypatch.setattr(imports,'images',images)
    monkeypatch.setattr(mineru.MinerUClient,'parse',parse)
    doc=pending(client); path=f"/api/v1/documents/{doc['id']}"
    assert client.post(path+'/extract',json={'parser':'mineru_agent'}).status_code==202
    assert network.wait(5) and client.delete(path).status_code==409
    assert client.post(path+'/extract').status_code==202
    with ThreadPoolExecutor(1) as executor:
        closing=executor.submit(imports.close)
        assert imports.stopping.wait(5) and not closing.done()
        release.set(); closing.result(timeout=5)
    assert imports.state(doc['id'])['status']=='interrupted'
    assert client.get(path+'/original').status_code==200


def test_settings_concurrency_keeps_style_and_mineru(client,app,monkeypatch):
    from app.settings import mineru as settings
    from concurrent.futures import ThreadPoolExecutor
    entered, release = Event(), Event()
    original = settings.save
    def save(value):
        entered.set(); assert release.wait(5); original(value)
    monkeypatch.setattr(settings,'save',save)
    with ThreadPoolExecutor(2) as executor:
        first=executor.submit(client.put,'/api/v1/settings/mineru',json={'token':'concurrent-mineru'})
        assert entered.wait(5)
        second=executor.submit(client.put,'/api/v1/settings/note-style',json={'note_style':'concise','summary_prompt':'isolated requirements'})
        release.set()
        assert first.result().status_code==second.result().status_code==200
    current=app.state.settings_service.current
    assert current.mineru_token=='concurrent-mineru' and current.note_style=='concise'
    assert settings.load(replace(current,mineru_token='')).mineru_token=='concurrent-mineru'
    from app.settings.model_store import load_settings
    assert load_settings(replace(current,note_style='detailed')).note_style=='concise'


@pytest.mark.parametrize('mode,pages,expected',[('mineru_agent',20,[20]),('mineru_agent',21,[20,1]),('mineru_vlm',201,[200,1])])
def test_pdf_batches_offsets_and_original_unchanged(client,configured,monkeypatch,tmp_path,mode,pages,expected):
    import hashlib
    imports=configured; doc=pending(client,pages)
    source=imports.documents.folder(doc['id'])/'original.pdf'
    before=hashlib.sha256(source.read_bytes()).hexdigest()
    counts=[]
    def parse(self,pdf,folder,mode,count):
        from pypdf import PdfReader
        with PdfReader(pdf) as part: assert len(part.pages)==count
        counts.append(count)
        return '# Official',None
    monkeypatch.setattr(mineru.MinerUClient,'parse',parse)
    monkeypatch.setattr(imports,'report',lambda *_:None)
    results=imports.cloud_batches(source,tmp_path,mode,'fake',doc['id'])
    assert counts==expected and results[-1][0]['last']==pages
    assert hashlib.sha256(source.read_bytes()).hexdigest()==before


def test_size_split_and_single_page_limit(client,configured,monkeypatch,tmp_path):
    from app.documents import imports as module
    imports=configured; doc=pending(client,20)
    source=imports.documents.folder(doc['id'])/'original.pdf'
    monkeypatch.setattr(imports,'report',lambda *_:None)
    calls=[]
    def parse(*args): calls.append(args[-1]); return '# Official',None
    monkeypatch.setattr(mineru.MinerUClient,'parse',parse)
    monkeypatch.setitem(module.BATCH_LIMITS,'mineru_agent',(20,2000))
    batches=imports.cloud_batches(source,tmp_path,'mineru_agent','',doc['id'])
    assert len(batches)>1 and sum(calls)==20
    assert [page for batch,_,_ in batches for page in range(batch['first'],batch['last']+1)]==list(range(1,21))
    monkeypatch.setitem(module.BATCH_LIMITS,'mineru_agent',(20,100))
    other=tmp_path/'too-small'; other.mkdir()
    with pytest.raises(AppError,match='单页超过'): imports.cloud_batches(source,other,'mineru_agent','',doc['id'])


def test_ocr_cache_reused_across_pages_and_execution(client,configured,monkeypatch,tmp_path):
    from .pdf_samples import PdfSample
    from PIL import Image
    from app.documents.image_text import ImageText
    image=io.BytesIO(); Image.new('RGB',(30,30),'black').save(image,format='PNG')
    source=tmp_path/'images.pdf'
    document = PdfSample()
    for _ in range(3): document.image(document.page(), (0,0,30,30), image.getvalue())
    document.save(source)
    calls=[]
    monkeypatch.setattr(ImageText,'recognize',lambda self,raw:(calls.append(raw) or '同一图片',''))
    monkeypatch.setattr(configured,'report',lambda *_:None)
    first,warnings=configured.images(source,tmp_path,configured.get_settings(),'fake')
    second,_=configured.images(source,tmp_path,configured.get_settings(),'fake')
    assert len(calls)==1 and len(first)==len(second)==3 and not warnings
    assert [record['page'] for record in first]==[1,2,3]


def test_precise_pages_offset_and_ocr_in_same_page(client,app,configured,monkeypatch):
    imports=configured; doc=pending(client,2)
    monkeypatch.setattr(imports,'images',lambda *args:([{'page':2,'image':1,'digest':'unique','text':'例题不能丢失\n图片内容'}],['独立警告']))
    def batches(*args):
        return [({'first':1,'last':1},'# Official',[{'type':'equation','text':'$$a=1$$','page_idx':0}]),
                ({'first':2,'last':2},'# Official',[{'type':'header','text':'例题不能丢失','page_idx':0}])]
    monkeypatch.setattr(imports,'cloud_batches',batches)
    from app.documents.schemas import DocumentExtractionInput
    app.state.current_settings.replace(replace(imports.get_settings(),mineru_token='fake'))
    imports.start(doc['id'],DocumentExtractionInput(parser='mineru_vlm'))
    drain(imports)
    state=imports.state(doc['id']); assert state['status']=='done',state
    content=client.get(f"/api/v1/documents/{state['document_id']}/content").json()
    assert [u['locator'] for u in content['units']]==[{'page':1},{'page':2}]
    assert '$$a=1$$' in content['units'][0]['text'] and '图片内容' not in content['units'][0]['text']
    assert '例题不能丢失' in content['units'][1]['text'] and '图片内容' in content['units'][1]['text']
    assert content['units'][1]['text'].count('例题不能丢失') == 1
    assert all('独立警告' not in u['text'] for u in content['units'])


def test_restart_reuses_saved_batches_and_delete_cleans_import_cache(client,app,configured,monkeypatch):
    from app.documents.imports import DocumentImports
    from app.documents.schemas import DocumentExtractionInput
    from app.errors import TaskInterrupted
    old=configured; doc=pending(client,21); sent=[]
    stop=[True]
    def parse(self,pdf,folder,mode,pages):
        cached=folder/'full.md'
        if cached.exists(): return cached.read_text(),None
        if folder.name=='21' and stop[0]: raise TaskInterrupted()
        sent.append(folder.name); cached.write_text('# saved cloud',encoding='utf-8')
        return cached.read_text(),None
    monkeypatch.setattr(mineru.MinerUClient,'parse',parse)
    monkeypatch.setattr(old,'images',lambda *args:([],[]))
    old.start(doc['id'],DocumentExtractionInput(parser='mineru_agent')); drain(old)
    assert old.state(doc['id'])['status']=='interrupted' and sent==['1']
    old.close(); stop[0]=False
    new=DocumentImports(old.documents,old.analyses, old.get_settings, old.sessions, old.diagnostics, old.operation_lock)
    monkeypatch.setattr(new,'images',lambda *args:([],[]))
    try:
        new.recover(); drain(new)
        state=new.state(doc['id']); assert state['status']=='done',state
        assert sent==['1','21']
        from app.db import DocumentTask
        with app.state.sessions() as session: assert session.query(DocumentTask).count()==1
        assert client.post(f"/api/v1/document-tasks/{state['task_id']}/stop").status_code==202
        assert client.delete(f"/api/v1/documents/{state['document_id']}").status_code==200
        assert not new.folder(doc['id']).exists()
    finally:
        new.close()


def test_cloud_failure_local_fallback_and_no_secret_in_state(client,configured,monkeypatch):
    imports=configured; doc=pending(client)
    def fail(*args): raise AppError('mineru_failed','模拟云端失败')
    monkeypatch.setattr(imports,'cloud',fail)
    path=f"/api/v1/documents/{doc['id']}"
    assert client.post(path+'/extract',json={'parser':'mineru_agent','note_style':'concise'}).status_code==202
    drain(imports); first=imports.state(doc['id']); assert first['status']=='failed'
    assert client.post(path+'/extract',json={'parser':'local'}).status_code==202
    drain(imports); state=imports.state(doc['id']); assert state['status']=='done',state
    from app.db import DocumentTask
    with imports.sessions() as session:
        task=session.get(DocumentTask,state['task_id'])
        assert task.config['note_style']=='concise' and 'mineru_token' not in task.config
    content=client.get(f"/api/v1/documents/{state['document_id']}/content").json()
    assert content['units'][0]['format']=='markdown'
    assert all('saved cloud' not in u['text'] for u in content['units'])
    assert 'fake-for-isolated-import' not in json.dumps(state)

@pytest.mark.parametrize('outcome', ['success', 'empty', 'failure'])
def test_legacy_ocr_checkpoint_maps_changed_encoding_without_charging_aliases(configured, monkeypatch, tmp_path, outcome):
    import hashlib
    from .pdf_samples import PdfSample
    from .test_pdf_images import png
    from app.documents import image_text
    from app.documents.image_text import ImageText
    from app.util.json_io import atomic_json
    sample = PdfSample()
    for _ in range(3):
        page = sample.page()
        sample.image(page, (0,0,40,30), png())
    source = tmp_path / 'legacy.pdf'
    sample.save(source)
    # 同一像素的旧 PNG 使用不同编码；摘要与新适配器输出确定不同。
    from PIL import Image
    raw = io.BytesIO()
    Image.new('RGB', (4,3), 'red').save(raw, 'PNG', compress_level=0)
    old_key = hashlib.sha256(raw.getvalue()).hexdigest()
    cached = {'success':['旧图片结果',''], 'empty':['','未识别到文字。'], 'failure':['','本地 OCR 读取失败。']}[outcome]
    record = {'page':1,'image':1,'digest':old_key,'text': '旧材料记录原样保留'}
    saved = {'pages':1,'records':[record.copy()],'cache':{old_key:cached},'warnings':[]}
    atomic_json(tmp_path/'ocr.json', saved)
    monkeypatch.setattr(configured, 'report', lambda *_:None)
    monkeypatch.setattr(ImageText, 'recognize', lambda *_:pytest.fail('completed image must never be recognized again'))
    monkeypatch.setattr(image_text, 'MAX_IMAGES', 1)
    from app.documents.pdf import PdfDocument, image_entries
    with PdfDocument(source) as document:
        new_key = next(image_entries(document.read_page(0,text=False)))[2]
    assert new_key != old_key
    records, warnings = configured.images(source,tmp_path,configured.get_settings(),'fake')
    assert records[0] == record and len(records) == 3
    assert records[1]['digest'] == records[2]['digest'] == old_key
    assert ('旧图片结果' in records[1]['text']) == (outcome == 'success')
    assert len(warnings) == (0 if outcome == 'success' else 2)
    persisted = json.loads((tmp_path/'ocr.json').read_text('utf-8'))
    assert persisted['pages'] == 3 and persisted['cache'] == {old_key:cached}
    assert persisted['records'][0] == record
    # 完成检查点直接复用，不重新解码或识别。
    monkeypatch.setattr(PdfDocument, 'read_page', lambda *_args, **_kw:pytest.fail('completed pages must be reused'))
    assert configured.images(source,tmp_path,configured.get_settings(),'fake') == (records,warnings)


def test_legacy_cache_mapping_keeps_image_numbers_after_decode_failure(configured, monkeypatch, tmp_path):
    from .pdf_samples import PdfSample
    from .test_pdf_images import png
    from app.documents import pdf
    from app.documents.image_text import ImageText
    from app.util.json_io import atomic_json
    sample = PdfSample()
    first = sample.page()
    bad = sample.image(first, (0,0,40,30), png())
    good = sample.image(first, (50,0,90,30), png('blue'))
    sample.image(sample.page(), (0,0,40,30), reference=good)
    source = tmp_path/'failed-number.pdf'
    sample.save(source)
    original = pdf.image_data
    def decode(image):
        if image.indirect_reference.idnum == bad.idnum:
            raise ValueError('old decode failure')
        return original(image)
    monkeypatch.setattr(pdf,'image_data',decode)
    saved = {'pages':1,'records':[{'page':1,'image':2,'digest':'old-blue','text':'saved'}],
             'cache':{'old-blue':['blue text','']},'warnings':['第 1 页 · 图片1：旧警告']}
    atomic_json(tmp_path/'ocr.json',saved)
    monkeypatch.setattr(configured,'report',lambda *_:None)
    monkeypatch.setattr(ImageText,'recognize',lambda *_:pytest.fail('legacy image 2 must be reused'))
    records,warnings=configured.images(source,tmp_path,configured.get_settings(),'fake')
    assert records[0] == saved['records'][0]
    assert records[1] == {'page':2,'image':1,'digest':'old-blue','text':'图片文字（本地 OCR，识别可能有误）：\nblue text'}
    assert warnings == saved['warnings']


def test_actual_ocr_and_network_wait_do_not_hold_pdfium_lock(configured, monkeypatch, tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from .pdf_samples import PdfSample
    from .test_pdf_images import png
    from app.documents import pdf
    from app.documents.image_text import ImageText
    sample=PdfSample()
    sample.image(sample.page(),(0,0,40,30),png())
    source=tmp_path/'parallel.pdf';sample.save(source)
    entered,release=Event(),Event()
    def recognize(*args):
        entered.set()
        assert release.wait(5)
        return 'image',''
    def parse(*args):
        assert entered.wait(2)
        assert pdf.PDFIUM_LOCK.acquire(blocking=False)
        pdf.PDFIUM_LOCK.release()
        release.set()
        return '# Network substitute',None
    monkeypatch.setattr(configured,'report',lambda *_:None)
    monkeypatch.setattr(ImageText,'recognize',recognize)
    monkeypatch.setattr(mineru.MinerUClient,'parse',parse)
    with ThreadPoolExecutor(1) as executor:
        future=executor.submit(configured.images,source,tmp_path,configured.get_settings(),'fake')
        try:
            result=configured.cloud_batches(source,tmp_path,'mineru_agent','fake','fake')
            assert result and len(future.result(timeout=2)[0]) == 1
        finally:
            release.set()
