"""Executable dependency rules for the shared analysis and recognition layers."""
import ast
import importlib.util
from pathlib import Path
import subprocess
import sys
import pytest

ROOT = Path(__file__).parents[1] / "app"


def imports(path):
    module = "app." + ".".join(path.relative_to(ROOT).with_suffix("").parts)
    package = module.removesuffix(".__init__") if path.name == "__init__.py" else module.rsplit(".", 1)[0]
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8-sig"))):
        if isinstance(node, ast.ImportFrom):
            yield importlib.util.resolve_name("." * node.level + (node.module or ""), package) if node.level else node.module
        elif isinstance(node, ast.Import):
            yield from (name.name for name in node.names)


def test_shared_layers_do_not_import_source_specific_business():
    for folder in ["analysis", "recognition", "util"]:
        for path in (ROOT / folder).rglob("*.py"):
            assert not any(name and name.startswith(("app.videos", "app.documents")) for name in imports(path)), path
    for path in (ROOT / "documents").rglob("*.py"):
        assert not any(name and name.startswith("app.videos") for name in imports(path)), path


@pytest.mark.parametrize('folder', ['documents', 'video_download'])
def test_business_modules_have_no_import_cycle_including_lazy_imports(folder):
    graph = {"app." + folder + "." + path.stem: set(imports(path)) for path in (ROOT / folder).glob("*.py")}
    visited = set()

    def visit(name, active):
        assert name not in active, active + [name]
        if name in visited:
            return
        for dependency in graph[name]:
            if dependency in graph:
                visit(dependency, active + [name])
        visited.add(name)
    for name in graph:
        visit(name, [])


def test_storage_process_and_concise_dependencies_are_explicit():
    assert not set(imports(ROOT / 'documents/store.py')) & {
        'app.documents.service', 'app.documents.imports', 'app.documents.tasks'}
    assert not any(name and name.startswith('app.video_download.')
                   for name in imports(ROOT / 'video_download/process.py'))
    for name in ('videos/concise.py', 'documents/concise.py', 'videos/analysis_service.py'):
        assert not any(dependency and dependency.endswith('.detailed') for dependency in imports(ROOT / name)), name
    for path in [ROOT/'main.py', *list((ROOT/'documents').glob('*.py')), ROOT/'analysis/llm.py', ROOT/'videos/detailed.py']:
        for node in ast.walk(ast.parse(path.read_text(encoding='utf-8-sig'))):
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    for child in ast.walk(target):
                        assert not (isinstance(child, ast.Attribute) and child.attr == 'extractions'), path
                        assert not (isinstance(child, ast.Attribute) and isinstance(child.value, ast.Name)
                                    and child.value.id == 'validate' and child.attr in {'chapter','accept'}), path


def test_document_execution_and_material_do_not_import_task_orchestration():
    for name in ("pipeline", "concise", "detailed", "material"):
        assert "app.documents.tasks" not in set(imports(ROOT / "documents" / f"{name}.py")), name
    material_imports = set(imports(ROOT / "documents" / "material.py"))
    assert not material_imports & {"app.documents.model", "app.documents.concise"}


def test_shared_client_and_tools_import_without_business_or_heavy_recognizers():
    subprocess.run([sys.executable, "-c", "import sys; from app.analysis.llm import ModelClient; from app.recognition.media import Recognizer; from app.util.file_ingest import save_stream; from app.util.media_process import command; assert not any(n.startswith(('app.videos', 'app.documents', 'rapidocr', 'faster_whisper')) for n in sys.modules)"], check=True)
