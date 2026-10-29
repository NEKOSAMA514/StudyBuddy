import pytest
from app.config import Settings
from app.db import Document, database
from app.main import create_app


@pytest.mark.parametrize("existing,expected", [
    ([], "studybuddy.sqlite3"),
    (["studynote.sqlite3"], "studynote.sqlite3"),
    (["studybuddy.sqlite3"], "studybuddy.sqlite3"),
    (["studynote.sqlite3", "studybuddy.sqlite3"], "studybuddy.sqlite3"),
])
def test_project_name_and_current_database(tmp_path, existing, expected):
    data = tmp_path / "data"
    data.mkdir()
    for filename in existing:
        engine, sessions = database(data / filename)
        with sessions() as session:
            session.add(Document(id=filename, fingerprint=filename, filename="已有资料.txt", format="txt",
                                 size_bytes=1, unit_count=1, character_count=1, warnings=[]))
            session.commit()
        engine.dispose()
    app = create_app(Settings(data_dir=data, model_dir=tmp_path / "models", log_dir=tmp_path / "log",
                              static_dir=tmp_path / "absent"), start_worker=False)
    assert app.title.startswith("StudyBuddy")
    with app.state.sessions() as session:
        assert session.get_bind().url.database == str(data / expected)
        if existing:
            assert session.get(Document, expected).filename == "已有资料.txt"
        session.get_bind().dispose()
    assert all((data / filename).is_file() for filename in existing)
    if existing == ["studynote.sqlite3"]:
        assert not (data / "studybuddy.sqlite3").exists()
