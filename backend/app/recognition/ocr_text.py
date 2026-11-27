"""画面文字比较时使用的符号和边界规则。"""
import re
from bisect import bisect_left, bisect_right
from itertools import accumulate
from string import punctuation


FORMULA_SYMBOLS = r"+-−±×÷=<>≤≥≠%‰\^_{}[]()/*°℃℉"
# 只转换同一标点的全角变体，保留上下标和单位。
OCR_SYMBOLS = {ord(symbol) + 0xFEE0: symbol for symbol in punctuation}
ELEMENT_SEQUENCE = re.compile(r"(?<![A-Za-z])(?:[A-Z][a-z]?){2,}(?![A-Za-z])")
CAPTION_PUNCTUATION = re.compile(r'[\s，,。；;：:、！？!?“”"‘’…]')


def requires_literal_match(text):
    # AgCl 等只含字母的式子也需要完整匹配。
    return any(char.isdigit() or char in FORMULA_SYMBOLS for char in text) or ELEMENT_SEQUENCE.search(text) is not None


def spoken_caption_matcher(speech):
    """在同期连续口播里查找已经读出的完整字幕。"""
    cues = sorted((item["start_seconds"], item["end_seconds"], CAPTION_PUNCTUATION.sub("", item["text"]))
                  for item in speech if item["text"].strip())
    starts = [start for start, _, _ in cues]
    ends = list(accumulate((end for _, end, _ in cues), max))

    def matches(text, first, last):
        caption = CAPTION_PUNCTUATION.sub("", text)
        if not re.fullmatch(r"[\u3400-\u9fff]{6,}", caption):
            return False
        # 逐字展开到末帧时，开头往往已说完。长停顿两侧的文字不能拼接命中。
        start, end = first - 6, last + 2
        fragments = []
        until = start
        for cue_start, cue_end, fragment in cues[bisect_left(ends, start):bisect_right(starts, end)]:
            if cue_end >= start:
                if cue_start - until > 1:
                    fragments.append("\n")
                fragments.append(fragment)
                until = max(until, cue_end)
        return caption in "".join(fragments)

    return matches
