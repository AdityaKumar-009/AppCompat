#!/usr/bin/env python3
"""Route legacy guest Java reflection through the compatibility shim.

Binder proxies cannot translate an API that disappeared before a Binder call occurs.
A common old-app pattern is Class.getMethod("removedHiddenApi", ...).invoke(...).
Rewrite only the reflection plumbing, preserving its register layout:

  Class.getMethod/getDeclaredMethod -> LegacyReflectionCompat static wrappers
  Method.invoke                     -> LegacyReflectionCompat static wrapper

The shim delegates ordinary reflection unchanged and only synthesizes explicitly
supported removed Android APIs. This creates reusable infrastructure for future legacy
framework migrations without package-name special cases.
"""
from __future__ import annotations

import sys
from pathlib import Path


def replace_once(text: str, old: str, new: str, label: str) -> str:
    count = text.count(old)
    if count == 0:
        if new in text:
            print(f"[legacy-reflection] {label}: already applied")
            return text
        raise SystemExit(f"[legacy-reflection] {label}: source pattern not found")
    if count != 1:
        raise SystemExit(f"[legacy-reflection] {label}: expected one match, found {count}")
    print(f"[legacy-reflection] {label}: applied")
    return text.replace(old, new, 1)


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: patch_legacy_reflection_translation.py <NewBlackbox-root>")
    root = Path(sys.argv[1]).resolve()
    path = root / (
        "Bcore/src/main/java/top/niunaijun/blackbox/utils/compat/LegacyGuestSdkCompat.java"
    )
    text = path.read_text(encoding="utf-8")

    old_import = '''import org.jf.dexlib2.Opcodes;
import org.jf.dexlib2.dexbacked.DexBackedDexFile;'''
    new_import = '''import org.jf.dexlib2.Opcode;
import org.jf.dexlib2.Opcodes;
import org.jf.dexlib2.dexbacked.DexBackedDexFile;
import org.jf.dexlib2.iface.instruction.Instruction;
import org.jf.dexlib2.iface.instruction.ReferenceInstruction;
import org.jf.dexlib2.iface.instruction.formats.Instruction35c;
import org.jf.dexlib2.iface.instruction.formats.Instruction3rc;
import org.jf.dexlib2.iface.reference.MethodReference;
import org.jf.dexlib2.immutable.instruction.ImmutableInstruction35c;
import org.jf.dexlib2.immutable.instruction.ImmutableInstruction3rc;
import org.jf.dexlib2.immutable.reference.ImmutableMethodReference;'''
    text = replace_once(text, old_import, new_import, "import instruction rewrite primitives")

    old_rewriter_import = '''import org.jf.dexlib2.rewriter.DexRewriter;
import org.jf.dexlib2.rewriter.Rewriter;'''
    new_rewriter_import = '''import org.jf.dexlib2.rewriter.DexRewriter;
import org.jf.dexlib2.rewriter.InstructionRewriter;
import org.jf.dexlib2.rewriter.Rewriter;'''
    text = replace_once(
        text, old_rewriter_import, new_rewriter_import,
        "import default instruction rewriter")

    old_constant = '''    private static final byte[] SOURCE_BYTES = ascii(SOURCE_DESCRIPTOR);'''
    new_constant = '''    private static final byte[] SOURCE_BYTES = ascii(SOURCE_DESCRIPTOR);
    private static final byte[] REFLECTION_CLASS_BYTES = ascii("Ljava/lang/Class;");
    private static final byte[] REFLECTION_METHOD_BYTES = ascii("Ljava/lang/reflect/Method;");
    private static final String REFLECTION_SHIM =
            "Lappcompat/compat/LegacyReflectionCompat;";'''
    text = replace_once(text, old_constant, new_constant, "declare reflection rewrite markers")

    text = replace_once(
        text,
        '''                if (containsBytes(data, SOURCE_BYTES)) matchingDexFiles++;''',
        '''                if (needsStructuralRewrite(data)) matchingDexFiles++;''',
        "scan reflective DEX files",
    )
    text = replace_once(
        text,
        '''                    if (isDexName(name) && isDex(data) && containsBytes(data, SOURCE_BYTES)) {''',
        '''                    if (isDexName(name) && isDex(data) && needsStructuralRewrite(data)) {''',
        "rewrite reflective DEX files",
    )

    old_module = '''            @Override
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
        });'''
    new_module = '''            @Override
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

            @Override
            public Rewriter<Instruction> getInstructionRewriter(final Rewriters rewriters) {
                final InstructionRewriter delegate = new InstructionRewriter(rewriters);
                return new Rewriter<Instruction>() {
                    @Override
                    public Instruction rewrite(Instruction value) {
                        Instruction translated = rewriteReflectionInvoke(value, replacements);
                        if (translated != null) return translated;
                        return delegate.rewrite(value);
                    }
                };
            }
        });'''
    text = replace_once(text, old_module, new_module, "rewrite reflection invoke opcodes")

    marker = '''    private static DexBackedDexFile parseDex(byte[] data) throws Exception {'''
    helpers = r'''    private static boolean needsStructuralRewrite(byte[] data) {
        if (containsBytes(data, SOURCE_BYTES)) return true;
        return containsBytes(data, REFLECTION_CLASS_BYTES)
                && containsBytes(data, REFLECTION_METHOD_BYTES);
    }

    /**
     * invoke-virtual and invoke-static use the same register count here because the
     * original receiver becomes the first explicit argument of the static shim.
     */
    private static Instruction rewriteReflectionInvoke(
            Instruction instruction, int[] replacements) {
        if (!(instruction instanceof ReferenceInstruction)) return null;
        Object reference = ((ReferenceInstruction) instruction).getReference();
        if (!(reference instanceof MethodReference)) return null;
        MethodReference method = (MethodReference) reference;

        ImmutableMethodReference target = null;
        if (isClassReflectionLookup(method, "getMethod")) {
            target = new ImmutableMethodReference(
                    REFLECTION_SHIM,
                    "getMethod",
                    Arrays.asList("Ljava/lang/Class;", "Ljava/lang/String;", "[Ljava/lang/Class;"),
                    "Ljava/lang/reflect/Method;");
        } else if (isClassReflectionLookup(method, "getDeclaredMethod")) {
            target = new ImmutableMethodReference(
                    REFLECTION_SHIM,
                    "getDeclaredMethod",
                    Arrays.asList("Ljava/lang/Class;", "Ljava/lang/String;", "[Ljava/lang/Class;"),
                    "Ljava/lang/reflect/Method;");
        } else if (isMethodInvoke(method)) {
            target = new ImmutableMethodReference(
                    REFLECTION_SHIM,
                    "invoke",
                    Arrays.asList(
                            "Ljava/lang/reflect/Method;",
                            "Ljava/lang/Object;",
                            "[Ljava/lang/Object;"),
                    "Ljava/lang/Object;");
        }
        if (target == null) return null;

        if (instruction instanceof Instruction35c) {
            Instruction35c invoke = (Instruction35c) instruction;
            if (instruction.getOpcode() != Opcode.INVOKE_VIRTUAL) return null;
            replacements[0]++;
            return new ImmutableInstruction35c(
                    Opcode.INVOKE_STATIC,
                    invoke.getRegisterCount(),
                    invoke.getRegisterC(),
                    invoke.getRegisterD(),
                    invoke.getRegisterE(),
                    invoke.getRegisterF(),
                    invoke.getRegisterG(),
                    target);
        }
        if (instruction instanceof Instruction3rc) {
            Instruction3rc invoke = (Instruction3rc) instruction;
            if (instruction.getOpcode() != Opcode.INVOKE_VIRTUAL_RANGE) return null;
            replacements[0]++;
            return new ImmutableInstruction3rc(
                    Opcode.INVOKE_STATIC_RANGE,
                    invoke.getStartRegister(),
                    invoke.getRegisterCount(),
                    target);
        }
        return null;
    }

    private static boolean isClassReflectionLookup(MethodReference method, String name) {
        if (!"Ljava/lang/Class;".equals(method.getDefiningClass())) return false;
        if (!name.equals(method.getName())) return false;
        if (!"Ljava/lang/reflect/Method;".equals(method.getReturnType())) return false;
        java.util.List<? extends CharSequence> params = method.getParameterTypes();
        return params.size() == 2
                && "Ljava/lang/String;".contentEquals(params.get(0))
                && "[Ljava/lang/Class;".contentEquals(params.get(1));
    }

    private static boolean isMethodInvoke(MethodReference method) {
        if (!"Ljava/lang/reflect/Method;".equals(method.getDefiningClass())) return false;
        if (!"invoke".equals(method.getName())) return false;
        if (!"Ljava/lang/Object;".equals(method.getReturnType())) return false;
        java.util.List<? extends CharSequence> params = method.getParameterTypes();
        return params.size() == 2
                && "Ljava/lang/Object;".contentEquals(params.get(0))
                && "[Ljava/lang/Object;".contentEquals(params.get(1));
    }

'''
    if "private static Instruction rewriteReflectionInvoke" not in text:
        if marker not in text:
            raise SystemExit("[legacy-reflection] helper insertion point not found")
        text = text.replace(marker, helpers + marker, 1)
        print("[legacy-reflection] reflection rewrite helpers: applied")

    required = (
        "REFLECTION_SHIM",
        "needsStructuralRewrite(data)",
        "rewriteReflectionInvoke(value, replacements)",
        "Opcode.INVOKE_STATIC_RANGE",
        '"getDeclaredMethod"',
        '"Ljava/lang/reflect/Method;".equals(method.getDefiningClass())',
    )
    for item in required:
        if item not in text:
            raise SystemExit(f"[legacy-reflection] verification failed: {item}")

    path.write_text(text, encoding="utf-8")
    print("[legacy-reflection] transparent legacy Java reflection translation verified")


if __name__ == "__main__":
    main()
