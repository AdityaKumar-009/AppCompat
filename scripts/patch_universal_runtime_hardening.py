#!/usr/bin/env python3
"""Compile/integration guardrails for the universal runtime translation pass."""
from __future__ import annotations

import sys
from pathlib import Path


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: patch_universal_runtime_hardening.py <NewBlackbox-root>")
    root = Path(sys.argv[1]).resolve()
    path = root / "Bcore/src/main/java/top/niunaijun/blackbox/fake/service/IActivityManagerProxy.java"
    text = path.read_text(encoding="utf-8")

    if "import top.niunaijun.blackbox.utils.IntentSanitizer;" not in text:
        marker = "import top.niunaijun.blackbox.utils.MethodParameterUtils;\n"
        if marker not in text:
            raise SystemExit("[universal-runtime-hardening] import insertion point missing")
        text = text.replace(
            marker,
            "import top.niunaijun.blackbox.utils.IntentSanitizer;\n" + marker,
            1,
        )
        print("[universal-runtime-hardening] IntentSanitizer import: applied")

    required = (
        "import top.niunaijun.blackbox.utils.IntentSanitizer;",
        "private Intent pendingShadowFor(Intent target)",
        "IntentSanitizer.sanitizeClassExtrasForIpc(target)",
        "top.niunaijun.blackbox.proxy.ProxyPendingReceiver",
        "top.niunaijun.blackbox.proxy.ProxyPendingService",
    )
    for item in required:
        if item not in text:
            raise SystemExit(f"[universal-runtime-hardening] missing invariant: {item}")
    path.write_text(text, encoding="utf-8")
    print("[universal-runtime-hardening] PendingIntent proxy integration verified")


if __name__ == "__main__":
    main()
