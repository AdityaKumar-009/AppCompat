#!/usr/bin/env python3
"""Repair call sites that relied on the upstream BuildCompat off-by-one names."""
from __future__ import annotations

import sys
from pathlib import Path


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: patch_sdk_gate_calls.py <NewBlackbox-root>")

    root = Path(sys.argv[1]).resolve()
    path = root / "Bcore/src/main/java/top/niunaijun/blackbox/fake/service/IActivityManagerProxy.java"
    text = path.read_text(encoding="utf-8")

    old = "            if (BuildCompat.isU()) {\n                int flagsIndex = args.length - 1;"
    new = "            if (BuildCompat.isTiramisu()) {\n                int flagsIndex = args.length - 1;"

    count = text.count(old)
    if count == 1:
        text = text.replace(old, new, 1)
        path.write_text(text, encoding="utf-8")
        print("[sdk-gates] Android 13 dynamic-receiver gate corrected")
    elif count == 0 and new in text:
        print("[sdk-gates] Android 13 dynamic-receiver gate already corrected")
    else:
        raise SystemExit(f"[sdk-gates] expected one receiver gate, found {count}")

    verified = path.read_text(encoding="utf-8")
    if new not in verified:
        raise SystemExit("[sdk-gates] receiver gate verification failed")


if __name__ == "__main__":
    main()
