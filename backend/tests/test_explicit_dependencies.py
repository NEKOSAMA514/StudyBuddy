"""Explicit dependency contracts, persistence acknowledgements and shared cleanup."""
import asyncio
from pathlib import Path

import pytest
from sqlalchemy import event

from app.analysis.callbacks import ChapterCallbacks
from app.analysis.checkpoints import checkpoint_path
from app.analysis.markdown import validate_markdown
from app.db import Task, DocumentTask
from app.documents.model import DocumentModelClient
from app.videos.model import ModelClient
from .conftest import upload
from .fakes import model_content


def test_application_injects_shared_reader_and_diagnostics(client, app):
    for owner in (app.state.pipeline, app.state.document_pipeline, app.state.document_store,
                  app.state.document_imports, app.state.video_downloads):
        assert owner.diagnostics is app.state.diagnostics
        assert owner.get_settings == app.state.current_settings.read
    assert not hasattr(app.state.pipeline, 'settings')
    assert not hasattr(app.state.document_store, 'extractions')
    assert not hasattr(app.state.settings_service, 'pipeline')
    assert not hasattr(app.state.settings_service, 'video_downloads')
    before = app.state.current_settings.read()
    assert client.put('/api/v1/settings/model', json={'token':'new-llm-secret', 'model':'new-model'}).status_code == 200
    assert client.put('/api/v1/settings/mineru', json={'token':'new-mineru-secret'}).status_code == 200
    after = app.state.current_settings.read()
    assert before.llm_key == '' and after.llm_model == 'new-model'
    assert app.state.pipeline.get_settings() is after
    assert app.state.document_pipeline.get_settings() is after
    assert app.state.diagnostics.redact('new-llm-secret new-mineru-secret') == '[REDACTED] [REDACTED]'


@pytest.mark.parametrize('factory', [ModelClient, DocumentModelClient])
def test_chapters_require_explicit_callbacks_before_sending(factory, app, monkeypatch):
    model = factory(app.state.current_settings.read())
    async def request(*args):
        pytest.fail('invalid callbacks must fail before sending')
    monkeypatch.setattr(model, 'stage_request', request)
    with pytest.raises(TypeError, match='ChapterCallbacks'):
        asyncio.run(model.generate_chapters({'chapters':[{'chapter_id':'C1'}]}, lambda value: value))


@pytest.mark.parametrize('kind', ['video', 'document'])
@pytest.mark.parametrize('outcome', ['repair', 'cancel', 'save_failure'])
def test_chapter_save_acknowledgement_controls_repair_and_completion(app, monkeypatch, kind, outcome):
    model = (ModelClient if kind == 'video' else DocumentModelClient)(app.state.current_settings.read())
    calls, saved = [], []
    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        acknowledgement = asyncio.get_running_loop().create_future()
        async def request(operation, messages):
            calls.append(messages)
            chapters = [{'chapter_id':'C1', 'summary':'摘要', 'markdown':'有效正文', 'issues':[]},
                        {'chapter_id':'C2', 'summary':'摘要', 'markdown':'', 'issues':[]}]
            if len(calls) == 2:
                assert 'C1' not in messages[-2]['content']
                chapters = [{**chapters[1], 'markdown':'修正正文'}]
            return model_content({'chapters':chapters})
        def chapter(draft):
            validate_markdown(draft['markdown'])
            return draft
        async def document_accept(draft):
            entered.set()
            await release.wait()
            if outcome == 'save_failure':
                raise OSError('save acknowledgement failed')
            saved.append(draft['chapter_id'])
        def video_accept(draft):
            entered.set()
            if draft['chapter_id'] == 'C2':
                done = asyncio.get_running_loop().create_future()
                saved.append('C2')
                done.set_result(None)
                return done
            return acknowledgement
        monkeypatch.setattr(model, 'stage_request', request)
        callbacks = ChapterCallbacks(lambda value: value, chapter, video_accept if kind == 'video' else document_accept)
        running = asyncio.create_task(model.generate_chapters({'chapters':[{'chapter_id':'C1'}, {'chapter_id':'C2'}]}, callbacks))
        await asyncio.wait_for(entered.wait(), 1)
        assert len(calls) == 1 and not running.done()
        if outcome == 'cancel':
            running.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(running, 1)
            assert len(calls) == 1 and not saved
            return
        if kind == 'video':
            if outcome == 'save_failure':
                acknowledgement.set_exception(OSError('save acknowledgement failed'))
            else:
                saved.append('C1')
                acknowledgement.set_result(None)
        release.set()
        if outcome == 'save_failure':
            with pytest.raises(OSError, match='save acknowledgement failed'):
                await asyncio.wait_for(running, 1)
            assert len(calls) == 1
        else:
            result = await asyncio.wait_for(running, 1)
            assert [c['chapter_id'] for c in result['chapters']] == ['C1','C2']
            assert saved == ['C1','C2'] and len(calls) == 2
    asyncio.run(scenario())


@pytest.mark.parametrize('kind', ['video', 'document'])
@pytest.mark.parametrize('failure', ['rename', 'delete', 'commit'])
def test_shared_cleanup_retries_without_changing_completed_task(client, app, video_file, monkeypatch, kind, failure):
    if kind == 'video':
        source = upload(client, video_file)['id']
        table, pipeline, key = Task, app.state.pipeline, 'video_id'
    else:
        source = client.post('/api/v1/documents', files={'file':('cleanup.txt', b'cleanup material')}).json()['id']
        table, pipeline, key = DocumentTask, app.state.document_pipeline, 'document_id'
    identifier = 'completed-cleanup'
    with app.state.sessions.begin() as session:
        session.add(table(id=identifier, **{key:source}, status='succeeded',
                          config={'checkpoint_cleanup_pending':True}, result=None, updated_at='completed-time'))
    folder = app.state.current_settings.read().data_dir / ('videos' if kind == 'video' else 'documents') / source
    owned = checkpoint_path(folder, identifier, 4, 'chapters', 'a'*16)
    owned.write_text('completed checkpoint')
    other = checkpoint_path(folder, 'unrelated', 4, 'chapters', 'b'*16)
    other.write_text('keep')
    engine = app.state.sessions.kw['bind']
    def fail_commit(*args):
        raise RuntimeError('cleanup commit failed')
    with monkeypatch.context() as scoped:
        if failure == 'commit':
            event.listen(engine, 'commit', fail_commit)
        else:
            name = 'replace' if failure == 'rename' else 'unlink'
            original = getattr(Path, name)
            def fail(path, *args, **kwargs):
                if identifier in path.name:
                    raise PermissionError('cleanup file locked')
                return original(path, *args, **kwargs)
            scoped.setattr(Path, name, fail)
        try:
            pipeline.cleanup_checkpoints(identifier)
        finally:
            if failure == 'commit':
                event.remove(engine, 'commit', fail_commit)
    with app.state.sessions() as session:
        task = session.get(table, identifier)
        assert task.status == 'succeeded' and task.error is None and task.result is None
        assert task.updated_at == 'completed-time' and task.config['checkpoint_cleanup_pending']
    pipeline.retry_checkpoint_cleanup()
    with app.state.sessions() as session:
        task = session.get(table, identifier)
        assert task.updated_at == 'completed-time' and task.status == 'succeeded'
        assert 'checkpoint_cleanup_pending' not in task.config
    # 与清理器一致，只检查标准大小写的归属名称；Windows glob 默认还匹配非标准大写临时副本。
    assert not list(folder.glob(f'blocks-*-{identifier}-*', case_sensitive=True))
    assert other.read_text() == 'keep'


def test_generation_hashes_and_prompt_versions_are_unchanged():
    from app.videos.versions import digest, PROMPT_VERSION
    from app.documents.material import digest as document_digest
    from app.documents.versions import DOCUMENT_PROMPT_VERSION
    values = [{'中文':'测试','list':[1,None,True]}, {'z':2,'a':1}]
    assert [digest(v) for v in values] == ['e332c951ea0c05d3', 'c4f6fb7ac09c33cc']
    assert [document_digest(v) for v in values] == ['55bc093cd495598cbcb92ba4c519ffbeade0bbedca312055884fe248e8454d37', '99168216144c7fed5d4c54916cf98d9c66096280c04a499822a99b6658bd177a']
    assert PROMPT_VERSION == 'video-notes-v14-styles-characters-c24e0b290d1a'
    assert DOCUMENT_PROMPT_VERSION == 'document-v1-92b1182761d4d746'
