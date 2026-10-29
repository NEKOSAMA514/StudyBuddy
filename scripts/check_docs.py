"""Check maintained Markdown locally; does not fetch external links."""
import argparse
import json
from pathlib import Path
import re
from urllib.parse import unquote


def check(root):
    # 本机验收记录依赖不入库的报告；协作规则只在本地提供时检查。
    files = [root / "README.md", *[path for path in sorted((root / "docs").glob("*.md"))
                                 if path.name != "portable-validation.md"]]
    if (root / "AGENTS.md").is_file():
        files.insert(1, root / "AGENTS.md")
    links = fences = examples = 0
    for file in files:
        content = file.read_text(encoding="utf-8-sig")
        assert "\ufffd" not in content, f"Replacement character: {file}"
        markers = re.findall(r"^```.*$", content, re.M)
        assert len(markers) % 2 == 0, f"Unclosed code fence: {file}"
        fences += len(markers) // 2
        for target in re.findall(r"\]\(([^)]+)\)", content):
            target = unquote(target.strip("<>").split("#")[0])
            if not target or re.match(r"[\w+.-]+:", target):
                continue
            assert (file.parent / target).exists(), f"Broken local link: {file}: {target}"
            links += 1
        for body in re.findall(r"^```json\s*\n(.*?)^```", content, re.M | re.S):
            json.loads(body)
            examples += 1
    return {"files": len(files), "local_links": links, "code_blocks": fences, "json_examples": examples}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    result = check(Path(__file__).resolve().parents[1])
    if args.report:
        args.report.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result))
