import asyncio
import json
from pathlib import Path
import pytest
from pydantic import ValidationError
from app.videos.detailed import DetailedVideoGeneration
from app.analysis.markdown import has_markdown_body, markdown_text, normalize_math
from app.videos.schemas import SCHEMAS, FORMAT_VERSION
from app.videos.pipeline import Pipeline
from app.config import Settings
from app.db import Task
from .conftest import upload
from .fakes import FakeModel, FakeRecognizer


SAMPLE = '''#### 比较与步骤

**核心判断**：材料需要连贯说明，保留 `a_b`。

3. 开始
   - 子步骤
4. 结束

| 对象 | 特点 |
| --- | --- |
| 甲 | a\\|b |
| 乙 | 稳定 |

> 仅引用原始材料。

```python
marker = "**literal**"
if marker:
    print(marker)
```

[资料](https://example.com)
'''

MATH_SAMPLE = r'''行内公式 $\frac{a_1}{b^2}+\sqrt{x}$ 与集合 $\{x\mid x>0\}$。

$$
\begin{pmatrix} 1 & 2 \\ 3 & 4 \end{pmatrix}
$$

反应式 $\ce{Ag+ + SCN- -> AgSCN v}$。
'''

CHEM_SAMPLE = r'''在酸性条件下，以铁铵矾 \ce{NH4Fe(SO4)2.12H2O} 为指示剂，使用 \ce{NH4SCN} 直接滴定 \ce{Ag+}。

$$ \ce{Ag+ + SCN- -> AgSCN v} $$

终点时生成 \ce{Fe(SCN)^{2+}}，与已有公式 $\ce{Fe^{3+}}$ 对照。
'''


@pytest.mark.parametrize("source,expected", [
    (r'离子\ce{Fe^{3+}}与$\ce{SCN-}$。', r'离子$\ce{Fe^{3+}}$与$\ce{SCN-}$。'),
    (r'$$ \ce{Ag+ + SCN- -> AgSCN v} $$', '$$\n' + r'\ce{Ag+ + SCN- -> AgSCN v}' + '\n$$'),
    (r'\ce{Ag+ + SCN- -> AgSCN v}', '$$\n' + r'\ce{Ag+ + SCN- -> AgSCN v}' + '\n$$'),
    (r'句中\(x^2\)公式。', '句中$x^2$公式。'),
    (r'\[x^2\]', '\n$$\nx^2\n$$\n'),
    (r'`\ce{Ag+}`和``$x$ ` \ce{Fe^{3+}}``', r'`\ce{Ag+}`和``$x$ ` \ce{Fe^{3+}}``'),
    ('~~~latex\n' + r'\ce{Ag+}' + '\n$$ x $$\n~~~', '~~~latex\n' + r'\ce{Ag+}' + '\n$$ x $$\n~~~'),
    ('    ' + r'\ce{Ag+}' + '\n', '    ' + r'\ce{Ag+}' + '\n'),
    (r'价格\$20；不完整\ce{Fe^{3+}', r'价格\$20；不完整\ce{Fe^{3+}'),
])
def test_math_normalization_preserves_chemistry_and_skips_literal_code(source, expected):
    normalized = normalize_math(source)
    assert normalized == expected
    assert normalize_math(normalized) == normalized


@pytest.mark.parametrize("prefix,formula", [
    ("1.  终点前\n    ", r"$$ \ce{Ag+ + SCN- -> AgSCN v} \quad (\text{白色}) $$"),
    ("- 终点前\n  ", r"$$\ce{Ag+ + SCN- -> AgSCN v (白色)}$$"),
    ("- 终点前\n  ", r"\ce{Ag+ + SCN- -> AgSCN v}"),
])
def test_display_math_keeps_list_indentation(prefix, formula):
    indent = prefix.rsplit("\n", 1)[1]
    body = formula.removeprefix("$$").removesuffix("$$").strip()
    normalized = normalize_math(prefix + formula + "\n\n后续**结论**。")
    assert normalized == prefix + "$$\n" + indent + body + "\n" + indent + "$$\n\n后续**结论**。"
    assert normalize_math(normalized) == normalized


def test_multiline_display_math_in_list_preserves_relative_formula_indentation():
    source = "1.  计算\n    $$\n    \\begin{aligned}\n      a &= b \\\\\n      c &= d\n    \\end{aligned}\n    $$\n2.  后续项目"
    assert normalize_math(source) == source


def test_markdown_plain_text_preserves_content_code_lists_tables_and_links():
    text = markdown_text(SAMPLE)
    assert has_markdown_body(SAMPLE)
    assert "比较与步骤" in text and "核心判断：材料需要连贯说明，保留 a_b。" in text
    assert "3. 开始\n  - 子步骤\n4. 结束" in text
    assert "对象 | 特点" in text and "甲 | a|b" in text and "乙 | 稳定" in text
    assert '> 仅引用' not in text and "仅引用原始材料。" in text
    assert 'marker = "**literal**"\nif marker:\n    print(marker)' in text
    assert "资料 (https://example.com)" in text
    assert "####" not in text and "```python" not in text and "**核心判断**" not in text


@pytest.mark.parametrize("body", ["", "   ", "#### 只有标题", "---", "```\n```", "![图片](https://example.com/a.png)", "<script>alert(1)</script>", "#### 标题\n\n![图片](https://example.com/a.png)"])
def test_empty_or_nonbody_markdown_does_not_count_as_a_subsection(body):
    assert not has_markdown_body(body)


def test_code_only_subsection_retains_initial_indentation_in_txt():
    markdown = "```python\n    nested = True\n    if nested:\n        run()\n```"
    assert has_markdown_body(markdown)
    assert markdown_text(markdown) == "    nested = True\n    if nested:\n        run()"


def test_math_export_preserves_latex_delimiters_braces_and_matrix_linebreaks():
    assert has_markdown_body(MATH_SAMPLE)
    assert markdown_text(MATH_SAMPLE) == MATH_SAMPLE.rstrip()
    assert markdown_text('代码 `$x_i$`，价格 \\$20。') == '代码 $x_i$，价格 $20。'
    assert markdown_text('```latex\n' + MATH_SAMPLE + '\n```') == MATH_SAMPLE.rstrip()


@pytest.mark.parametrize("kind", ["paragraph", "subheading", "list_item"])
def test_old_body_types_are_rejected_without_conversion(kind):
    with pytest.raises(ValidationError):
        SCHEMAS["chapters"].model_validate({"chapters": [{"chapter_id": "C1", "summary": "摘要", "blocks": [
            {"kind": kind, "text": "旧正文", "evidence_ids": ["E1"]}]}]})


def test_markdown_chapter_requires_readable_body_without_per_paragraph_references(tmp_path):
    model = FakeModel(Settings())
    coordinator = DetailedVideoGeneration("markdown", model, model.settings, tmp_path,
        [{"id": "source", "kind": "speech", "start_seconds": 1, "end_seconds": 2, "text": "原文"}],
        10, asyncio.Event(), lambda: None, lambda stage: None, {})
    payload = {"chapters": [{"chapter_id": "C1", "primary": coordinator.evidence, "context": []}]}
    value = {"chapters": [{"chapter_id": "C1", "summary": "摘要", "markdown": SAMPLE, "issues": []}]}
    coordinator.validate("chapters", payload, value)
    value["chapters"][0]["markdown"] = "#### 只有标题"
    with pytest.raises(ValueError, match="chapter_needs_body_content"):
        coordinator.validate("chapters", payload, value)


@pytest.mark.parametrize("body", [SAMPLE, MATH_SAMPLE, CHEM_SAMPLE, (Path(__file__).parent / "fixtures/chemistry-lists.md").read_text(encoding="utf-8")])
def test_txt_and_markdown_download_read_the_same_saved_body(client, app, video_file, body):
    video = upload(client, video_file)
    task_id = client.post(f'/api/v1/videos/{video["id"]}/analyses').json()["task_id"]
    Pipeline(app.state.pipeline.get_settings, app.state.sessions, app.state.diagnostics, FakeRecognizer, FakeModel).run(task_id)
    with app.state.sessions.begin() as session:
        task = session.get(Task, task_id)
        result = json.loads(json.dumps(task.result))
        assert result["format_version"] == FORMAT_VERSION == 4
        result["chapters"][0]["markdown"] = body
        result["source_issues"].append({"id": "I1", "chapter_id": "C1", "kind": "source_issue", "quote": "TCP",
            "reason": "名称存疑", "evidence_ids": result["chapters"][0]["evidence_ids"]})
        task.result = result
    markdown = client.get(f"/api/v1/analyses/{task_id}/notes?format=md")
    text = client.get(f"/api/v1/analyses/{task_id}/notes")
    explicit_text = client.get(f"/api/v1/analyses/{task_id}/notes?format=txt")
    assert markdown.status_code == text.status_code == 200
    assert markdown.headers["content-type"].startswith("text/markdown")
    assert text.headers["content-type"].startswith("text/plain")
    assert ".md" in markdown.headers["content-disposition"] and ".txt" in text.headers["content-disposition"]
    normalized = normalize_math(body)
    assert client.get(f"/api/v1/analyses/{task_id}").json()["chapters"][0]["markdown"] == normalized
    assert normalized in markdown.text and "## 存疑部分" in markdown.text
    assert markdown_text(normalized) in text.text and text.text == explicit_text.text
    assert "原文：TCP" in text.text and "00:" not in text.text.split("存疑部分：")[1]
    assert "原文：TCP" in markdown.text and "00:" not in markdown.text.split("## 存疑部分")[1]
    assert client.get(f"/api/v1/analyses/{task_id}/notes?format=html").status_code == 422
    with app.state.sessions() as session:
        assert session.get(Task, task_id).result == result
