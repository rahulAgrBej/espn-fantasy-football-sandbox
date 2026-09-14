#!/usr/bin/env python3
"""Dump the key paths actually present in a cached ESPN payload.

Use this to verify field-path assumptions against a real response before
trusting an extractor. Point it at any JSON file under data/raw/.

    python scripts/probe.py data/raw/2026/msettings-mteam-<hash>.json
    python scripts/probe.py <file> --depth 4 --grep record
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def walk(node, prefix="", depth=0, max_depth=3):
    if depth > max_depth:
        return
    if isinstance(node, dict):
        for key, value in node.items():
            path = f"{prefix}.{key}" if prefix else key
            yield path, _describe(value)
            yield from walk(value, path, depth + 1, max_depth)
    elif isinstance(node, list) and node:
        path = f"{prefix}[]"
        yield from walk(node[0], path, depth, max_depth)


def _describe(value):
    if isinstance(value, dict):
        return f"dict({len(value)} keys)"
    if isinstance(value, list):
        return f"list({len(value)})" + (f" of {type(value[0]).__name__}" if value else "")
    text = repr(value)
    return text if len(text) <= 60 else text[:57] + "..."


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("path", type=Path)
    ap.add_argument("--depth", type=int, default=3)
    ap.add_argument("--grep", help="only paths containing this substring")
    args = ap.parse_args()

    data = json.loads(args.path.read_text())
    if isinstance(data, list):
        data = data[0]

    for path, desc in walk(data, max_depth=args.depth):
        if args.grep and args.grep.lower() not in path.lower():
            continue
        print(f"{path:<62} {desc}")


if __name__ == "__main__":
    main()
