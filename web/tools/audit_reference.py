from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re


TOKENS = re.compile(r"--(?:spring|out|drawer|dur-fast|dur-base|dur-panel|bg|ink|ink2|muted|faint|raised|sunken|plate|line|focus-ring|e1|e2|e3|top|space-\d+)$")
SELECTORS = re.compile(r"\.ac-(?:btn|seg|switch|tip|dialog|popover|input|select)|\.dk-(?:pnav|stepper)")
KEYFRAMES = re.compile(r"@keyframes ac-(?:tip|dialog|fade|sheet)")


def blocks(text):
    stack = []
    boundary = 0
    quoted = None
    escaped = False
    comment = False
    index = 0
    while index < len(text):
        char = text[index]
        if comment:
            if text[index:index + 2] == "*/":
                comment = False
                index += 2
                continue
        elif quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quoted:
                quoted = None
        elif text[index:index + 2] == "/*":
            comment = True
            index += 2
            continue
        elif char in "\"'":
            quoted = char
        elif char == "{":
            if stack:
                stack[-1]["children"] = True
            stack.append({"start": boundary, "body": index + 1,
                          "selector": text[boundary:index].strip(), "children": False})
            boundary = index + 1
        elif char == "}":
            if not stack:
                raise ValueError("Unbalanced CSS braces")
            rule = stack.pop()
            if not rule["children"]:
                yield rule, text[rule["body"]:index], index + 1, [item["selector"] for item in stack]
            boundary = index + 1
        index += 1
    if stack or quoted or comment:
        raise ValueError("Incomplete CSS source")


def extract(root):
    assets = root / "app" / "assets"
    css_path = assets / "main-CE1c8hst.css"
    js_path = assets / "main-DWcRDe9d.js"
    css_bytes, js_bytes = css_path.read_bytes(), js_path.read_bytes()
    css, js = css_bytes.decode("utf-8"), js_bytes.decode("utf-8")
    rules, variables = [], []
    for rule, body, end, contexts in blocks(css):
        selected = SELECTORS.search(rule["selector"]) or any(KEYFRAMES.search(parent) for parent in contexts)
        reduced = any("prefers-reduced-motion:reduce" in parent for parent in contexts)
        if selected or reduced:
            start = rule["start"]
            while start < rule["body"] and css[start].isspace():
                start += 1
            rules.append({"selector": rule["selector"], "contexts": contexts,
                          "byte_start": len(css[:start].encode("utf-8")),
                          "byte_end": len(css[:end].encode("utf-8")),
                          "source": css[start:end]})
        for match in re.finditer(r"(?:^|;)(--[\w-]+):([^;]*)(?=;|$)", body):
            name = match.group(1)
            if TOKENS.fullmatch(name):
                start = rule["body"] + match.start(1)
                variables.append({"name": name, "value": match.group(2),
                                  "selector": rule["selector"], "contexts": contexts,
                                  "byte_start": len(css[:start].encode("utf-8"))})
    pivot = js.index("className:`ac-seg__pill`")
    snippets = []
    for marker in ("offsetLeft", "offsetWidth", '"--x":', '"--w":', 'className:`ac-seg__pill`'):
        location = js.find(marker, max(0, pivot - 3500), pivot + 500)
        if location == -1:
            raise ValueError(f"Segment control marker missing: {marker}")
        start, end = max(0, location - 100), min(len(js), location + 280)
        snippets.append({"marker": marker, "byte_start": len(js[:start].encode("utf-8")),
                         "source": js[start:end]})
    return {"format": 1, "reference": root.name,
            "assets": [{"path": str(path.relative_to(root)).replace("\\", "/"),
                        "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}
                       for path, data in ((css_path, css_bytes), (js_path, js_bytes))],
            "css_rules": rules, "css_variables": variables, "segment_js": snippets}


def main():
    parser = argparse.ArgumentParser(description="Extract exact reference UI rules, including CSS parent conditions")
    parser.add_argument("reference", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    result = extract(args.reference)
    if args.check:
        if json.loads(args.output.read_text(encoding="utf-8")) != result:
            raise SystemExit("Reference evidence differs from source; regenerate and review it")
    else:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"checked": args.check, "rules": len(result["css_rules"]),
                      "variables": len(result["css_variables"]),
                      "js_snippets": len(result["segment_js"]), "assets": result["assets"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
