#!/usr/bin/env python3
"""Compile-safety fixes for the generated structural guest DEX translator."""
from __future__ import annotations

import sys
from pathlib import Path


GUAVA = "implementation 'com.google.guava:guava:31.1-android'"


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: patch_guest_sdk_structural_hardening.py <NewBlackbox-root>")
    root = Path(sys.argv[1]).resolve()
    path = root / "Bcore/src/main/java/top/niunaijun/blackbox/utils/compat/LegacyGuestSdkCompat.java"
    text = path.read_text(encoding="utf-8")

    # dexlib2's ImmutableMethodReference exposes Guava's ImmutableList in its public
    # signature. The later legacy-reflection patch instantiates that class directly,
    # so javac must have Guava on Bcore's compile classpath even when dexlib2's
    # transitive metadata is unavailable in the pinned engine build.
    gradle_path = root / "Bcore/build.gradle"
    gradle = gradle_path.read_text(encoding="utf-8")
    if GUAVA not in gradle:
        dexlib = "    implementation 'org.smali:dexlib2:2.5.2'\n"
        if dexlib not in gradle:
            raise SystemExit("[guest-sdk-structural-hardening] dexlib2 dependency missing")
        gradle = gradle.replace(
            dexlib,
            dexlib + "    // Required by dexlib2 immutable reference API used by reflection rewriting.\n"
            + f"    {GUAVA}\n",
            1,
        )
        gradle_path.write_text(gradle, encoding="utf-8")
        print("[guest-sdk-structural-hardening] explicit Guava compile dependency added")
    else:
        print("[guest-sdk-structural-hardening] explicit Guava compile dependency already present")

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

    # Keep java.util.Arrays imported. The following legacy-reflection patch adds
    # Arrays.asList(...) calls when it creates immutable method references.
    if "import java.util.Arrays;" not in text:
        import_anchor = "import java.io.InputStream;\n"
        if import_anchor not in text:
            raise SystemExit("[guest-sdk-structural-hardening] Java import anchor missing")
        text = text.replace(import_anchor, import_anchor + "import java.util.Arrays;\n", 1)
        print("[guest-sdk-structural-hardening] java.util.Arrays import restored")

    path.write_text(text, encoding="utf-8")

    verified = path.read_text(encoding="utf-8")
    if ".use(inputStream" in verified:
        raise SystemExit("[guest-sdk-structural-hardening] invalid Kotlin-style Java expression remains")
    if "method.getImplementation().getInstructions()" not in verified:
        raise SystemExit("[guest-sdk-structural-hardening] deep validation missing")
    if "import java.util.Arrays;" not in verified:
        raise SystemExit("[guest-sdk-structural-hardening] java.util.Arrays import missing")
    print("[guest-sdk-structural-hardening] structural translator compile invariants verified")


if __name__ == "__main__":
    main()
