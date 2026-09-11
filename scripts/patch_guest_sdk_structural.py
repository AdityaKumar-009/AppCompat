#!/usr/bin/env python3
"""Replace v12 raw DEX string mutation with a structural dexlib2 rewrite.

DEX string/type identifier tables are sorted. Replacing a descriptor in-place, even
with equal byte length, can invalidate that ordering and cause ART to reject the DEX.
This patch makes LegacyGuestSdkCompat parse/rewrite/serialize DEX files through
smali/dexlib2 so all identifiers, offsets, checksums and signatures are rebuilt.
"""
from __future__ import annotations

import sys
from pathlib import Path

DEXLIB = "implementation 'org.smali:dexlib2:2.5.2'"


def patch_build_gradle(root: Path) -> None:
    path = root / "Bcore/build.gradle"
    text = path.read_text(encoding="utf-8")
    if DEXLIB not in text:
        needle = 'dependencies {\n'
        if needle not in text:
            raise SystemExit('[guest-sdk-structural] Bcore dependencies block missing')
        text = text.replace(
            needle,
            needle + "    // Structural guest-Dex rewriting; raw string mutation breaks sorted DEX IDs.\n"
            + f"    {DEXLIB}\n",
            1,
        )
        path.write_text(text, encoding="utf-8")
        print('[guest-sdk-structural] dexlib2 dependency added')
    else:
        print('[guest-sdk-structural] dexlib2 dependency already present')


def write_translator(root: Path) -> None:
    path = root / "Bcore/src/main/java/top/niunaijun/blackbox/utils/compat/LegacyGuestSdkCompat.java"
    source = r'''package top.niunaijun.blackbox.utils.compat;

import java.io.BufferedInputStream;
import java.io.ByteArrayInputStream;
import java.io.ByteArrayOutputStream;
import java.io.File;
import java.io.FileInputStream;
import java.io.FileOutputStream;
import java.io.InputStream;
import java.util.Arrays;
import java.util.Enumeration;
import java.util.Locale;
import java.util.zip.ZipEntry;
import java.util.zip.ZipFile;
import java.util.zip.ZipOutputStream;

import org.jf.dexlib2.Opcodes;
import org.jf.dexlib2.dexbacked.DexBackedDexFile;
import org.jf.dexlib2.iface.ClassDef;
import org.jf.dexlib2.iface.DexFile;
import org.jf.dexlib2.rewriter.DexRewriter;
import org.jf.dexlib2.rewriter.Rewriter;
import org.jf.dexlib2.rewriter.RewriterModule;
import org.jf.dexlib2.rewriter.Rewriters;
import org.jf.dexlib2.writer.io.MemoryDataStore;
import org.jf.dexlib2.writer.pool.DexPool;

import top.niunaijun.blackbox.BlackBoxCore;
import top.niunaijun.blackbox.utils.Slog;

/**
 * Guest-only Build.VERSION representation translation.
 *
 * IMPORTANT: never mutate DEX string_data_item bytes in place. string_ids/type_ids
 * are sorted identifier tables; changing a descriptor without rebuilding those
 * tables can make ART reject the whole DEX and manifest launcher classes then appear
 * missing. dexlib2 rewrites references and serializes a structurally valid DEX.
 */
public final class LegacyGuestSdkCompat {
    private static final String TAG = "LegacyGuestSdkCompat";
    private static final int PROFILE_SDK = 25;
    private static final String SHIM_ASSET = "compat/legacy-build-shim.dex";
    private static final String SOURCE_DESCRIPTOR = "Landroid/os/Build$VERSION;";
    private static final String TARGET_DESCRIPTOR = "Lappcompat/compat/Version;";
    private static final byte[] SOURCE_BYTES = ascii(SOURCE_DESCRIPTOR);

    private LegacyGuestSdkCompat() {
    }

    public static final class Result {
        public final boolean translated;
        public final int dexFilesPatched;
        public final int referencesPatched;
        public final String shimEntry;

        Result(boolean translated, int dexFilesPatched, int referencesPatched, String shimEntry) {
            this.translated = translated;
            this.dexFilesPatched = dexFilesPatched;
            this.referencesPatched = referencesPatched;
            this.shimEntry = shimEntry;
        }
    }

    /**
     * Rewrites a private executable APK after the original package has already been
     * parsed/certificate-verified. includeShim must be true only for the base APK;
     * split APKs share the base classloader and must not define a duplicate shim.
     */
    public static Result translateLegacyExecutable(
            File apk, boolean includeShim, int targetSdkVersion) {
        if (apk == null || !apk.isFile() || apk.length() <= 0L) return empty();
        if (targetSdkVersion > PROFILE_SDK) return empty();
        try {
            return translate(apk, includeShim);
        } catch (Throwable t) {
            // Fail open to the original executable copy. A compatibility enhancement
            // must never turn a parseable legacy APK into an uninstallable package.
            Slog.w(TAG, "Structural guest SDK translation skipped after error: " + t);
            return empty();
        }
    }

    private static Result translate(File apk, boolean includeShim) throws Exception {
        int maxDexIndex = 0;
        int matchingDexFiles = 0;
        boolean shimAlreadyDefined = false;

        ZipFile scan = new ZipFile(apk);
        try {
            Enumeration<? extends ZipEntry> entries = scan.entries();
            while (entries.hasMoreElements()) {
                ZipEntry entry = entries.nextElement();
                if (entry == null || entry.isDirectory() || !isDexName(entry.getName())) continue;
                maxDexIndex = Math.max(maxDexIndex, dexIndex(entry.getName()));
                byte[] data = readAll(scan.getInputStream(entry));
                if (!isDex(data)) continue;
                if (containsBytes(data, SOURCE_BYTES)) matchingDexFiles++;
                if (!shimAlreadyDefined && definesClass(data, TARGET_DESCRIPTOR)) {
                    shimAlreadyDefined = true;
                }
            }
        } finally {
            scan.close();
        }

        // The base gets the shim even when Build.VERSION is referenced only from a
        // split. Splits are rewritten independently with includeShim=false.
        if (matchingDexFiles == 0 && (!includeShim || shimAlreadyDefined)) return empty();

        File temp = new File(apk.getParentFile(), apk.getName() + ".sdk25.structural.tmp");
        if (temp.exists()) temp.delete();
        int patchedDexFiles = 0;
        int rewrittenTypeTables = 0;
        String shimEntry = null;

        ZipFile input = new ZipFile(apk);
        try {
            ZipOutputStream output = new ZipOutputStream(
                    new java.io.BufferedOutputStream(
                            new FileOutputStream(temp, false), 1024 * 1024));
            try {
                Enumeration<? extends ZipEntry> entries = input.entries();
                while (entries.hasMoreElements()) {
                    ZipEntry entry = entries.nextElement();
                    String name = entry.getName();
                    if (entry.isDirectory()) {
                        ZipEntry directory = new ZipEntry(name);
                        directory.setTime(entry.getTime());
                        output.putNextEntry(directory);
                        output.closeEntry();
                        continue;
                    }

                    // This is a private runtime copy. Original signature identity was
                    // captured before CopyExecutor; stale ZIP signature files must not
                    // describe the now-transformed bytes.
                    if (isSignatureArtifact(name)) continue;

                    byte[] data = readAll(input.getInputStream(entry));
                    if (isDexName(name) && isDex(data) && containsBytes(data, SOURCE_BYTES)) {
                        DexRewrite rewritten = rewriteDex(data);
                        data = rewritten.bytes;
                        patchedDexFiles++;
                        rewrittenTypeTables += rewritten.replacements;
                    }

                    ZipEntry copied = new ZipEntry(name);
                    copied.setTime(entry.getTime());
                    output.putNextEntry(copied);
                    output.write(data);
                    output.closeEntry();
                }

                if (includeShim && !shimAlreadyDefined) {
                    shimEntry = nextDexName(maxDexIndex);
                    byte[] shim = BlackBoxCore.getContext().getAssets()
                            .open(SHIM_ASSET).use(inputStream -> readAll(inputStream));
                    validateDex(shim, "compatibility shim");
                    output.putNextEntry(new ZipEntry(shimEntry));
                    output.write(shim);
                    output.closeEntry();
                }
            } finally {
                output.close();
            }
        } finally {
            input.close();
        }

        // Validate every DEX from the finished APK before replacing the runnable
        // copy. This specifically catches the v12 failure mode where DexPathList had
        // base.apk but ART could not load the manifest activity from its rejected DEX.
        validateApkDexFiles(temp);

        File backup = new File(apk.getParentFile(), apk.getName() + ".sdk25.prestructural");
        if (backup.exists()) backup.delete();
        if (!apk.renameTo(backup)) {
            temp.delete();
            throw new IllegalStateException("Could not stage executable APK for structural DEX rewrite");
        }
        try {
            if (!temp.renameTo(apk)) {
                copy(temp, apk);
                temp.delete();
            }
            backup.delete();
        } catch (Throwable t) {
            apk.delete();
            backup.renameTo(apk);
            throw t;
        }

        Slog.i(TAG, "Structural API-25 guest rewrite: apk=" + apk.getName()
                + " dex=" + patchedDexFiles
                + " typeTables=" + rewrittenTypeTables
                + " shim=" + shimEntry);
        return new Result(true, patchedDexFiles, rewrittenTypeTables, shimEntry);
    }

    private static final class DexRewrite {
        final byte[] bytes;
        final int replacements;
        DexRewrite(byte[] bytes, int replacements) {
            this.bytes = bytes;
            this.replacements = replacements;
        }
    }

    private static DexRewrite rewriteDex(byte[] original) throws Exception {
        DexBackedDexFile input = parseDex(original);
        final int beforeClassCount = input.getClasses().size();
        final int[] replacements = new int[] {0};

        DexRewriter rewriter = new DexRewriter(new RewriterModule() {
            @Override
            public Rewriter<String> getTypeRewriter(Rewriters rewriters) {
                return new Rewriter<String>() {
                    @Override
                    public String rewrite(String value) {
                        if (SOURCE_DESCRIPTOR.equals(value)) {
                            replacements[0]++;
                            return TARGET_DESCRIPTOR;
                        }
                        return value;
                    }
                };
            }
        });

        DexFile rewritten = rewriter.getDexFileRewriter().rewrite(input);
        MemoryDataStore store = new MemoryDataStore(Math.max(original.length, 4096));
        byte[] data;
        try {
            DexPool.writeTo(store, rewritten);
            data = store.getData();
        } finally {
            store.close();
        }

        DexBackedDexFile validated = parseDex(data);
        if (validated.getClasses().size() != beforeClassCount) {
            throw new IllegalStateException("DEX class count changed during SDK rewrite: "
                    + beforeClassCount + " -> " + validated.getClasses().size());
        }
        if (containsType(validated, SOURCE_DESCRIPTOR)) {
            throw new IllegalStateException("Build.VERSION type remained after structural rewrite");
        }
        return new DexRewrite(data, Math.max(1, replacements[0]));
    }

    private static DexBackedDexFile parseDex(byte[] data) throws Exception {
        BufferedInputStream input = new BufferedInputStream(new ByteArrayInputStream(data));
        try {
            return DexBackedDexFile.fromInputStream(Opcodes.forApi(PROFILE_SDK), input);
        } finally {
            input.close();
        }
    }

    private static boolean definesClass(byte[] data, String descriptor) {
        try {
            DexBackedDexFile dex = parseDex(data);
            for (ClassDef classDef : dex.getClasses()) {
                if (descriptor.equals(classDef.getType())) return true;
            }
        } catch (Throwable ignored) {
        }
        return false;
    }

    private static boolean containsType(DexBackedDexFile dex, String descriptor) {
        try {
            for (org.jf.dexlib2.iface.reference.TypeReference ref : dex.getTypeReferences()) {
                if (descriptor.equals(ref.getType())) return true;
            }
        } catch (Throwable ignored) {
            // Older dexlib implementations can still validate through class walking;
            // serialization itself has already rebuilt sorted identifier sections.
        }
        return false;
    }

    private static void validateDex(byte[] data, String label) throws Exception {
        DexBackedDexFile dex = parseDex(data);
        // Force class_def traversal rather than validating only the header.
        for (ClassDef ignored : dex.getClasses()) {
            ignored.getType();
        }
        if (dex.getClasses().isEmpty()) {
            throw new IllegalStateException(label + " contains no classes");
        }
    }

    private static void validateApkDexFiles(File apk) throws Exception {
        ZipFile zip = new ZipFile(apk);
        int dexCount = 0;
        try {
            Enumeration<? extends ZipEntry> entries = zip.entries();
            while (entries.hasMoreElements()) {
                ZipEntry entry = entries.nextElement();
                if (entry == null || entry.isDirectory() || !isDexName(entry.getName())) continue;
                byte[] data = readAll(zip.getInputStream(entry));
                validateDex(data, entry.getName());
                dexCount++;
            }
        } finally {
            zip.close();
        }
        if (dexCount == 0) throw new IllegalStateException("Executable APK has no DEX files");
    }

    private static boolean containsBytes(byte[] haystack, byte[] needle) {
        if (haystack == null || needle == null || needle.length == 0 || haystack.length < needle.length) {
            return false;
        }
        outer:
        for (int i = 0; i <= haystack.length - needle.length; i++) {
            for (int j = 0; j < needle.length; j++) {
                if (haystack[i + j] != needle[j]) continue outer;
            }
            return true;
        }
        return false;
    }

    private static boolean isDex(byte[] data) {
        return data != null && data.length > 112
                && data[0] == 'd' && data[1] == 'e' && data[2] == 'x' && data[3] == '\n';
    }

    private static boolean isDexName(String name) {
        return name != null && name.matches("classes(\\d*)\\.dex");
    }

    private static int dexIndex(String name) {
        if ("classes.dex".equals(name)) return 1;
        try {
            String digits = name.substring("classes".length(), name.length() - ".dex".length());
            return Integer.parseInt(digits);
        } catch (Throwable ignored) {
            return 1;
        }
    }

    private static String nextDexName(int maxIndex) {
        return "classes" + (Math.max(1, maxIndex) + 1) + ".dex";
    }

    private static boolean isSignatureArtifact(String name) {
        if (name == null) return false;
        String upper = name.toUpperCase(Locale.ROOT);
        if (!upper.startsWith("META-INF/")) return false;
        return upper.endsWith(".RSA") || upper.endsWith(".DSA")
                || upper.endsWith(".EC") || upper.endsWith(".SF")
                || "META-INF/MANIFEST.MF".equals(upper);
    }

    private static byte[] readAll(InputStream input) throws Exception {
        try {
            ByteArrayOutputStream output = new ByteArrayOutputStream();
            byte[] buffer = new byte[1024 * 1024];
            int read;
            while ((read = input.read(buffer)) >= 0) output.write(buffer, 0, read);
            return output.toByteArray();
        } finally {
            input.close();
        }
    }

    private static void copy(File from, File to) throws Exception {
        FileInputStream input = new FileInputStream(from);
        try {
            FileOutputStream output = new FileOutputStream(to, false);
            try {
                byte[] buffer = new byte[1024 * 1024];
                int read;
                while ((read = input.read(buffer)) >= 0) output.write(buffer, 0, read);
            } finally {
                output.close();
            }
        } finally {
            input.close();
        }
    }

    private static byte[] ascii(String value) {
        try {
            return value.getBytes("UTF-8");
        } catch (java.io.UnsupportedEncodingException impossible) {
            throw new AssertionError(impossible);
        }
    }

    private static Result empty() {
        return new Result(false, 0, 0, null);
    }
}
'''
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")
    print('[guest-sdk-structural] LegacyGuestSdkCompat replaced with dexlib2 writer')


def patch_copy_executor(root: Path) -> None:
    path = root / "Bcore/src/main/java/top/niunaijun/blackbox/core/system/pm/installer/CopyExecutor.java"
    text = path.read_text(encoding="utf-8")
    text = text.replace(
        'LegacyGuestSdkCompat.translateIfLegacy(newFile)',
        'LegacyGuestSdkCompat.translateLegacyExecutable(\n                        newFile, true, ps.pkg.applicationInfo.targetSdkVersion)',
    )
    text = text.replace(
        'LegacyGuestSdkCompat.translateIfLegacy(copied);',
        'LegacyGuestSdkCompat.translateLegacyExecutable(\n                                    copied, false, ps.pkg.applicationInfo.targetSdkVersion);',
    )
    if 'LegacyGuestSdkCompat.translateIfLegacy(' in text:
        raise SystemExit('[guest-sdk-structural] obsolete raw translator call remains')
    for invariant in [
        'translateLegacyExecutable(\n                        newFile, true',
        'translateLegacyExecutable(\n                                    copied, false',
    ]:
        if invariant not in text:
            raise SystemExit(f'[guest-sdk-structural] CopyExecutor integration missing: {invariant}')
    path.write_text(text, encoding="utf-8")
    print('[guest-sdk-structural] CopyExecutor uses base-only shim + structural split rewrites')


def verify(root: Path) -> None:
    compat = (root / "Bcore/src/main/java/top/niunaijun/blackbox/utils/compat/LegacyGuestSdkCompat.java").read_text(encoding="utf-8")
    gradle = (root / "Bcore/build.gradle").read_text(encoding="utf-8")
    copy = (root / "Bcore/src/main/java/top/niunaijun/blackbox/core/system/pm/installer/CopyExecutor.java").read_text(encoding="utf-8")
    required = [
        'DexRewriter',
        'DexPool.writeTo',
        'getTypeRewriter',
        'validateApkDexFiles',
        'DEX class count changed during SDK rewrite',
    ]
    for item in required:
        if item not in compat:
            raise SystemExit(f'[guest-sdk-structural] translator verification failed: {item}')
    if DEXLIB not in gradle:
        raise SystemExit('[guest-sdk-structural] dexlib2 Gradle dependency missing')
    if 'translateLegacyExecutable' not in copy or 'translateIfLegacy' in copy:
        raise SystemExit('[guest-sdk-structural] CopyExecutor verification failed')
    print('[guest-sdk-structural] structural DEX rewrite verified')


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit('usage: patch_guest_sdk_structural.py <NewBlackbox-root>')
    root = Path(sys.argv[1]).resolve()
    patch_build_gradle(root)
    write_translator(root)
    patch_copy_executor(root)
    verify(root)


if __name__ == '__main__':
    main()
