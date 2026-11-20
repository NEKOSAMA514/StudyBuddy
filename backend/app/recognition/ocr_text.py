"""画面文字比较时使用的符号和边界规则。"""
import re
from string import punctuation


FORMULA_SYMBOLS = r"+-−±×÷=<>≤≥≠%‰\^_{}[]()/*°℃℉"
# 只转换同一标点的全角变体，保留上下标和单位。
OCR_SYMBOLS = {ord(symbol) + 0xFEE0: symbol for symbol in punctuation}
ELEMENT_SEQUENCE = re.compile(r"(?<![A-Za-z])(?:[A-Z][a-z]?){2,}(?![A-Za-z])")


def requires_literal_match(text):
    # AgCl 等只含字母的式子也需要完整匹配。
    return any(char.isdigit() or char in FORMULA_SYMBOLS for char in text) or ELEMENT_SEQUENCE.search(text) is not None
