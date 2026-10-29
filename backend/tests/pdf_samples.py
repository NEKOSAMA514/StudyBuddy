"""用独立的 pypdf 对象与内容流构造 PDF 样本，不依赖被测适配器。"""
import io

from pypdf import PdfWriter
from pypdf.generic import (ArrayObject, DecodedStreamObject, DictionaryObject,
                           FloatObject, NameObject, NumberObject, TextStringObject)


def dictionary(**values):
    return DictionaryObject({NameObject('/' + key): value for key, value in values.items()})


class PdfSample:
    def __init__(self):
        self.writer = PdfWriter()

    def page(self, width=595, height=842):
        page = self.writer.add_blank_page(width=width, height=height)
        page[NameObject('/Resources')] = dictionary()
        return page

    def stream(self, data, **values):
        stream = DecodedStreamObject()
        stream.update(dictionary(**values))
        stream.set_data(data)
        return self.writer._add_object(stream)

    def append(self, page, data):
        content = self.stream(data)
        previous = page.get('/Contents')
        if previous is None:
            page[NameObject('/Contents')] = content
        elif isinstance(previous, ArrayObject):
            previous.append(content)
        else:
            page[NameObject('/Contents')] = ArrayObject([previous, content])
        return content

    def resource(self, page, kind, reference):
        resources = page['/Resources']
        category = resources.setdefault(NameObject('/' + kind), dictionary())
        name = NameObject(f'/R{len(category)}')
        category[name] = reference
        return name

    def text(self, page, text, x=20, y=30, size=12, opacity=None):
        if all(ord(c) < 256 for c in text):
            font = self.writer._add_object(dictionary(Type=NameObject('/Font'), Subtype=NameObject('/Type1'),
                BaseFont=NameObject('/Helvetica'), Encoding=NameObject('/WinAnsiEncoding')))
            encoded = text.encode('cp1252')
        else:
            mapping = {c: i + 1 for i, c in enumerate(dict.fromkeys(text))}
            cmap = (b'/CIDInit /ProcSet findresource begin 12 dict begin begincmap\n'
                    b'/CIDSystemInfo << /Registry (Adobe) /Ordering (Identity) /Supplement 0 >> def\n'
                    b'/CMapName /Sample def /CMapType 2 def\n'
                    b'1 begincodespacerange <0000> <FFFF> endcodespacerange\n' +
                    f'{len(mapping)} beginbfchar\n'.encode() +
                    b'\n'.join(f'<{i:04X}> <{c.encode("utf-16-be").hex()}>'.encode() for c, i in mapping.items()) +
                    b'\nendbfchar endcmap CMapName currentdict /CMap defineresource pop end end')
            descendant = self.writer._add_object(dictionary(Type=NameObject('/Font'), Subtype=NameObject('/CIDFontType2'),
                BaseFont=NameObject('/Arial'), DW=NumberObject(1000),
                CIDSystemInfo=dictionary(Registry=TextStringObject('Adobe'), Ordering=TextStringObject('Identity'), Supplement=NumberObject(0))))
            font = self.writer._add_object(dictionary(Type=NameObject('/Font'), Subtype=NameObject('/Type0'),
                BaseFont=NameObject('/Arial'), Encoding=NameObject('/Identity-H'),
                DescendantFonts=ArrayObject([descendant]), ToUnicode=self.stream(cmap)))
            encoded = b''.join(mapping[c].to_bytes(2, 'big') for c in text)
        name = self.resource(page, 'Font', font)
        prefix = b''
        if opacity is not None:
            state = self.writer._add_object(dictionary(ca=FloatObject(opacity), CA=FloatObject(opacity)))
            state_name = self.resource(page, 'ExtGState', state)
            prefix = f'{state_name} gs '.encode()
        return self.append(page, b'q ' + prefix + f'BT {name} {size} Tf {x} {float(page.mediabox.height) - y} Td <{encoded.hex()}> Tj ET Q'.encode())

    def image_object(self, raw):
        from PIL import Image
        with Image.open(io.BytesIO(raw)) as source:
            image = source.copy()
        alpha = image.getchannel('A') if image.mode in {'RGBA', 'LA'} else None
        image = image.convert('L' if image.mode in {'L', 'LA', '1'} else 'RGB')
        values = dict(Type=NameObject('/XObject'), Subtype=NameObject('/Image'),
                      Width=NumberObject(image.width), Height=NumberObject(image.height),
                      BitsPerComponent=NumberObject(8),
                      ColorSpace=NameObject('/DeviceGray' if image.mode == 'L' else '/DeviceRGB'))
        if alpha is not None:
            values['SMask'] = self.stream(alpha.tobytes(), Type=NameObject('/XObject'), Subtype=NameObject('/Image'),
                Width=NumberObject(alpha.width), Height=NumberObject(alpha.height),
                BitsPerComponent=NumberObject(8), ColorSpace=NameObject('/DeviceGray'))
        return self.stream(image.tobytes(), **values)

    def image(self, page, rect, raw=None, reference=None):
        reference = reference if reference is not None else self.image_object(raw)
        name = self.resource(page, 'XObject', reference)
        x0, y0, x1, y1 = rect
        self.append(page, f'q {x1-x0} 0 0 {y1-y0} {x0} {float(page.mediabox.height)-y1} cm {name} Do Q'.encode())
        return reference

    def form(self, page, source, matrix=(1, 0, 0, 1, 0, 0)):
        contents = source.get_contents()
        form = self.stream(contents.get_data() if contents is not None else b'', Type=NameObject('/XObject'),
            Subtype=NameObject('/Form'), BBox=source.mediabox, Resources=source['/Resources'])
        name = self.resource(page, 'XObject', form)
        self.append(page, ('q ' + ' '.join(map(str, matrix)) + f' cm {name} Do Q').encode())
        return form

    def bytes(self):
        output = io.BytesIO()
        self.writer.write(output)
        return output.getvalue()

    def save(self, path):
        path.write_bytes(self.bytes())


def text_pdf(texts):
    sample = PdfSample()
    for text in texts:
        sample.text(sample.page(300, 300), text, x=30, y=100)
    return sample.bytes()
