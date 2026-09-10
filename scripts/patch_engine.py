#!/usr/bin/env python3
"""Apply AppCompat's deterministic compatibility patches to the pinned engine.

The pinned BlackBox fork has two fragile Application bootstrap paths that matter on
modern Android: its final fallback casts a package Context to Application, and its
manual Application constructor does not use Instrumentation.newApplication(), which
is the framework path that attaches the Context correctly. A detached Application
can fail later during providers/onCreate and gets flattened into the misleading
`RuntimeException: Unable to makeApplication` message.

Keep every patch explicit and fail-fast so CI never silently builds against an
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

    # Never cast Context -> Application. If framework makeApplication paths return
    # null, use a real Application creation path and only then the minimal wrapper.
    text = replace_once(
        text,
        "                        application = (Application) packageContext;",
        "                        startupStage = \"direct Application recovery\";\n"
        "                        application = createApplicationWithFallback(applicationInfo);\n"
        "                        if (application == null) {\n"
        "                            application = createMinimalApplication(packageContext, packageName);\n"
        "                        }",
        "replace invalid Context-to-Application fallback",
    )

    # Hidden-API reflection and class loading can fail with LinkageError/Error types,
    # not only Exception. Keep those failures inside the recovery ladder.
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

    # AOSP LoadedApk creates Applications through Instrumentation.newApplication(),
    # which instantiates the class and invokes Application.attach(Context). The
    # upstream manual newInstance() path skips that framework attach contract.
    text = replace_once(
        text,
        "            ClassLoader classLoader = getClassLoader(appInfo);\n"
        "            Class<?> appClass = classLoader.loadClass(appInfo.className);\n"
        "            Application application = (Application) appClass.newInstance();",
        "            Context appContext = createPackageContext(appInfo);\n"
        "            if (appContext == null) {\n"
        "                appContext = createFallbackContext(appInfo.packageName);\n"
        "            }\n"
        "            if (appContext == null) {\n"
        "                throw new IllegalStateException(\"No guest package Context available\");\n"
        "            }\n"
        "            ClassLoader classLoader = appContext.getClassLoader();\n"
        "            if (classLoader == null) {\n"
        "                classLoader = getClassLoader(appInfo);\n"
        "            }\n"
        "            String appClassName = TextUtils.isEmpty(appInfo.className)\n"
        "                    ? Application.class.getName() : appInfo.className;\n"
        "            Application application = AppInstrumentation.get().newApplication(\n"
        "                    classLoader, appClassName, appContext);",
        "create Application through Instrumentation",
    )

    # The secondary constructor is retained as a different recovery route, but it
    # must also attach using Instrumentation instead of returning a detached object.
    text = replace_once(
        text,
        "            Class<?> appClass = classLoader.loadClass(appInfo.className);\n"
        "            Application application = (Application) appClass.newInstance();",
        "            Context appContext = createPackageContext(appInfo);\n"
        "            if (appContext == null) {\n"
        "                appContext = createFallbackContext(appInfo.packageName);\n"
        "            }\n"
        "            if (appContext == null) {\n"
        "                throw new IllegalStateException(\"No guest package Context available for fallback\");\n"
        "            }\n"
        "            String appClassName = TextUtils.isEmpty(appInfo.className)\n"
        "                    ? Application.class.getName() : appInfo.className;\n"
        "            Application application = AppInstrumentation.get().newApplication(\n"
        "                    classLoader, appClassName, appContext);",
        "attach secondary Application fallback",
    )

    # Fallback wrappers still need a valid base context. Application does not
    # declare attachBaseContext; its hidden attach(Context) method is the framework
    # entry point. Fall back to ContextWrapper.attachBaseContext only if necessary.
    text = replace_once(
        text,
        "            try {\n"
        "                Method attachBaseContext = Application.class.getDeclaredMethod(\"attachBaseContext\", Context.class);\n"
        "                attachBaseContext.setAccessible(true);\n"
        "                attachBaseContext.invoke(application, packageContext);\n"
        "                Slog.d(TAG, \"Successfully attached base context to application: \" + appInfo.className);\n"
        "            } catch (Exception e) {\n"
        "                Slog.w(TAG, \"Could not attach base context to application: \" + e.getMessage());\n"
        "            }",
        "            try {\n"
        "                Method attach = Application.class.getDeclaredMethod(\"attach\", Context.class);\n"
        "                attach.setAccessible(true);\n"
        "                attach.invoke(application, packageContext);\n"
        "                Slog.d(TAG, \"Successfully attached application through framework attach(): \" + appInfo.className);\n"
        "            } catch (Throwable attachFailure) {\n"
        "                Slog.w(TAG, \"Application.attach failed, trying ContextWrapper attachBaseContext: \" + attachFailure.getMessage());\n"
        "                try {\n"
        "                    Method attachBaseContext = ContextWrapper.class.getDeclaredMethod(\"attachBaseContext\", Context.class);\n"
        "                    attachBaseContext.setAccessible(true);\n"
        "                    attachBaseContext.invoke(application, packageContext);\n"
        "                    Slog.d(TAG, \"Successfully attached base context through ContextWrapper: \" + appInfo.className);\n"
        "                } catch (Throwable baseAttachFailure) {\n"
        "                    Slog.e(TAG, \"Could not attach any base context to application\", baseAttachFailure);\n"
        "                }\n"
        "            }",
        "repair fallback Application context attach",
    )

    # The final createMinimalApplication() path contained the same invalid
    # getDeclaredMethod lookup, so fix it independently rather than assuming the
    # earlier Application fallback covered this code path.
    text = replace_once(
        text,
        "                try {\n"
        "                    Method attachBaseContext = Application.class.getDeclaredMethod(\"attachBaseContext\", Context.class);\n"
        "                    attachBaseContext.setAccessible(true);\n"
        "                    attachBaseContext.invoke(app, packageContext);\n"
        "                    Slog.d(TAG, \"Successfully attached base context to minimal application for \" + packageName);\n"
        "                } catch (Exception e) {\n"
        "                    Slog.w(TAG, \"Could not attach base context to minimal application: \" + e.getMessage());\n"
        "                }",
        "                try {\n"
        "                    Method attach = Application.class.getDeclaredMethod(\"attach\", Context.class);\n"
        "                    attach.setAccessible(true);\n"
        "                    attach.invoke(app, packageContext);\n"
        "                    Slog.d(TAG, \"Successfully attached minimal application through framework attach() for \" + packageName);\n"
        "                } catch (Throwable attachFailure) {\n"
        "                    Slog.w(TAG, \"Minimal Application.attach failed, trying ContextWrapper attachBaseContext: \" + attachFailure.getMessage());\n"
        "                    try {\n"
        "                        Method attachBaseContext = ContextWrapper.class.getDeclaredMethod(\"attachBaseContext\", Context.class);\n"
        "                        attachBaseContext.setAccessible(true);\n"
        "                        attachBaseContext.invoke(app, packageContext);\n"
        "                        Slog.d(TAG, \"Successfully attached minimal application through ContextWrapper for \" + packageName);\n"
        "                    } catch (Throwable baseAttachFailure) {\n"
        "                        Slog.e(TAG, \"Could not attach any base context to minimal application\", baseAttachFailure);\n"
        "                    }\n"
        "                }",
        "repair minimal Application context attach",
    )

    # Track the exact startup phase so any remaining guest-specific failure is no
    # longer flattened into the unhelpful 'Unable to makeApplication' wrapper.
    text = replace_once(
        text,
        "        Application application;\n        try {",
        "        Application application;\n"
        "        String startupStage = \"before Application creation\";\n"
        "        try {",
        "add startup stage diagnostics",
    )
    text = replace_once(
        text,
        "                application = BRLoadedApk.get(loadedApk).makeApplication(false, null);",
        "                startupStage = \"LoadedApk.makeApplication(custom)\";\n"
        "                application = BRLoadedApk.get(loadedApk).makeApplication(false, null);",
        "mark custom makeApplication stage",
    )
    text = replace_once(
        text,
        "                    application = BRLoadedApk.get(loadedApk).makeApplication(true, null);",
        "                    startupStage = \"LoadedApk.makeApplication(default)\";\n"
        "                    application = BRLoadedApk.get(loadedApk).makeApplication(true, null);",
        "mark default makeApplication stage",
    )
    text = replace_once(
        text,
        "            installProviders(mInitialApplication, bindData.processName, bindData.providers);",
        "            startupStage = \"content provider installation\";\n"
        "            installProviders(mInitialApplication, bindData.processName, bindData.providers);",
        "mark provider installation stage",
    )
    text = replace_once(
        text,
        "            AppInstrumentation.get().callApplicationOnCreate(application);",
        "            startupStage = \"Application.onCreate\";\n"
        "            AppInstrumentation.get().callApplicationOnCreate(application);",
        "mark Application.onCreate stage",
    )
    text = replace_once(
        text,
        "        } catch (Exception e) {\n"
        "            Slog.e(TAG, \"Critical error in handleBindApplication\", e);\n"
        "            throw new RuntimeException(\"Unable to makeApplication\", e);\n"
        "        }",
        "        } catch (Throwable e) {\n"
        "            Slog.e(TAG, \"Critical error during guest startup stage=\" + startupStage, e);\n"
        "            throw new RuntimeException(\"Guest startup failed at \" + startupStage + \" for \" + packageName, e);\n"
        "        }",
        "preserve startup stage in crash",
    )

    # Recovery helpers themselves should contain Error/LinkageError rather than
    # aborting before the next fallback can run.
    text = replace_once(
        text,
        "        } catch (Exception e) {\n            Slog.w(TAG, \"Failed to create application normally: \" + e.getMessage());",
        "        } catch (Throwable e) {\n            Slog.w(TAG, \"Failed to create application normally: \" + e.getMessage());",
        "catch Throwable in custom application recovery",
    )
    text = replace_once(
        text,
        "        } catch (Exception e) {\n            Slog.e(TAG, \"Fallback application creation failed: \" + e.getMessage());",
        "        } catch (Throwable e) {\n            Slog.e(TAG, \"Fallback application creation failed: \" + e.getMessage());",
        "catch Throwable in secondary application recovery",
    )
    text = replace_once(
        text,
        "            } catch (Exception wrapperException) {\n                Slog.e(TAG, \"Failed to create minimal application wrapper\", wrapperException);",
        "            } catch (Throwable wrapperException) {\n                Slog.e(TAG, \"Failed to create minimal application wrapper\", wrapperException);",
        "catch Throwable in minimal application recovery",
    )
    text = replace_once(
        text,
        "        } catch (Exception e) {\n            Slog.e(TAG, \"Error creating application: \" + e.getMessage());\n            return null;\n        }",
        "        } catch (Throwable e) {\n            Slog.e(TAG, \"Error creating application: \" + e.getMessage(), e);\n            return null;\n        }",
        "catch Throwable in direct Application constructor",
    )

    source.write_text(text, encoding="utf-8")

    # CI guardrails: these are the invariants this patch exists to enforce.
    verified = source.read_text(encoding="utf-8")
    required = [
        "application = createApplicationWithFallback(applicationInfo);",
        "application = createMinimalApplication(packageContext, packageName);",
        "AppInstrumentation.get().newApplication(",
        "Application.class.getDeclaredMethod(\"attach\", Context.class)",
        "ContextWrapper.class.getDeclaredMethod(\"attachBaseContext\", Context.class)",
        "Successfully attached minimal application through framework attach()",
        "Guest startup failed at ",
        "startupStage = \"Application.onCreate\";",
    ]
    if "application = (Application) packageContext;" in verified:
        raise SystemExit("[patch-engine] verification failed: invalid Context cast remains")
    if "Application.class.getDeclaredMethod(\"attachBaseContext\", Context.class)" in verified:
        raise SystemExit("[patch-engine] verification failed: invalid Application.attachBaseContext lookup remains")
    for invariant in required:
        if invariant not in verified:
            raise SystemExit(f"[patch-engine] verification failed: missing invariant: {invariant}")

    print("[patch-engine] application bootstrap translation verified")


if __name__ == "__main__":
    main()
