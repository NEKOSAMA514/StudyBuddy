import ctypes
import hashlib
import io
from concurrent.futures import ThreadPoolExecutor
from threading import Event, get_ident

import pytest
from PIL import Image
from pypdf.generic import NameObject, NumberObject

from app.documents import pdf, image_text
from app.documents.extraction import extract
from app.documents.image_text import ImageText
from app.errors import AppError, TaskInterrupted
from .pdf_samples import PdfSample, text_pdf
from .test_pdf_images import png


def test_every_native_call_and_close_is_locked_across_threads(tmp_path, monkeypatch):
    import pypdfium2.raw as raw
    calls = []
    for name in dir(raw):
        original = getattr(raw, name)
        if name.startswith('FPDF') and isinstance(original, ctypes._CFuncPtr):
            def guarded(*args, _name=name, _original=original):
                assert pdf.PDFIUM_LOCK._is_owned(), _name
                calls.append((_name, get_ident()))
                return _original(*args)
            monkeypatch.setattr(raw, name, guarded)
    path = tmp_path / 'concurrent.pdf'
    path.write_bytes(text_pdf(['concurrent text']))
    with ThreadPoolExecutor(2) as executor:
        results = list(executor.map(lambda _: extract(path, 'pdf'), range(8)))
    assert all(r['units'][0]['text'] == 'concurrent text' for r in results)
    assert len({thread for name, thread in calls}) == 2
    for name in ('FPDF_CloseDocument', 'FPDF_ClosePage', 'FPDFText_ClosePage'):
        assert sum(called == name for called, _ in calls) == 8


def test_lock_wait_is_cancellable_and_does_not_release_another_owner():
    entered, stop = Event(), Event()
    checks = []
    def check():
        checks.append(1)
        entered.set()
        if stop.is_set():
            raise TaskInterrupted()
    def waiting():
        with pdf.pdfium_lock(check):
            pytest.fail('cancelled waiter acquired lock')
    with ThreadPoolExecutor(1) as executor:
        with pdf.pdfium_lock():
            future = executor.submit(waiting)
            assert entered.wait(2)
            stop.set()
            with pytest.raises(TaskInterrupted):
                future.result(timeout=2)
            assert pdf.PDFIUM_LOCK._is_owned()
    assert len(checks) >= 2


def test_slow_ocr_releases_native_lock_and_other_documents_can_finish(tmp_path, monkeypatch):
    path = tmp_path / 'images.pdf'
    sample = PdfSample()
    sample.text(sample.page(), 'Body')
    sample.image(sample.writer.pages[0], (0,40,40,70), png())
    sample.save(path)
    other = tmp_path / 'text.pdf'
    other.write_bytes(text_pdf(['other document']))
    entered, release = Event(), Event()
    def recognize(*args):
        assert not pdf.PDFIUM_LOCK._is_owned()
        entered.set()
        assert release.wait(5)
        return 'OCR text', ''
    monkeypatch.setattr(ImageText, 'recognize', recognize)
    with ThreadPoolExecutor(2) as executor:
        running = executor.submit(extract, path, 'pdf')
        try:
            assert entered.wait(2)
            assert executor.submit(extract, other, 'pdf').result(timeout=2)['units'][0]['text'] == 'other document'
        finally:
            release.set()
        assert 'OCR text' in running.result(timeout=2)['units'][0]['text']


def test_cancel_during_text_read_closes_native_resources(tmp_path, monkeypatch):
    import pypdfium2 as pdfium
    path = tmp_path / 'cancel.pdf'
    path.write_bytes(text_pdf(['x' * 600]))
    before = path.read_bytes()
    stop, opened = Event(), []
    original_open, original_unicode = pdfium.PdfDocument, pdfium.raw.FPDFText_GetUnicode
    def open_document(*args, **kwargs):
        document = original_open(*args, **kwargs)
        opened.append(document)
        return document
    def unicode(*args):
        stop.set()
        return original_unicode(*args)
    def check():
        if stop.is_set():
            raise TaskInterrupted()
    monkeypatch.setattr(pdfium, 'PdfDocument', open_document)
    monkeypatch.setattr(pdfium.raw, 'FPDFText_GetUnicode', unicode)
    with pytest.raises(TaskInterrupted):
        extract(path, 'pdf', ImageText(check=check))
    assert opened and all(d.raw is None for d in opened)
    assert path.read_bytes() == before
    path.rename(tmp_path / 'released.pdf')


@pytest.mark.parametrize('target', ['base', 'mask', 'combined'])
def test_encoded_dimensions_are_checked_before_pixel_decoding(tmp_path, monkeypatch, target):
    sample = PdfSample()
    page = sample.page()
    def jpeg(size):
        output = io.BytesIO()
        Image.new('L', size, 128).save(output, 'JPEG')
        ref = sample.stream(output.getvalue(), Type=NameObject('/XObject'), Subtype=NameObject('/Image'),
            Width=NumberObject(1), Height=NumberObject(1), BitsPerComponent=NumberObject(8),
            ColorSpace=NameObject('/DeviceGray'), Filter=NameObject('/DCTDecode'))
        return ref
    base = jpeg((8,2) if target == 'combined' else (8,6) if target == 'base' else (2,2))
    mask = jpeg((2,8) if target == 'combined' else (8,6) if target == 'mask' else (2,2))
    base.get_object()[NameObject('/SMask')] = mask
    sample.image(page, (0,0,20,20), reference=base)
    path = tmp_path / 'header.pdf'
    sample.save(path)
    monkeypatch.setattr(image_text, 'MAX_IMAGE_PIXELS', 20)
    monkeypatch.setattr(pdf, 'decode_plane', lambda *_: pytest.fail('pixel decoding preceded size validation'))
    with pytest.raises(AppError) as error:
        extract(path, 'pdf')
    assert error.value.code == 'document_limit'


def test_real_corrupt_drawn_image_keeps_body_and_valid_image(tmp_path, monkeypatch):
    sample = PdfSample()
    page = sample.page()
    sample.text(page, 'Keep body')
    bad = sample.image(page, (0,40,40,70), png())
    bad.get_object().set_data(b'corrupt JPEG')
    bad.get_object()[NameObject('/Filter')] = NameObject('/DCTDecode')
    sample.image(page, (50,40,90,70), png('blue'))
    path = tmp_path / 'corrupt.pdf'
    sample.save(path)
    calls = []
    monkeypatch.setattr(ImageText, 'recognize', lambda self, raw: (calls.append(raw) or 'valid image', ''))
    result = extract(path, 'pdf')
    assert 'Keep body' in result['units'][0]['text'] and 'valid image' in result['units'][0]['text']
    assert len(calls) == 1
    assert sum('图片1：图片格式无法读取' in w for w in result['warnings']) == 1


def test_pdfium_decodes_image_when_pypdf_cannot(tmp_path, monkeypatch):
    from pypdf.generic import _image_xobject
    sample = PdfSample()
    sample.image(sample.page(), (0,0,40,30), png('blue'))
    path = tmp_path / 'decoder.pdf'
    sample.save(path)
    def unavailable(*args, **kwargs):
        raise ValueError('unsupported filter in pypdf')
    monkeypatch.setattr(_image_xobject, '_xobj_to_image', unavailable)
    seen = []
    def recognize(self, raw):
        with Image.open(io.BytesIO(raw)) as image:
            assert image.convert('RGBA').tobytes() == bytes((0, 0, 255, 255)) * 12
            seen.append(image.size)
        return 'decoded', ''
    monkeypatch.setattr(ImageText, 'recognize', recognize)
    assert 'decoded' in extract(path, 'pdf')['units'][0]['text']
    assert seen == [(4,3)]


def test_optional_cache_key_preserves_default_behavior_and_image_quota(monkeypatch):
    images = ImageText()
    calls = []
    monkeypatch.setattr(ImageText, 'recognize', lambda self, raw: (calls.append(raw) or 'text', ''))
    images.read(b'old encoding', 'old')
    old = hashlib.sha256(b'old encoding').hexdigest()
    monkeypatch.setattr(image_text, 'MAX_IMAGES', 1)
    assert 'text' in images.read(b'new encoding', 'new', cache_key=old)
    assert len(images.cache) == 1 and calls == [b'old encoding']
    with pytest.raises(AppError) as error:
        images.read(b'another image', 'another')
    assert error.value.code == 'document_limit'


@pytest.mark.parametrize('algorithm,password', [('RC4-128','private'), ('AES-128','private'), ('AES-256',''), ('AES-256','private')])
def test_encrypted_pdf_is_rejected_even_with_empty_user_password(tmp_path, algorithm, password):
    sample = PdfSample()
    sample.page()
    sample.writer.encrypt(password, 'owner', algorithm=algorithm)
    path = tmp_path / 'encrypted.pdf'
    sample.save(path)
    with pytest.raises(AppError) as error:
        extract(path, 'pdf')
    assert error.value.code == 'encrypted_document'


def test_image_limit_stops_before_decoding_later_images(tmp_path, monkeypatch):
    sample = PdfSample()
    page = sample.page()
    for i, color in enumerate(['red','blue','green','yellow']):
        sample.image(page,(i*40,0,(i+1)*40,30),png(color))
    path = tmp_path/'many-images.pdf';sample.save(path)
    calls, decoded = [], []
    original = pdf.image_data
    def decode(image):
        decoded.append(1)
        return original(image)
    monkeypatch.setattr(pdf,'image_data',decode)
    monkeypatch.setattr(image_text,'MAX_IMAGES',2)
    monkeypatch.setattr(ImageText,'recognize',lambda self,raw:(calls.append(raw) or 'text',''))
    with pytest.raises(AppError) as error:
        extract(path,'pdf')
    assert error.value.code == 'document_limit'
    assert len(calls) == 2 and len(decoded) == 3


def test_non_bmp_private_char_filter_uses_combined_unicode_and_alpha(tmp_path, monkeypatch):
    sample=PdfSample();page=sample.page(300,300)
    sample.text(page,'保留\U000f0001正文',x=20,y=100,opacity=0)
    sample.image(page,(10,80,160,120),png())
    path=tmp_path/'private-plane.pdf';sample.save(path)
    monkeypatch.setattr(ImageText,'recognize',lambda *_:('',''))
    result=extract(path,'pdf')
    assert result['units'][0]['text'].replace(' ','') == '保留正文'
    assert sum('隐藏异常字符' in w for w in result['warnings']) == 1
