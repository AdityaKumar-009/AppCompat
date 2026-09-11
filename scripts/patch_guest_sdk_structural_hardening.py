#!/usr/bin/env python3
"""Compile-safety fixes for the generated structural guest DEX translator."""
from __future__ import annotations

import sys
from pathlib import Path


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: patch_guest_sdk_structural_hardening.py <NewBlackbox-root>")
    root = Path(sys.argv[1]).resolve()
    path = root / "Bcore/src/main/java/top/niunaijun/blackbox/utils/compat/LegacyGuestSdkCompat.java"
    text = path.read_text(encoding="utf-8")

    old = '''                    byte[] shim = BlackBoxCore.getContext().getAssets()
                            .open(SHIM_ASSET).use(inputStream -> readAll(inputStream));'''
    new = '''                    byte[] shim = readAll(
                            BlackBoxCore.getContext().getAssets().open(SHIM_ASSET));'''
    if old in text:
        text = text.replace(old, new, 1)
        print("[guest-sdk-structural-hardening] fixed Java asset stream expression")
    elif new not in text:
        raise SystemExit("[guest-sdk-structural-hardening] shim stream pattern not found")

    # Force traversal of method instructions during validation, not only class defs.
    # This catches malformed instruction/reference tables before replacing base.apk.
    old_validation = '''        for (ClassDef ignored : dex.getClasses()) {
            ignored.getType();
        }
        if (dex.getClasses().isEmpty()) {'''
    new_validation = '''        for (ClassDef classDef : dex.getClasses()) {
            classDef.getType();
            for (org.jf.dexlib2.iface.Method method : classDef.getMethods()) {
                method.getName();
                if (method.getImplementation() != null) {
                    for (org.jf.dexlib2.iface.instruction.Instruction instruction
                            : method.getImplementation().getInstructions()) {
                        instruction.getOpcode();
                    }
                }
            }
        }
        if (dex.getClasses().isEmpty()) {'''
    if old_validation in text:
        text = text.replace(old_validation, new_validation, 1)
        print("[guest-sdk-structural-hardening] deep DEX validation enabled")
    elif new_validation not in text:
        raise SystemExit("[guest-sdk-structural-hardening] DEX validation pattern not found")

    text = text.replace("import java.util.Arrays;\n", "")
    path.write_text(text, encoding="utf-8")

    verified = path.read_text(encoding="utf-8")
    if ".use(inputStream" in verified:
        raise SystemExit("[guest-sdk-structural-hardening] invalid Kotlin-style Java expression remains")
    if "method.getImplementation().getInstructions()" not in verified:
        raise SystemExit("[guest-sdk-structural-hardening] deep validation missing")
    print("[guest-sdk-structural-hardening] structural translator compile invariants verified")


if __name__ == "__main__":
    main()
