#!/usr/bin/env python3
"""Apply AppCompat's deterministic compatibility patches to the pinned engine.

The upstream BlackBox fork currently contains a broken final application-creation
fallback: it casts a package Context to Application. On real devices that throws a
ClassCastException, which is then wrapped as `RuntimeException: Unable to
makeApplication` before the guest's first Activity can start.

Keep this patch tiny, explicit and fail-fast so CI never silently builds against an
unexpected upstream source layout.
"""
from __future__ import annotations

import sys
from pathlib import Path


def replace_once(text: str, old: str, new: str, label: str) -> str:
    count = text.count(old)
    if count == 0:
        # Idempotency: accept a tree that already contains the replacement.
        if new in text:
            print(f"[patch-engine] {label}: already applied")
            return text
        raise SystemExit(f"[patch-engine] {label}: expected source pattern not found")
    if count != 1:
        raise SystemExit(f"[patch-engine] {label}: expected exactly one match, found {count}")
    print(f"[patch-engine] {label}: applied")
    return text.replace(old, new, 1)


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: patch_engine.py <NewBlackbox-root>")

    root = Path(sys.argv[1]).resolve()
    source = root / "Bcore/src/main/java/top/niunaijun/blackbox/app/BActivityThread.java"
    if not source.is_file():
        raise SystemExit(f"[patch-engine] engine source missing: {source}")

    text = source.read_text(encoding="utf-8")

    # The standard LoadedApk path is still preferred because it performs the exact
    # framework setup Android expects. If reflection/class-loader differences make it
    # return null, use the engine's existing custom-Application constructor and only
    # then its minimal Application wrapper. Never cast Context -> Application.
    text = replace_once(
        text,
        "                        application = (Application) packageContext;",
        "                        application = createApplicationWithFallback(applicationInfo);\n"
        "                        if (application == null) {\n"
        "                            application = createMinimalApplication(packageContext, packageName);\n"
        "                        }",
        "replace invalid Context-to-Application fallback",
    )

    # Hidden-API reflection and class loading can fail with LinkageError/Error types,
    # not only Exception. Contain those failures inside the application-creation
    # recovery ladder instead of aborting the whole virtual process immediately.
    text = replace_once(
        text,
        "            } catch (Exception makeAppException) {\n"
        "                Slog.e(TAG, \"Failed to makeApplication, trying fallback approach\", makeAppException);",
        "            } catch (Throwable makeAppException) {\n"
        "                Slog.e(TAG, \"Failed to makeApplication, trying fallback approach\", makeAppException);",
        "catch non-Exception makeApplication failures",
    )

    text = replace_once(
        text,
        "                } catch (Exception e) {\n"
        "                    Slog.e(TAG, \"Fallback makeApplication also failed\", e);",
        "                } catch (Throwable e) {\n"
        "                    Slog.e(TAG, \"Fallback makeApplication also failed\", e);",
        "catch non-Exception forced-default failures",
    )

    source.write_text(text, encoding="utf-8")

    # CI guardrails: these are the invariants this patch exists to enforce.
    verified = source.read_text(encoding="utf-8")
    if "application = (Application) packageContext;" in verified:
        raise SystemExit("[patch-engine] verification failed: invalid Context cast remains")
    if "application = createApplicationWithFallback(applicationInfo);" not in verified:
        raise SystemExit("[patch-engine] verification failed: custom Application fallback missing")
    if "application = createMinimalApplication(packageContext, packageName);" not in verified:
        raise SystemExit("[patch-engine] verification failed: minimal Application fallback missing")

    print("[patch-engine] application bootstrap recovery verified")


if __name__ == "__main__":
    main()
