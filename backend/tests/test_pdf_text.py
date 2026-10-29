import io
import pytest
from pypdf.generic import NameObject, FloatObject, IndirectObject

from app.documents.extraction import extract
from app.documents.pdf import PdfDocument
from .pdf_samples import PdfSample, dictionary


def sample_pdf(*, nested=False, font_name="SymbolMT", hidden=False, overlap=True, extra=False):
    sample = PdfSample()
    page = sample.page(300, 300)
    cmap = sample.stream(b"""/CIDInit /ProcSet findresource begin 12 dict begin begincmap
/CMapType 2 def 1 begincodespacerange <00> <FF> endcodespacerange
2 beginbfchar
<6F> <F07F>
<F06E> <0041>
endbfchar
2 beginbfrange
<6D> <6E> <F06D>
<6B> <6C> [<F078> <F0E5>]
endbfrange
endcmap end end""")
    font = sample.writer._add_object(dictionary(Type=NameObject('/Font'), Subtype=NameObject('/Type1'),
        BaseFont=NameObject('/ABCDEF+' + font_name), Encoding=NameObject('/WinAnsiEncoding'), ToUnicode=cmap))
    name = sample.resource(page, 'Font', font)
    prefix = ''
    if hidden:
        opacity = 0.5 if hidden == 'partial' else 0
        state = sample.writer._add_object(dictionary(ca=FloatObject(opacity), CA=FloatObject(opacity)))
        state_name = sample.resource(page, 'ExtGState', state)
        prefix = f'{state_name} gs '
    value = '416D6E6B6C6F41' if extra else '6D6E6B6C6F'
    sample.append(page, f'q {prefix}BT {name} 12 Tf 20 100 Td <{value}> Tj ET Q'.encode())
    if extra:
        from .test_pdf_images import png
        sample.image(page, (10,170,150,210) if overlap else (10,20,150,60), png())
        sample.text(page, 'Hidden normal text', x=20, y=195, size=11, opacity=0)
    if nested:
        target = sample.page(300, 300)
        sample.form(target, page)
        sample.writer.remove_page(0)
    return sample.bytes()


def read(raw, tmp_path):
    path = tmp_path / 'sample.pdf'
    path.write_bytes(raw)
    with PdfDocument(path) as document:
        return document.read_page(0)


@pytest.mark.parametrize('nested', [False, True])
@pytest.mark.parametrize('font', ['Symbol', 'SymbolMT'])
def test_symbol_pua_is_repaired_in_text_spans_without_modifying_pdf(nested, font, tmp_path):
    raw = sample_pdf(nested=nested, font_name=font)
    page = read(raw, tmp_path)
    repaired = page['text']
    assert repaired == 'μνξ∑\uf07f'
    assert repaired.count('μνξ∑') == 1
    path = tmp_path / 'sample.pdf'
    result = extract(path, 'pdf')
    assert result['units'][0]['text'] == repaired
    assert path.read_bytes() == raw


def test_other_fonts_with_identical_pua_are_not_changed(tmp_path):
    assert read(sample_pdf(font_name='Wingdings'), tmp_path)['text'] == '\uf06d\uf06e\uf078\uf0e5\uf07f'


@pytest.mark.parametrize('hidden,overlap,font', [
    (True, True, 'Calibri'), (False, True, 'Calibri'),
    (True, False, 'Calibri'), (True, True, 'SymbolMT'), ('partial', True, 'Calibri'),
])
def test_only_hidden_unknown_characters_over_images_are_removed(tmp_path, monkeypatch, hidden, overlap, font):
    from app.documents.image_text import ImageText
    path = tmp_path / 'hidden.pdf'
    path.write_bytes(sample_pdf(font_name=font, hidden=hidden, overlap=overlap, extra=True))
    before = path.read_bytes()
    monkeypatch.setattr(ImageText, 'recognize', lambda *_: ('', '未识别到文字。'))
    result = extract(path, 'pdf')
    text = result['units'][0]['text']
    removed = hidden is True and overlap
    assert 'Hidden normal text' in text and text.count('A') == 2
    assert ('\uf07f' not in text) == removed
    assert ('μνξ∑' in text) == (font == 'SymbolMT')
    warnings = [w for w in result['warnings'] if '隐藏异常字符' in w]
    assert len(warnings) == int(removed)
    if warnings:
        assert warnings[0].startswith('第 1 页') and warnings[0] not in text
    assert '未识别到文字' not in text and result['character_count'] == len(text)
    from app.documents.material import split_material
    material, _segments = split_material(result)
    assert '隐藏异常字符' not in str(material) and '未识别到文字' not in str(material)
    assert ''.join(fragment['text'] for fragment in material) == text
    assert path.read_bytes() == before


def test_failed_image_enumeration_keeps_text_and_warns(tmp_path, monkeypatch):
    path = tmp_path / 'enumeration.pdf'
    path.write_bytes(sample_pdf(font_name='Calibri'))
    def fail(*args, **kwargs):
        raise ValueError('cannot enumerate')
    monkeypatch.setattr('app.documents.pdf.drawn_images', fail)
    result = extract(path, 'pdf')
    assert '\uf07f' in result['units'][0]['text']
    assert sum('图片信息无法读取' in w for w in result['warnings']) == 1
    assert not any('隐藏异常字符' in w for w in result['warnings'])


def test_reading_symbol_text_never_changes_pdf_objects(tmp_path):
    path = tmp_path / 'unchanged.pdf'
    path.write_bytes(sample_pdf())
    with PdfDocument(path) as document:
        def snapshot():
            objects = {}
            for generation, entries in document.reader.xref.items():
                for identifier in entries:
                    if identifier:
                        stream = io.BytesIO()
                        document.reader.get_object(IndirectObject(identifier, generation, document.reader)).write_to_stream(stream)
                        objects[identifier, generation] = stream.getvalue()
            return repr(document.reader.trailer), objects
        before = snapshot()
        document.read_page(0)
        assert snapshot() == before
    assert path.read_bytes() == sample_pdf()


def test_sorted_chinese_text_and_text_outside_page_are_retained(tmp_path):
    path = tmp_path / 'ordered.pdf'
    sample = PdfSample()
    page = sample.page(300,300)
    sample.text(page, 'Second block', y=120)
    sample.text(page, '中文知识', y=60)
    sample.text(page, 'Outside page', x=320, y=180)
    sample.save(path)
    result = extract(path, 'pdf')
    text = result['units'][0]['text']
    assert '中文知识' in text.replace(' ', '')
    assert text.index('中文') < text.index('Second block') < text.index('Outside page')
    assert result['units'][0]['id'] == 'D1'
    assert result['units'][0]['locator'] == {'page': 1}
    assert result['character_count'] == len(text)


@pytest.mark.parametrize('angle', [0, 45, 90, 180, 225, 270])
@pytest.mark.parametrize('nested', [False, True])
def test_rotated_baselines_keep_character_and_line_order(tmp_path, angle, nested):
    import math
    sample = PdfSample()
    page = sample.page(300, 300)
    sample.text(page, 'CHAPTER', x=60, y=120)
    sample.text(page, 'SECOND LINE', x=60, y=140)
    cosine, sine = round(math.cos(math.radians(angle)), 9), round(math.sin(math.radians(angle)), 9)
    matrix = (cosine, sine, -sine, cosine, 150 - 150*cosine + 150*sine, 150 - 150*sine - 150*cosine)
    if nested:
        target = sample.page(300, 300)
        sample.form(target, page, matrix)
        sample.writer.remove_page(0)
    else:
        content = page.get_contents().get_data()
        page[NameObject('/Contents')] = sample.stream(('q ' + ' '.join(map(str, matrix)) + ' cm\n').encode() + content + b'\nQ')
    path = tmp_path / 'rotated.pdf'
    sample.save(path)
    original = path.read_bytes()
    result = extract(path, 'pdf')
    text = result['units'][0]['text']
    assert text.splitlines() == ['CHAPTER', 'SECOND LINE']
    assert result['units'][0]['locator'] == {'page': 1}
    assert result['character_count'] == len(text)
    assert path.read_bytes() == original


@pytest.mark.parametrize('angle', [0, 90, 180, 270])
def test_rotated_baseline_sorts_glyph_positions_instead_of_drawing_order(tmp_path, angle):
    import math
    sample = PdfSample()
    page = sample.page(300, 300)
    for index, letter in reversed(list(enumerate('CHAPTER'))):
        sample.text(page, letter, x=60 + index*12, y=120)
    cosine, sine = round(math.cos(math.radians(angle)), 9), round(math.sin(math.radians(angle)), 9)
    matrix = (cosine, sine, -sine, cosine, 150 - 150*cosine + 150*sine, 150 - 150*sine - 150*cosine)
    content = page.get_contents().get_data()
    page[NameObject('/Contents')] = sample.stream(('q ' + ' '.join(map(str, matrix)) + ' cm\n').encode() + content + b'\nQ')
    path = tmp_path / 'reverse-draw.pdf'
    sample.save(path)
    text = extract(path, 'pdf')['units'][0]['text']
    assert ''.join(text.split()) == 'CHAPTER'
    assert len(text.splitlines()) == 1


def test_non_bmp_unicode_is_not_truncated(tmp_path):
    sample = PdfSample()
    sample.text(sample.page(), '中文𠀀𝛼😀末尾')
    path = tmp_path / 'unicode.pdf'
    sample.save(path)
    assert extract(path, 'pdf')['units'][0]['text'].replace(' ', '') == '中文𠀀𝛼😀末尾'


def test_pdf_import_and_current_fixture_work_without_pymupdf(tmp_path):
    import subprocess
    import sys
    from pathlib import Path
    program = '''
import importlib.abc
import sys
from pathlib import Path
class BlockLegacy(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split(".")[0] in {"pymupdf", "fitz", "pymupdf4llm", "pymupdf_layout"}:
            raise ImportError("legacy package is unavailable")
sys.meta_path.insert(0, BlockLegacy())
from tests.document_samples import sample
from app.documents.extraction import extract
path = Path(sys.argv[1])
path.write_bytes(sample("pdf"))
result = extract(path, "pdf")
assert result["units"][0]["text"] == "First page evidence"
assert result["units"][1]["locator"] == {"page": 2}
'''
    completed = subprocess.run([sys.executable, '-c', program, str(tmp_path / 'isolated.pdf')],
                               cwd=Path(__file__).parents[1], capture_output=True, text=True)
    assert completed.returncode == 0, completed.stderr
