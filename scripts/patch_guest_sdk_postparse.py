#!/usr/bin/env python3
"""Move guest DEX translation behind original APK certificate parsing.

BlackBox calls PackageParser.collectCertificates() while importing a package. A DEX
rewrite necessarily invalidates the original APK signature, so rewriting the staging
APK before package parsing makes otherwise valid apps fail installation. Preserve the
original APK through parsing/certificate collection, then translate only BlackBox's
private executable copy in CopyExecutor. BPackage keeps the original signature
metadata while the class loader executes the translated private copy.
"""
from __future__ import annotations

import sys
from pathlib import Path


def replace_once(text: str, old: str, new: str, label: str) -> str:
    count = text.count(old)
    if count == 0:
        if new in text:
            print(f"[guest-sdk-postparse] {label}: already applied")
            return text
        raise SystemExit(f"[guest-sdk-postparse] {label}: source pattern not found")
    if count != 1:
        raise SystemExit(f"[guest-sdk-postparse] {label}: expected one match, found {count}")
    print(f"[guest-sdk-postparse] {label}: applied")
    return text.replace(old, new, 1)


def remove_preparse_translation(root: Path) -> None:
    path = root / "Bcore/src/main/java/top/niunaijun/blackbox/core/system/pm/BPackageManagerService.java"
    text = path.read_text(encoding="utf-8")
    old = '''            // The helper targetSdk cannot change Build.VERSION.SDK_INT observed by
            // guest bytecode. Translate only target<=25 APK code to the API-25 shim;
            // BlackBox itself keeps the real host SDK for Binder compatibility.
            LegacyGuestSdkCompat.Result guestSdk = LegacyGuestSdkCompat.translateIfLegacy(apkFile);
            if (guestSdk.translated) {
                Slog.i(TAG, "guest SDK profile: refs=" + guestSdk.referencesPatched
                        + " dex=" + guestSdk.dexFilesPatched + " shim=" + guestSdk.shimEntry);
            }

'''
    if old in text:
        text = text.replace(old, "", 1)
        print("[guest-sdk-postparse] removed pre-certificate DEX translation")
    elif "LegacyGuestSdkCompat.Result guestSdk" in text:
        raise SystemExit("[guest-sdk-postparse] unknown preparse translation shape")

    # Keep an explicit source invariant beside the parser. This line also documents
    # why the old pre-parse call must never be reintroduced during future refactors.
    marker = '''            // LegacyGuestSdkCompat.translateIfLegacy(apkFile) must NOT run here:
            // PackageParser.collectCertificates() below needs the untouched signed APK.
'''
    insertion = '''            PackageInfo packageArchiveInfo = BlackBoxCore.getPackageManager().getPackageArchiveInfo(apkFile.getAbsolutePath(), 0);'''
    if marker not in text:
        if insertion not in text:
            raise SystemExit("[guest-sdk-postparse] package-info insertion point missing")
        text = text.replace(insertion, marker + insertion, 1)
        print("[guest-sdk-postparse] documented pre-certificate no-translation invariant")
    path.write_text(text, encoding="utf-8")


def patch_copy_executor(root: Path) -> None:
    path = root / "Bcore/src/main/java/top/niunaijun/blackbox/core/system/pm/installer/CopyExecutor.java"
    text = path.read_text(encoding="utf-8")
    text = replace_once(
        text,
        "import top.niunaijun.blackbox.utils.Slog;",
        "import top.niunaijun.blackbox.utils.Slog;\nimport top.niunaijun.blackbox.utils.compat.LegacyGuestSdkCompat;",
        "import post-parse guest SDK translator",
    )

    old_base = '''                if (option.isFlag(InstallOption.FLAG_URI_FILE)) {
                    boolean b = BzFileUtils.renameTo(origFile, newFile);
                    if (!b) {
                        BzFileUtils.copyFile(origFile, newFile);
                    }
                } else {
                    BzFileUtils.copyFile(origFile, newFile);
                }
                newFile.setReadOnly();'''
    new_base = '''                if (option.isFlag(InstallOption.FLAG_URI_FILE)) {
                    boolean b = BzFileUtils.renameTo(origFile, newFile);
                    if (!b) {
                        BzFileUtils.copyFile(origFile, newFile);
                    }
                } else {
                    BzFileUtils.copyFile(origFile, newFile);
                }

                // PackageParser has already collected certificates from origFile.
                // Rewrite only this private executable copy so signature identity in
                // BPackage remains the original app's identity.
                LegacyGuestSdkCompat.Result sdkTranslation =
                        LegacyGuestSdkCompat.translateIfLegacy(newFile);
                if (sdkTranslation.translated) {
                    Slog.i(TAG, "guest SDK executable profile: pkg=" + ps.pkg.packageName
                            + " refs=" + sdkTranslation.referencesPatched
                            + " dex=" + sdkTranslation.dexFilesPatched
                            + " shim=" + sdkTranslation.shimEntry);
                }
                newFile.setReadOnly();'''
    text = replace_once(text, old_base, new_base, "translate private base APK after certificate parse")

    old_split = '''                        try {
                            BzFileUtils.copyFile(splitFile, copied);
                            copied.setReadOnly();
                            newSplitPaths.add(copied.getAbsolutePath());'''
    new_split = '''                        try {
                            BzFileUtils.copyFile(splitFile, copied);
                            // Split code executes through the same guest classloader.
                            // Apply the representation translation to any legacy split
                            // that contains direct Build.VERSION references too.
                            LegacyGuestSdkCompat.translateIfLegacy(copied);
                            copied.setReadOnly();
                            newSplitPaths.add(copied.getAbsolutePath());'''
    text = replace_once(text, old_split, new_split, "translate private split APK copies")
    path.write_text(text, encoding="utf-8")


def verify(root: Path) -> None:
    pm = (root / "Bcore/src/main/java/top/niunaijun/blackbox/core/system/pm/BPackageManagerService.java").read_text(encoding="utf-8")
    copy = (root / "Bcore/src/main/java/top/niunaijun/blackbox/core/system/pm/installer/CopyExecutor.java").read_text(encoding="utf-8")
    if "LegacyGuestSdkCompat.Result guestSdk" in pm:
        raise SystemExit("[guest-sdk-postparse] executable translation still occurs before PackageParser certificates")
    if "LegacyGuestSdkCompat.translateIfLegacy(apkFile) must NOT run here" not in pm:
        raise SystemExit("[guest-sdk-postparse] preparse invariant marker missing")
    if "PackageParserCompat.collectCertificates(parser, aPackage, 0);" not in pm:
        raise SystemExit("[guest-sdk-postparse] expected original certificate collection path missing")
    for invariant in [
        "LegacyGuestSdkCompat.translateIfLegacy(newFile)",
        "LegacyGuestSdkCompat.translateIfLegacy(copied)",
        "PackageParser has already collected certificates",
    ]:
        if invariant not in copy:
            raise SystemExit(f"[guest-sdk-postparse] verification failed: {invariant}")
    print("[guest-sdk-postparse] original signatures preserved; private executable APK translation verified")


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: patch_guest_sdk_postparse.py <NewBlackbox-root>")
    root = Path(sys.argv[1]).resolve()
    remove_preparse_translation(root)
    patch_copy_executor(root)
    verify(root)


if __name__ == "__main__":
    main()
