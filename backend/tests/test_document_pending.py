from dataclasses import replace
from threading import Event

import pytest

from .document_samples import sample


@pytest.fixture(autouse=True)
def isolated_import_dependencies(app, monkeypatch):
    from app.documents import store as service, imports
    settings = app.state.settings_service
    app.state.current_settings.replace(replace(settings.current, llm_key='isolated-note-key'))
    for module in (service, imports):
        monkeypatch.setattr(module, 'runtime_ready', lambda _: True)
        monkeypatch.setattr(module, 'model_ready', lambda *_: True)
    create = app.state.document_analyses.create
    monkeypatch.setattr(app.state.document_analyses, 'create', lambda identifier, value, **kw:
        {'task_id':kw['task_id'],'status':'queued'} if 'task_id' in kw else create(identifier, value))


def import_file(client, extension='txt', raw=None):
    response = client.post('/api/v1/documents', data={'defer_extraction':'true'},
                           files={'file':(f'pending.{extension}', sample(extension) if raw is None else raw)})
    assert response.status_code == 201, response.text
    return response.json()


def finish(client, app, identifier, engine='rapidocr'):
    response = client.post(f'/api/v1/documents/{identifier}/extract', json={'parser':'local','ocr_option':engine})
    assert response.status_code == 202, response.text
    app.state.document_imports.executor.submit(lambda:None).result(timeout=10)
    state = client.get(f'/api/v1/documents/{identifier}/extraction').json()
    return state


@pytest.mark.parametrize('extension', ['pdf','docx','pptx','xlsx','csv','txt','md'])
def test_original_only_until_confirmed_then_background_extraction(client, app, monkeypatch, extension):
    from app.documents import store as service
    original = service.extract
    calls = []
    def extract(*args):
        calls.append(args[2].settings.ocr_engine)
        return original(*args)
    monkeypatch.setattr(service, 'extract', extract)
    raw = sample(extension)
    pending = import_file(client, extension, raw)
    assert pending['extracted'] is False and pending['unit_count'] == pending['character_count'] == 0
    assert calls == [] and pending['latest_task'] is None
    assert client.get('/api/v1/documents').json()[0]['extracted'] is False
    assert client.get(f"/api/v1/documents/{pending['id']}/original").content == raw
    folder = app.state.document_analyses.documents.folder(pending['id'])
    assert not (folder/'content.json').exists()
    assert import_file(client, extension, raw)['id'] == pending['id']
    assert client.get(f"/api/v1/documents/{pending['id']}/content").status_code == 409
    assert client.post(f"/api/v1/documents/{pending['id']}/analyses").status_code == 409
    state = finish(client, app, pending['id'], 'rapidocr')
    assert state['status'] == 'done', state
    prepared = client.get('/api/v1/documents').json()[0]
    assert prepared['id'] == state['document_id'] and prepared['character_count'] > 0
    assert calls == ['rapidocr'] and not folder.exists()
    assert client.post(f"/api/v1/documents/{prepared['id']}/extract").status_code == 409
    assert calls == ['rapidocr']


def test_pending_survives_reopen_and_failure_keeps_original(client, app, monkeypatch):
    from app.config import Settings
    from app.main import create_app
    from fastapi.testclient import TestClient
    pending = import_file(client, 'pdf', b'invalid retained until processing')
    settings = app.state.settings_service.current
    reopened = create_app(Settings(data_dir=settings.data_dir, model_dir=settings.model_dir, log_dir=settings.log_dir,
                                   llm_key='isolated-reopened'), start_worker=False)
    with TestClient(reopened) as other:
        assert other.get('/api/v1/documents').json()[0]['id'] == pending['id']
        state = finish(other, reopened, pending['id'])
        assert state['status'] == 'failed'
        assert other.get(f"/api/v1/documents/{pending['id']}/original").content == b'invalid retained until processing'
        assert other.get('/api/v1/documents').json()[0]['extracted'] is False


def test_rename_and_pin_survive_background_processing(client, app):
    pending = import_file(client)
    client.patch(f"/api/v1/documents/{pending['id']}", json={'filename':'课堂笔记','pinned':True})
    assert finish(client, app, pending['id'])['status'] == 'done'
    prepared = client.get('/api/v1/documents').json()[0]
    assert prepared['filename']=='课堂笔记' and prepared['pinned'] and prepared['extracted']
    assert prepared['created_at']==pending['created_at'] and prepared['format']=='txt'


def test_same_engine_reuses_material_without_changing_content(client, app):
    raw=sample('pdf')
    def process(engine):
        pending=import_file(client,'pdf',raw)
        state=finish(client,app,pending['id'],engine)
        assert state['status']=='done',state
        return state['document_id']
    first=process('rapidocr')
    folder=app.state.document_analyses.documents.folder(first)
    saved=(folder/'content.json').read_bytes()
    assert process('rapidocr')==first
    assert (folder/'content.json').read_bytes()==saved
    assert len(client.get('/api/v1/documents').json())==1


def test_duplicate_confirm_reuses_job_and_delete_is_blocked_without_holding_lock(client, app, monkeypatch):
    from app.documents import store as service
    pending=import_file(client); entered, release=Event(),Event()
    original=service.extract
    def blocked(*args):
        assert app.state.worker.lock.acquire(blocking=False)
        app.state.worker.lock.release()
        entered.set(); assert release.wait(5)
        return original(*args)
    monkeypatch.setattr(service,'extract',blocked)
    path=f"/api/v1/documents/{pending['id']}"
    try:
        response=client.post(path+'/extract',json={'parser':'local'})
        assert response.status_code==202 and entered.wait(5)
        assert client.post(path+'/extract').json()['task_id']==response.json()['task_id']
        assert client.delete(path).status_code==409
    finally:
        release.set()
    app.state.document_imports.executor.submit(lambda:None).result(timeout=10)
    assert client.get(path+'/extraction').json()['status']=='done'
    assert len(client.get('/api/v1/documents').json())==1


@pytest.mark.parametrize('extension', ['pdf','docx','pptx','xlsx','csv','txt','md'])
def test_restart_between_material_commit_and_import_mapping(client, app, monkeypatch, extension):
    import hashlib
    from app.db import DocumentTask
    from app.documents.imports import DocumentImports
    from app.documents.tasks import DocumentAnalyses
    from app.errors import TaskInterrupted
    imports = app.state.document_imports
    documents = imports.documents
    # Restore real task creation; this regression must prove database idempotency.
    monkeypatch.setattr(imports.analyses, 'create', DocumentAnalyses.create.__get__(imports.analyses))
    raw = sample(extension)
    pending = import_file(client, extension, raw)
    prepare, extracted = documents.prepare, []
    def interrupted(*args, **kwargs):
        result = prepare(*args, **kwargs)
        extracted.append(result['id'])
        raise TaskInterrupted()
    with monkeypatch.context() as scoped:
        scoped.setattr(documents, 'prepare', interrupted)
        response = client.post(f"/api/v1/documents/{pending['id']}/extract", json={'parser':'local'})
        assert response.status_code == 202, response.text
        imports.executor.submit(lambda:None).result(timeout=10)
    state = imports.state(pending['id'])
    assert state['status'] == 'interrupted' and state['document_id'] is None
    assert len(extracted) == 1 and not documents.folder(pending['id']).exists()
    material = documents.folder(extracted[0])
    saved = (material/'content.json').read_bytes()
    assert (material/f'original.{extension}').read_bytes() == raw
    original = hashlib.sha256(raw).hexdigest()
    expected = hashlib.sha256(f'{original}:rapidocr'.encode()).hexdigest() if extension in {'pdf','docx','pptx'} else original
    from app.db import Document
    with app.state.sessions() as session:
        assert session.get(Document, extracted[0]).fingerprint == expected
        assert session.query(DocumentTask).count() == 0
    reopened = DocumentImports(documents, imports.analyses, imports.get_settings, imports.sessions, imports.diagnostics, imports.operation_lock)
    try:
        reopened.recover()
        reopened.executor.submit(lambda:None).result(timeout=10)
        final = reopened.public(pending['id'])
        assert final and final['status'] == 'done', reopened.state(pending['id'])
        assert final['document_id'] == extracted[0] and final['task_id'] == state['task_id']
        reopened.run(pending['id'])
        reopened.recover()
        reopened.executor.submit(lambda:None).result(timeout=10)
        with app.state.sessions() as session:
            tasks = session.query(DocumentTask).all()
            assert len(tasks) == 1 and tasks[0].id == state['task_id']
        assert list(app.state.worker.pending.queue).count(('document', state['task_id'])) == 1
        assert (material/'content.json').read_bytes() == saved
    finally:
        reopened.close()
