import io
import json
from pathlib import Path

from pypdf.generic import NameObject
from .pdf_samples import PdfSample
import pytest
from PIL import Image

from app.documents.extraction import extract
from app.documents.image_text import ImageText
from app.errors import AppError


def png(color="red"):
    stream = io.BytesIO()
    Image.new("RGB", (4, 3), color).save(stream, "PNG")
    return stream.getvalue()


def test_only_drawn_images_are_read_and_same_page_duplicates_keep_first_order(tmp_path, monkeypatch):
    path = tmp_path / "resources.pdf"
    document = PdfSample()
    page = document.page()
    document.text(page, "Page one")
    used = document.image(page, (0,40,40,70), png())
    document.image(page, (50,40,90,70), reference=used)
    document.image(page, (100,40,140,70), png("blue"))
    unused = document.image_object(png("green"))
    document.resource(page, 'XObject', unused)
    unused.get_object().set_data(b'damaged unused image')
    unused.get_object()[NameObject('/Filter')] = NameObject('/DCTDecode')
    document.image(document.page(), (0,0,40,30), reference=used)
    document.save(path)
    calls = []
    def recognize(self, raw):
        with Image.open(io.BytesIO(raw)) as image:
            color = image.convert("RGB").getpixel((0, 0))
        calls.append(color)
        return str(color), ""
    monkeypatch.setattr(ImageText, "recognize", recognize)
    result = extract(path, "pdf")
    assert calls == [(255, 0, 0), (0, 0, 255)]
    assert result["units"][0]["text"].count("(255, 0, 0)") == 1
    assert result["units"][0]["text"].index("(255, 0, 0)") < result["units"][0]["text"].index("(0, 0, 255)")
    assert "(255, 0, 0)" in result["units"][1]["text"]
    assert [u["locator"] for u in result["units"]] == [{"page": 1}, {"page": 2}]
    assert not any("无法读取" in warning for warning in result["warnings"])


def test_drawn_images_inside_forms_are_kept(tmp_path, monkeypatch):
    path = tmp_path / "form.pdf"
    document = PdfSample()
    source = document.page(100,100)
    document.text(source, "Form text", x=10, y=20)
    document.image(source, (0,30,40,60), png())
    inner = document.page(100,100)
    document.form(inner, source)
    page = document.page()
    document.form(page, inner)
    document.form(page, inner, (1,0,0,1,100,0))
    document.writer.remove_page(0)
    document.writer.remove_page(0)
    document.save(path)
    calls = []
    monkeypatch.setattr(ImageText, "recognize", lambda self, raw: (calls.append(raw) or "Form image", ""))
    text = extract(path, "pdf")["units"][0]["text"]
    assert "Form text" in text and text.count("Form image") == 1 and len(calls) == 1


def test_inline_image_uses_original_pixels_not_page_render(tmp_path, monkeypatch):
    path = tmp_path / "inline.pdf"
    document = PdfSample()
    page = document.page(300,300)
    document.text(page, "Inline page")
    document.append(page, b"q 100 0 0 100 0 0 cm BI /W 2 /H 2 /BPC 8 /CS /RGB /F /AHx ID FF000000FF000000FFFFFFFF> EI Q")
    document.save(path)
    calls = []
    def recognize(self, raw):
        with Image.open(io.BytesIO(raw)) as image:
            rgb = image.convert("RGB")
            calls.append((image.size, [rgb.getpixel((x, y)) for y in range(2) for x in range(2)]))
        return "Inline image", ""
    monkeypatch.setattr(ImageText, "recognize", recognize)
    text = extract(path, "pdf")["units"][0]["text"]
    assert "Inline page" in text and "Inline image" in text
    assert calls == [((2, 2), [(255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 255, 255)])]


def test_transparent_image_preserves_soft_mask_for_ocr(tmp_path, monkeypatch):
    path = tmp_path / "transparent.pdf"
    stream = io.BytesIO()
    image = Image.new("RGBA", (2, 2))
    image.putdata([(0, 0, 0, 0), (0, 0, 0, 255), (0, 0, 0, 128), (255, 0, 0, 255)])
    image.save(stream, "PNG")
    document = PdfSample()
    document.image(document.page(), (0,0,20,20), stream.getvalue())
    document.save(path)
    calls = []
    def recognize(self, raw):
        with Image.open(io.BytesIO(raw)) as image:
            assert image.mode == "RGBA"
            alpha = image.getchannel("A")
            calls.append([alpha.getpixel((x, y)) for y in range(2) for x in range(2)])
            background = Image.new("RGB", image.size, "white")
            background.paste(image, mask=image.getchannel("A"))
            assert [background.getpixel((x, y)) for y in range(2) for x in range(2)] == [(255, 255, 255), (0, 0, 0), (127, 127, 127), (255, 0, 0)]
        return "Transparent image", ""
    monkeypatch.setattr(ImageText, "recognize", recognize)
    assert "Transparent image" in extract(path, "pdf")["units"][0]["text"]
    assert calls == [[0, 255, 128, 255]]


def masked_pdf(path, base_size, mask_size, mode="RGB", alpha=128):
    base, mask = io.BytesIO(), io.BytesIO()
    Image.new(mode, base_size, 0).save(base, "PNG")
    Image.new("L", mask_size, alpha).save(mask, "PNG")
    document = PdfSample()
    page = document.page()
    document.text(page, "Masked image page")
    image = document.image(page, (10,50,90,110), base.getvalue())
    mask_xref = document.image_object(mask.getvalue())
    image.get_object()[NameObject('/SMask')] = mask_xref
    document.save(path)
    return document, image, mask_xref


@pytest.mark.parametrize("base_size,mask_size,mode", [
    ((2, 2), (8, 6), "L"), ((8, 6), (2, 2), "RGB"), ((8, 2), (2, 6), "RGB"),
])
def test_different_mask_sizes_preserve_resolution_alpha_and_pixels(tmp_path, monkeypatch, base_size, mask_size, mode):
    path = tmp_path / "different-sizes.pdf"
    masked_pdf(path, base_size, mask_size, mode)
    calls = []
    def recognize(self, raw):
        with Image.open(io.BytesIO(raw)) as image:
            calls.append(image.size)
            assert image.mode in {"LA", "RGBA"} and image.size == (8, 6)
            rgba = image.convert("RGBA")
            assert {rgba.getpixel((x, y)) for y in range(6) for x in range(8)} == {(0, 0, 0, 128)}
        return "Mask text", ""
    monkeypatch.setattr(ImageText, "recognize", recognize)
    result = extract(path, "pdf")
    assert calls == [(8, 6)] and "Mask text" in result["units"][0]["text"]
    assert not any("无法读取" in warning for warning in result["warnings"])


@pytest.mark.parametrize("sizes", [((2, 2), (8, 6)), ((8, 2), (2, 8))])
def test_mask_and_combined_pixel_limits_are_checked_before_decoding(tmp_path, monkeypatch, sizes):
    from app.documents.pdf import PdfDocument
    from app.documents import image_text, pdf
    path = tmp_path / "large-mask.pdf"
    masked_pdf(path, *sizes)
    original = path.read_bytes()
    monkeypatch.setattr(image_text, "MAX_IMAGE_PIXELS", 20)
    monkeypatch.setattr(pdf, 'decode_plane', lambda *_: pytest.fail("pixels decoded before size check"))
    with PdfDocument(path) as document:
        with pytest.raises(AppError) as error:
            list(document.read_page(0)["images"])
    assert error.value.code == "document_limit" and path.read_bytes() == original
    path.rename(tmp_path / "closed.pdf")


@pytest.mark.parametrize("outcome", ["decode_failure", "ocr_failure", "empty", "success"])
def test_repeated_images_are_processed_once_even_when_unsuccessful(tmp_path, monkeypatch, outcome):
    from app.documents import pdf
    path = tmp_path / "repeated.pdf"
    document = PdfSample()
    page = document.page()
    document.text(page, "Body text")
    xref = document.image(page, (10,50,50,80), png())
    document.image(page, (60,50,100,80), reference=xref)
    document.image(page, (110,50,150,80), png("blue"))
    document.save(path)
    decoded, recognized = [], []
    original = pdf.image_data
    def decode(image):
        decoded.append(image.indirect_reference.idnum)
        if image.indirect_reference.idnum == xref.idnum and outcome == "decode_failure":
            raise ValueError("cannot decode")
        return original(image)
    def recognize(self, raw):
        recognized.append(raw)
        return {"ocr_failure": ("", "本地 OCR 读取失败。"), "empty": ("", "未识别到文字。")}.get(outcome, ("Text", ""))
    monkeypatch.setattr(pdf, "image_data", decode)
    monkeypatch.setattr(ImageText, "recognize", recognize)
    result = extract(path, "pdf")
    assert decoded.count(xref.idnum) == 1 and len(decoded) == 2
    assert len(recognized) == (1 if outcome == "decode_failure" else 2)
    problems = [w for w in result["warnings"] if "图片1：" in w]
    assert len(problems) == (0 if outcome == "success" else 1)
    assert not any("图片3" in w for w in result["warnings"])


def test_large_mask_keeps_its_shape_and_distinct_masks_are_not_merged(tmp_path, monkeypatch):
    path = tmp_path / "distinct-masks.pdf"
    document, image_xref, mask_xref = masked_pdf(path, (2,2), (8,6))
    mask_xref.get_object().set_data(bytes([0]*24 + [255]*24))
    from copy import deepcopy
    other_mask = deepcopy(mask_xref.get_object())
    del other_mask.indirect_reference
    other_mask.set_data(bytes([255]*24 + [0]*24))
    other_mask = document.writer._add_object(other_mask)
    other_image = deepcopy(image_xref.get_object())
    del other_image.indirect_reference
    other_image[NameObject('/SMask')] = other_mask
    document.image(document.writer.pages[0], (200,50,280,110), reference=document.writer._add_object(other_image))
    document.save(path)
    alphas = []
    def recognize(self, raw):
        with Image.open(io.BytesIO(raw)) as image:
            assert image.size == (8, 6)
            rgba = image.convert("RGBA")
            alphas.append([rgba.getpixel((x, y))[3] for y in range(6) for x in range(8)])
        return f"Shape {len(alphas)}", ""
    monkeypatch.setattr(ImageText, "recognize", recognize)
    result = extract(path, "pdf")
    assert alphas == [[0] * 24 + [255] * 24, [255] * 24 + [0] * 24]
    assert "Shape 1" in result["units"][0]["text"] and "Shape 2" in result["units"][0]["text"]
    assert not any("无法读取" in w for w in result["warnings"])


# 固定旧实现输出，避免新插值与合成同时变化后让两侧断言一起漂移。
@pytest.mark.parametrize("case", json.loads((Path(__file__).parent / "fixtures/pdf-mask-pixels.json").read_text("utf-8")))
def test_nonuniform_mask_pixels_match_recorded_baseline(tmp_path, monkeypatch, case):
    document = PdfSample()
    page = document.page()
    base_size, mask_size = tuple(case["base"]), tuple(case["mask"])
    base = Image.new("RGB", base_size)
    base.putdata([((x*73) % 256, (y*127) % 256, ((x+y)*61) % 256)
                  for y in range(base_size[1]) for x in range(base_size[0])])
    raw = io.BytesIO()
    base.save(raw, "PNG")
    reference = document.image(page, (0, 0, 80, 60), raw.getvalue())
    mask = Image.new("L", mask_size)
    mask.putdata([(x*31+y*47) % 256 for y in range(mask_size[1]) for x in range(mask_size[0])])
    raw = io.BytesIO()
    mask.save(raw, "PNG")
    reference.get_object()[NameObject("/SMask")] = document.image_object(raw.getvalue())
    path = tmp_path / "gradient.pdf"
    document.save(path)
    calls = []
    def recognize(self, raw):
        with Image.open(io.BytesIO(raw)) as image:
            assert image.size == tuple(case["size"])
            assert image.convert("RGBA").tobytes().hex() == case["rgba"]
        calls.append(raw)
        return "Gradient image", ""
    monkeypatch.setattr(ImageText, "recognize", recognize)
    assert "Gradient image" in extract(path, "pdf")["units"][0]["text"]
    assert len(calls) == 1


@pytest.mark.parametrize("failure", [False, True])
def test_repeated_inline_images_are_deduplicated_before_decode(tmp_path, monkeypatch, failure):
    from app.documents import pdf
    path = tmp_path / "inline-repeat.pdf"
    document = PdfSample()
    page = document.page()
    document.text(page, "Inline body")
    image = b"BI /W 2 /H 2 /BPC 8 /CS /RGB /F /AHx ID FF000000FF000000FFFFFFFF> EI"
    document.append(page, b"q 40 0 0 40 0 50 cm " + image + b" Q q 40 0 0 40 50 50 cm " + image + b" Q")
    document.save(path)
    decoded = []
    original = pdf.image_data
    def decode(image):
        decoded.append(image)
        if failure:
            raise ValueError("inline decode failure")
        return original(image)
    monkeypatch.setattr(pdf, "image_data", decode)
    monkeypatch.setattr(ImageText, "recognize", lambda *_: ("Inline OCR", ""))
    result = extract(path, "pdf")
    assert len(decoded) == 1
    assert "Inline body" in result["units"][0]["text"]
    assert result["units"][0]["text"].count("Inline OCR") == int(not failure)
    assert len([w for w in result["warnings"] if "图片1：图片格式无法读取" in w]) == int(failure)


def test_bad_image_does_not_drop_text_or_other_images(tmp_path, monkeypatch):
    from app.documents import pdf
    path = tmp_path / "mixed.pdf"
    document = PdfSample()
    page = document.page()
    document.text(page, "Valid text")
    bad = document.image(page, (0,40,40,70), png())
    document.image(page, (50,40,90,70), png("blue"))
    document.save(path)
    original = pdf.image_data
    def decode(image):
        if image.indirect_reference.idnum == bad.idnum:
            raise ValueError("isolated decoder failure")
        return original(image)
    monkeypatch.setattr(pdf, "image_data", decode)
    monkeypatch.setattr(ImageText, "recognize", lambda *_: ("Valid image", ""))
    result = extract(path, "pdf")
    assert "Valid text" in result["units"][0]["text"] and "Valid image" in result["units"][0]["text"]
    assert "无法读取" not in result["units"][0]["text"]
    assert any("第 1 页 · 图片1" in warning and "无法读取" in warning for warning in result["warnings"])


@pytest.mark.parametrize("limit", ["pixels", "pages", "characters", "stream"])
def test_pdf_limits_close_document_and_keep_original(tmp_path, monkeypatch, limit):
    from app.documents import extraction, image_text, pdf
    import pypdfium2
    path = tmp_path / "limited.pdf"
    document = PdfSample()
    page = document.page()
    document.text(page, "Body text")
    document.image(page, (0,40,40,70), png())
    document.save(path)
    before = path.read_bytes()
    opened = []
    original = pypdfium2.PdfDocument
    def track(*args, **kwargs):
        document = original(*args, **kwargs)
        opened.append(document)
        return document
    monkeypatch.setattr(pypdfium2, "PdfDocument", track)
    if limit == "pixels":
        monkeypatch.setattr(image_text, "MAX_IMAGE_PIXELS", 1)
    elif limit == "pages":
        monkeypatch.setattr(extraction, "MAX_UNITS", 0)
    elif limit == "characters":
        monkeypatch.setattr(extraction, "MAX_TEXT", 1)
    else:
        monkeypatch.setattr(pdf, "MAX_CONTENT_BYTES", 1)
    with pytest.raises(AppError) as error:
        extract(path, "pdf")
    assert error.value.code == "document_limit"
    assert all(document.raw is None for document in opened)
    assert path.read_bytes() == before
    path.rename(tmp_path / "released.pdf")
