"""MinerU HTTP 契约及结果读取；每个批次持久保存任务编号与原始 Markdown。"""
import json
import re
import stat
import zipfile
from html.parser import HTMLParser
from pathlib import PurePosixPath
from urllib.parse import urlsplit

import httpx
from httpx import Client

from ..errors import AppError
from ..util.json_io import atomic_json
from ..analysis.markdown import markdown_text
from .formatting import literal

API = "https://mineru.net"
MODES = {"mineru_agent", "mineru_vlm"}


def read_json(path, default=None):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


def trusted_url(url):
    parts = urlsplit(url)
    domains = ("mineru.net", "aliyuncs.com", "openxlab.org.cn", "shlab.tech")
    if (parts.scheme != "https" or parts.port not in (None, 443) or parts.username or parts.password or
            not any(parts.hostname == domain or (parts.hostname or "").endswith("." + domain) for domain in domains)):
        raise AppError("mineru_download", "MinerU 返回的下载地址不在允许的服务范围内。")
    return url


# 封装云端批次提交、轮询与结果保存，停止检查由调用方明确传入。
class MinerUClient:
    def __init__(self, token, check, wait, report):
        self.token, self.check, self.wait, self.report = token, check, wait, report

    def reason(self, value):
        text = str(value or "服务未提供具体原因").replace("\r", " ").replace("\n", " ")
        if self.token:
            text = text.replace(self.token, "[REDACTED]")
        return re.sub(r"https?://\S+", "[外部地址]", text)[:300]

    def api(self, client, method, path, **kwargs):
        for attempt in range(3):
            self.check()
            try:
                response = client.request(method, API + path, **kwargs)
                response.raise_for_status()
                value = response.json()
                if value.get("code") != 0:
                    raise AppError("mineru_failed", f"MinerU 请求失败（代码 {self.reason(value.get('code'))}）：{self.reason(value.get('msg'))}")
                return value["data"]
            except (httpx.TimeoutException, httpx.NetworkError, httpx.HTTPStatusError) as exc:
                retryable = not isinstance(exc, httpx.HTTPStatusError) or exc.response.status_code in {408, 429, 500, 502, 503, 504}
                if method != "GET" or not retryable or attempt == 2:
                    raise
                self.report("等待重试", "云端状态暂不可用，稍后重新查询")
                self.wait(3 * (attempt + 1))

    def download(self, client, url, output, limit):
        temporary = output.with_suffix(output.suffix + ".part")
        try:
            for _ in range(6):
                self.check()
                with client.stream("GET", trusted_url(url)) as response:
                    if response.is_redirect:
                        url = str(response.url.join(response.headers["location"]))
                        continue
                    response.raise_for_status()
                    size = 0
                    with temporary.open("wb") as stream:
                        for chunk in response.iter_bytes():
                            self.check()
                            size += len(chunk)
                            if size > limit:
                                raise AppError("mineru_download", "MinerU 返回的结果超过本地读取限制。")
                            stream.write(chunk)
                    temporary.replace(output)
                    return
            raise AppError("mineru_download", "MinerU 下载地址重定向次数过多。")
        finally:
            temporary.unlink(missing_ok=True)

    def parse(self, pdf, folder, mode, pages):
        folder.mkdir(parents=True, exist_ok=True)
        markdown = folder / "full.md"
        if markdown.exists() and (mode == "mineru_agent" or (folder / "pages.json").exists()):
            self.report("复用结果", "复用已下载的解析结果")
            return markdown.read_text(encoding="utf-8"), read_json(folder / "pages.json")
        state_path = folder / "remote.json"
        state = read_json(state_path, {})
        headers = {"Authorization": "Bearer " + self.token} if mode == "mineru_vlm" else {}
        with Client(timeout=60, headers=headers, follow_redirects=False) as api, Client(timeout=180, follow_redirects=False) as storage:
            if not state:
                atomic_json(state_path, {"submission_uncertain": True})
                self.report("提交解析", "申请云端解析任务")
                try:
                    if mode == "mineru_agent":
                        data = self.api(api, "POST", "/api/v1/agent/parse/file", json={"file_name": "batch.pdf", "language": "ch",
                            "enable_formula": True, "enable_table": True, "is_ocr": False})
                        state = {"task_id": data["task_id"], "upload_url": data["file_url"], "uploaded": False}
                    else:
                        data = self.api(api, "POST", "/api/v4/file-urls/batch", json={"files": [{"name": "batch.pdf", "is_ocr": True}],
                            "model_version": "vlm", "enable_formula": True, "enable_table": True, "language": "ch"})
                        state = {"task_id": data["batch_id"], "upload_url": data["file_urls"][0], "uploaded": False}
                except AppError:
                    atomic_json(state_path, {"failed": True})
                    raise
                except httpx.HTTPStatusError as exc:
                    if 400 <= exc.response.status_code < 500 and exc.response.status_code != 408:
                        atomic_json(state_path, {"failed": True})
                    raise
                atomic_json(state_path, state)
            if state.get("submission_uncertain"):
                raise AppError("mineru_submission_uncertain", "上次提交未取得任务编号，无法确认云端是否已接收；不会重复提交，可整份改用本地提取。")
            if state.get("failed"):
                raise AppError("mineru_failed", "上次请求已明确失败，请手动重试未完成部分。")

            def status():
                path = "/api/v1/agent/parse/" if mode == "mineru_agent" else "/api/v4/extract-results/batch/"
                result = self.api(api, "GET", path + state["task_id"])
                return result if mode == "mineru_agent" else result["extract_result"][0]

            # 上传响应丢失时先查询原任务，不重新创建解析任务。
            result = status()
            if not state["uploaded"] and result["state"] == "waiting-file":
                self.report("上传文件", "正在上传当前 PDF 批次")
                self.check()
                with pdf.open("rb") as source:
                    response = storage.put(trusted_url(state["upload_url"]), content=source)
                response.raise_for_status()
                state["uploaded"] = True
                atomic_json(state_path, state)
            for _ in range(600):
                self.check()
                result = status()
                current = result["state"]
                if current == "done":
                    break
                if current == "failed":
                    # 失败是明确终态；手动重试才允许创建替代请求。
                    state["failed"] = True
                    atomic_json(state_path, state)
                    raise AppError("mineru_failed", f"MinerU 解析失败：{self.reason(result.get('err_msg'))}（代码 {self.reason(result.get('err_code', 'unknown'))}），可重试或改用本地提取。")
                self.report("云端排队" if current == "pending" else "云端解析", "等待 MinerU 返回完整结果")
                self.wait(3)
            else:
                raise AppError("mineru_timeout", "云端解析仍未完成，任务编号已保留，可稍后继续查询。")
            self.report("下载结果", "正在保存官方 Markdown 结果")
            if mode == "mineru_agent":
                self.download(storage, result["markdown_url"], markdown, 32 * 1024 * 1024)
                return markdown.read_text(encoding="utf-8"), None
            archive = folder / "result.zip"
            if not archive.exists():
                self.download(storage, result["full_zip_url"], archive, 256 * 1024 * 1024)
            text, content = read_archive(archive, pages)
            atomic_json(folder / "pages.json", content)
            temporary = markdown.with_suffix(".tmp")
            temporary.write_text(text, encoding="utf-8")
            temporary.replace(markdown)
            return text, content


# 读取并校验官方结果包及页码范围，不能确认页码时明确拒绝结果。
def read_archive(path, pages):
    with zipfile.ZipFile(path) as archive:
        entries = archive.infolist()
        if len(entries) > 2000 or sum(item.file_size for item in entries) > 128 * 1024 * 1024:
            raise AppError("mineru_result", "MinerU 压缩结果超过本地读取限制。")
        for item in entries:
            original_name = item.orig_filename
            name = PurePosixPath(original_name)
            if (name.is_absolute() or ".." in name.parts or "\\" in original_name or ":" in original_name or
                    stat.S_ISLNK(item.external_attr >> 16)):
                raise AppError("mineru_result", "MinerU 压缩结果包含不安全的文件路径。")
        markdown = next(item for item in entries if PurePosixPath(item.filename).name == "full.md")
        content = next(item for item in entries if item.filename.endswith("_content_list.json"))
        text = archive.read(markdown).decode("utf-8-sig")
        blocks = json.loads(archive.read(content))
    if (not isinstance(blocks, list) or any(not isinstance(item, dict) or type(item.get("page_idx")) is not int or
            not 0 <= item["page_idx"] < pages for item in blocks)):
        raise AppError("mineru_result", "MinerU 逐页结果无法验证，未猜测原文件页码。")
    return text, blocks


def page_markdown(blocks):
    """采用官方结构化文字的原顺序，保留标题级别、公式和表格，不猜解公式。"""
    parts = []
    for item in blocks:
        text = item.get("text", "")
        if item.get("type") == "table":
            text = item.get("table_body", "")
            if "<table" in text.lower():
                text = table_markdown(text)
        if item.get("type") in {"image", "chart", "table"}:
            captions = item.get(item["type"] + "_caption", [])
            footnotes = item.get(item["type"] + "_footnote", [])
            text = "\n\n".join([*(captions if isinstance(captions, list) else [captions]), text,
                                   *(footnotes if isinstance(footnotes, list) else [footnotes])])
        if text:
            level = item.get("text_level", 0)
            if type(level) is int and level > 0:
                text = "#" * min(level, 6) + " " + text
            parts.append(text)
    return "\n\n".join(parts)


# 按跨行跨列占位展开矩形表格，合并文字只放起点，非法结构不静默错列。
def table_markdown(html):
    def invalid():
        raise AppError("mineru_result", "MinerU 表格结构无法读取，未丢弃表格后继续生成。")

    class Rows(HTMLParser):
        def __init__(self):
            super().__init__()
            self.grid, self.row, self.cell = {}, None, None
            self.height, self.column, self.in_table, self.closed = 0, 0, False, False

        def handle_starttag(self, tag, attrs):
            if tag == "table":
                if self.in_table or self.closed:
                    invalid()
                self.in_table = True
            elif tag == "tr":
                if not self.in_table or self.row is not None:
                    invalid()
                self.row, self.column = self.height, 0
                self.height += 1
            elif tag in {"td", "th"}:
                if self.row is None or self.cell is not None:
                    invalid()
                attrs = dict(attrs)
                spans = [attrs.get(key, "1") for key in ("rowspan", "colspan")]
                if any(not value or not re.fullmatch(r"[0-9]+", value) or int(value) < 1 for value in spans):
                    invalid()
                self.rowspan, self.colspan = map(int, spans)
                self.cell = []
            elif tag == "br" and self.cell is not None:
                self.cell.append(" ")

        def handle_data(self, value):
            if self.cell is not None:
                self.cell.append(value)

        def handle_endtag(self, tag):
            if tag in {"td", "th"}:
                if self.cell is None:
                    invalid()
                while (self.row, self.column) in self.grid:
                    self.column += 1
                text = "".join(self.cell).strip().replace("|", "\\|")
                for row in range(self.row, self.row + self.rowspan):
                    for column in range(self.column, self.column + self.colspan):
                        if (row, column) in self.grid:
                            invalid()
                        self.grid[row, column] = text if (row, column) == (self.row, self.column) else ""
                self.column += self.colspan
                self.cell = None
            elif tag == "tr":
                if self.row is None or self.cell is not None:
                    invalid()
                self.row = None
            elif tag == "table":
                if not self.in_table or self.row is not None:
                    invalid()
                self.in_table, self.closed = False, True
    parser = Rows()
    parser.feed(html)
    parser.close()
    if not parser.closed or not parser.grid or max(row for row, _ in parser.grid) >= parser.height:
        invalid()
    width = max(column for _, column in parser.grid) + 1
    lines = ["| " + " | ".join(parser.grid.get((row, column), "") for column in range(width)) + " |"
             for row in range(parser.height)]
    return "\n".join([lines[0], "| " + " | ".join(["---"] * width) + " |", *lines[1:]])


# 按页或批次补充图片文字，跳过正文已有行和重复图片，保留新增信息。
def supplement(text, records):
    base = re.sub(r"\s+", "", markdown_text(text, include_link_urls=False))
    prefix = "图片文字（本地 OCR，识别可能有误）："
    seen, parts = set(), []
    for record in records:
        value = record["text"].strip()
        key = record["digest"]
        plain = value.removeprefix(prefix).strip()
        if not plain or key in seen or re.sub(r"\s+", "", plain) in base:
            continue
        seen.add(key)
        lines = []
        for line in plain.splitlines():
            normalized = re.sub(r"\s+", "", line)
            if not normalized or normalized not in base:
                lines.append(line)
        remaining = "\n".join(lines).strip()
        if not remaining:
            continue
        base += "\0" + re.sub(r"\s+", "", remaining)
        value = (prefix + "\n" if value.startswith(prefix) else "") + remaining
        parts.append(f"第 {record['page']} 页 · 图片{record['image']}\n\n{literal(value)}")
    return text + ("\n\n### 图片文字补充\n\n" + "\n\n".join(parts) if parts else "")
