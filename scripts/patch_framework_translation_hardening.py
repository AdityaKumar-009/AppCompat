#!/usr/bin/env python3
"""Harden the generated FrameworkTypeAdapter without broad exception swallowing."""
from __future__ import annotations

import sys
from pathlib import Path


def replace_once(text: str, old: str, new: str, label: str) -> str:
    count = text.count(old)
    if count == 0:
        if new in text:
            print(f"[framework-hardening] {label}: already applied")
            return text
        raise SystemExit(f"[framework-hardening] {label}: expected source pattern not found")
    if count != 1:
        raise SystemExit(f"[framework-hardening] {label}: expected one match, found {count}")
    print(f"[framework-hardening] {label}: applied")
    return text.replace(old, new, 1)


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: patch_framework_translation_hardening.py <NewBlackbox-root>")

    root = Path(sys.argv[1]).resolve()
    path = root / "Bcore/src/main/java/top/niunaijun/blackbox/utils/compat/FrameworkTypeAdapter.java"
    text = path.read_text(encoding="utf-8")

    text = replace_once(
        text,
        "import java.lang.reflect.Method;",
        "import java.lang.reflect.Method;\nimport java.lang.reflect.Modifier;",
        "import Modifier",
    )

    text = replace_once(
        text,
        "                || cause instanceof AbstractMethodError\n"
        "                || cause instanceof NoSuchMethodError\n"
        "                || cause instanceof IncompatibleClassChangeError;",
        "                || cause instanceof AbstractMethodError\n"
        "                || cause instanceof NoSuchMethodError\n"
        "                || cause instanceof NoSuchFieldError\n"
        "                || cause instanceof NoSuchMethodException\n"
        "                || cause instanceof NoSuchFieldException\n"
        "                || cause instanceof IllegalAccessException\n"
        "                || cause instanceof IndexOutOfBoundsException\n"
        "                || cause instanceof IncompatibleClassChangeError;",
        "recognize shifted signatures and hidden-member drift",
    )

    text = replace_once(
        text,
        "        if (Set.class.isAssignableFrom(expected)) {\n"
        "            return Collections.emptySet();\n"
        "        }\n"
        "        if (Collection.class.isAssignableFrom(expected)) {\n"
        "            return Collections.emptyList();\n"
        "        }\n"
        "        return null;\n"
        "    }\n\n"
        "    private static boolean isCompatible(Class<?> expected, Object value) {",
        "        if (Collection.class.isAssignableFrom(expected)) {\n"
        "            Object emptyCollection = emptyCollectionFor(expected);\n"
        "            if (emptyCollection != null) {\n"
        "                return emptyCollection;\n"
        "            }\n"
        "        }\n"
        "        return null;\n"
        "    }\n\n"
        "    private static Object emptyCollectionFor(Class<?> expected) {\n"
        "        // Prefer a real instance of a concrete framework return type so\n"
        "        // java.lang.reflect.Proxy never sees an interface-incompatible\n"
        "        // Collections.empty* implementation.\n"
        "        if (!expected.isInterface() && !Modifier.isAbstract(expected.getModifiers())) {\n"
        "            try {\n"
        "                java.lang.reflect.Constructor<?> constructor = expected.getDeclaredConstructor();\n"
        "                constructor.setAccessible(true);\n"
        "                Object instance = constructor.newInstance();\n"
        "                if (expected.isInstance(instance)) {\n"
        "                    return instance;\n"
        "                }\n"
        "            } catch (Throwable ignored) {\n"
        "            }\n"
        "        }\n"
        "        if (Set.class.isAssignableFrom(expected)\n"
        "                && expected.isAssignableFrom(LinkedHashSet.class)) {\n"
        "            return new LinkedHashSet<Object>();\n"
        "        }\n"
        "        if (Collection.class.isAssignableFrom(expected)\n"
        "                && expected.isAssignableFrom(ArrayList.class)) {\n"
        "            return new ArrayList<Object>();\n"
        "        }\n"
        "        return null;\n"
        "    }\n\n"
        "    private static boolean isCompatible(Class<?> expected, Object value) {",
        "return concrete-compatible collection defaults",
    )

    path.write_text(text, encoding="utf-8")

    verified = path.read_text(encoding="utf-8")
    for invariant in [
        "cause instanceof IndexOutOfBoundsException",
        "cause instanceof NoSuchMethodException",
        "cause instanceof NoSuchFieldException",
        "private static Object emptyCollectionFor(Class<?> expected)",
        "expected.isAssignableFrom(ArrayList.class)",
    ]:
        if invariant not in verified:
            raise SystemExit(f"[framework-hardening] verification failed: {invariant}")

    print("[framework-hardening] framework drift recovery hardened")


if __name__ == "__main__":
    main()
