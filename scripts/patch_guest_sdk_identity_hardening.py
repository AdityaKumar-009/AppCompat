#!/usr/bin/env python3
"""Small compile/runtime hardening applied after guest SDK translator generation."""
from __future__ import annotations

import sys
from pathlib import Path


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: patch_guest_sdk_identity_hardening.py <NewBlackbox-root>")
    root = Path(sys.argv[1]).resolve()
    path = root / "Bcore/src/main/java/top/niunaijun/blackbox/utils/compat/LegacyGuestSdkCompat.java"
    text = path.read_text(encoding="utf-8")
    old = '''            ZipOutputStream output = new ZipOutputStream(
                    new FileOutputStream(temp, false), 1024 * 1024);'''
    new = '''            ZipOutputStream output = new ZipOutputStream(
                    new java.io.BufferedOutputStream(
                            new FileOutputStream(temp, false), 1024 * 1024));'''
    if old in text:
        text = text.replace(old, new, 1)
        print("[guest-sdk-hardening] buffered ZipOutputStream constructor fixed")
    elif new not in text:
        raise SystemExit("[guest-sdk-hardening] ZipOutputStream pattern not found")
    path.write_text(text, encoding="utf-8")

    verified = path.read_text(encoding="utf-8")
    if "new java.io.BufferedOutputStream" not in verified:
        raise SystemExit("[guest-sdk-hardening] verification failed")
    print("[guest-sdk-hardening] guest APK repackaging verified")


if __name__ == "__main__":
    main()
