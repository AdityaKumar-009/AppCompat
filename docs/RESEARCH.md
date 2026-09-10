# AppCompat Compatibility Engineering Report

## Executive finding

A useful legacy-APK compatibility product is feasible on modern Android, but no ordinary third-party application can truthfully guarantee 100% compatibility with every historical APK. Android applications can depend on framework behavior, Binder services, CPU ABIs, Google/vendor services, package signatures, DRM, remote servers, hardware, kernel interfaces and security policies that no longer exist. AppCompat therefore uses a progressive strategy: inspect first, run natively when appropriate, use a userspace virtual framework when Android behavior is the blocker, and identify hard blockers rather than pretending they were repaired.

The first implementation uses Flutter only for the product UI. APK parsing and execution are Android-native. The virtual backend is the Apache-2.0 NewBlackbox engine pinned to commit `89b59836c66f173756a4ae258cf379a957649820` (2026-07-18), rather than an unpinned dependency. AppCompat disables the engine's optional remote log-sender destination and does not surface anti-detection, identity spoofing or unrelated modification controls.

## Why old APKs fail on new Android

### 1. Installation policy is not the same as runtime incompatibility

Android 14 blocks normal installation of apps targeting an SDK lower than 23. Android 15 raises the installation floor to target SDK 24. Google's own documentation exposes an ADB-only `--bypass-low-target-sdk-block` testing switch, which demonstrates an important distinction: some APKs are blocked by install policy even though ART/framework execution could otherwise work.

AppCompat avoids using the normal package installer for its virtual path. An APK is copied to private app storage, parsed, and then installed into the virtual package manager. The UI only offers Android's normal installer when the analyzer believes the package is not subject to the known low-target block.

Sources:
- Android 14 behavior changes: https://developer.android.com/about/versions/14/behavior-changes-all
- Android 15 behavior changes: https://developer.android.com/about/versions/15/behavior-changes-all

### 2. Android's compatibility framework is primarily a platform/developer mechanism

Modern Android already contains per-change compatibility switches so developers can test behavior changes. Those switches are extremely useful evidence for the design of a compatibility layer, but a normal consumer app cannot simply toggle arbitrary platform compatibility changes for another normally installed package. AppCompat therefore needs to host legacy packages inside its own virtual framework and proxy the Android services they expect.

Source:
- Android compatibility framework tools: https://developer.android.com/guide/app-compatibility/test-debug

### 3. CPU architecture can be a hard blocker

Pure Java/Kotlin APKs are relatively portable because ART executes their bytecode on the host architecture. Native APKs contain `.so` files compiled for ABIs such as `armeabi-v7a`, `arm64-v8a`, `x86` or `x86_64`. A strict 64-bit-only Android device that exposes no 32-bit runtime cannot directly load an APK whose native component is 32-bit-only.

A userspace virtual package manager does not itself solve instruction-set translation. The current AppCompat build detects this condition before launch and reports it as a CPU bridge blocker. A future full-coverage tier would need a legally distributable ARM32-to-ARM64 binary translator or an isolated legacy guest OS. Pretending that a Java/framework hook library solves this would produce crashes rather than compatibility.

Source:
- Android 64-bit application guidance: https://developer.android.com/google/play/requirements/64-bit

### 4. Removed/hidden APIs are progressively constrained

Old applications sometimes call non-SDK interfaces. Android has progressively restricted these interfaces, and the restriction set changes across releases. Virtualization engines can proxy many framework calls, but vendor/private interfaces may still be absent or changed beneath the proxy layer.

Source:
- Restrictions on non-SDK interfaces: https://developer.android.com/guide/app-compatibility/restrictions-non-sdk-interfaces

### 5. Storage semantics changed substantially

Legacy applications may assume arbitrary writable `/sdcard/...` paths. Scoped storage and later media/storage changes invalidate many of those assumptions. A virtual filesystem can redirect a large fraction of paths, but apps that exchange files with external apps through hard-coded paths may need per-app translation logic.

Source:
- Android storage behavior changes: https://developer.android.com/about/versions/11/privacy/storage

### 6. Rewriting an APK destroys its original signature

Static APK rewriting sounds attractive for manifest/API patching, but modifying signed APK bytes invalidates the developer's original signature. Re-signing can break signature-level permissions, upgrade continuity, licensing and backend checks. AppCompat therefore prefers runtime virtualization over rewriting the selected APK.

Source:
- Android APK signing / apksigner: https://developer.android.com/tools/apksigner

## Runtime choice

### NewBlackbox

NewBlackbox is an actively maintained fork of the BlackBox userspace Android virtualization project and is Apache-2.0 licensed. Its core exposes virtual package installation, application launch, virtual users, package management and extensive framework/service hooks. Its current source builds ARM64 and ARMv7 native components and contains Android 14+/15+/16-oriented compatibility work.

AppCompat integrates the engine as source modules instead of copying its UI or product features. The upstream revision is downloaded by `scripts/fetch_engine.sh` and pinned by commit hash for reproducibility.

Sources:
- NewBlackbox: https://github.com/ALEX5402/NewBlackbox
- Blacks-BlackBox Android 14+/16-oriented fork: https://github.com/Black00Z/Blacks-BlackBox
- Original BlackBox lineage: https://github.com/FBlackBox/BlackBox

## AppCompat execution pipeline

```text
APK selected through Android document provider
            |
            v
Private on-device copy
            |
            v
PackageManager + ZIP inspection
  - package / version
  - minSdk / targetSdk
  - requested permissions
  - DEX count
  - bundled native ABIs
            |
            v
Compatibility decision
  +------------------+--------------------+
  |                                       |
  v                                       v
Normal Android path                 AppCompat runtime
when viable                         when legacy behavior
  |                                       |
  v                                       v
System installer                    Virtual package install
                                           |
                                           v
                                  Framework/service hooks
                                           |
                                           v
                                      Launch APK
```

No APK is uploaded for analysis.

## Compatibility envelope

| APK class | Expected AppCompat v0.1 outcome | Main remaining risk |
| --- | --- | --- |
| Old Java/Kotlin, self-contained | High likelihood | Removed framework/vendor behavior |
| Old Java/Kotlin, legacy storage | High-to-medium | Hard-coded shared paths |
| Matching-ABI native app/game | Medium | Native/JNI assumptions and graphics |
| 32-bit-only native APK on device with 32-bit runtime | Medium | Engine/native hooks and app JNI behavior |
| 32-bit-only native APK on strict 64-bit-only device | Not solved in v0.1 | Requires binary translation/legacy VM |
| App bound to dead web service | Not recoverable generically | Server is gone |
| DRM/signature-bound app | Low/variable | Integrity/license checks |
| Old Google Play Services dependency | Variable | Service/backend/API version coupling |
| Obsolete physical hardware/API | Variable or impossible | Hardware no longer exists |

These are engineering categories, not fabricated compatibility percentages. A meaningful percentage can only be produced after testing a representative APK corpus across real Android versions and device ABIs.

## Product KPIs

The prototype is designed around measurable KPIs rather than a claimed universal percentage:

1. **No-cloud APK analysis.** Selected APK bytes remain on-device.
2. **One primary workflow.** Select APK -> inspect -> run; advanced internals are not exposed in the default UI.
3. **No false success.** ABI/install-policy blockers are shown before a launch attempt.
4. **Low idle overhead.** No full legacy Android VM is booted when a userspace framework is sufficient.
5. **Repeatable backend.** The virtualization engine is commit-pinned instead of tracking mutable `main` at build time.
6. **Privacy hardening.** The upstream optional remote log sender is disabled by `CompatRuntime`.
7. **Safe fallback.** Android's own installer remains available for packages that can use native installation.
8. **Recoverability.** Virtualized packages persist in an AppCompat library and can be launched or removed without repeatedly selecting the source APK.

## Distribution constraint

The compatibility backend benefits from an older host `targetSdkVersion` because newer target behavior further restricts hidden/system compatibility techniques. The prototype therefore compiles against Android API 36 while targeting API 28. This is suitable for a directly distributed/sideloaded research build, but it does not satisfy current Google Play target-SDK requirements for new app submissions. A Play-distributed edition would require a different architecture and would lose some virtualization capability.

Google Play's target API policy is documented at:
- https://support.google.com/googleplay/android-developer/answer/11926878

## Security model

Running a decade-old APK is inherently riskier than running maintained software. AppCompat improves isolation by storing imported APKs and virtual app state inside its own sandbox, but the host application necessarily declares capabilities required by the virtualized apps it is expected to serve. Android still controls dangerous runtime permissions. AppCompat does not silently grant Android runtime permissions to itself.

The project intentionally does not expose:
- device identity spoofing,
- anti-detection controls,
- root-hiding controls,
- signature/integrity bypass controls,
- arbitrary security-policy disabling.

Those are not required for the compatibility objective.

## Next compatibility tiers

The largest legitimate engineering gain after v0.1 is not more UI. It is runtime coverage:

1. Build a regression corpus of legal/open-source APKs spanning Android 2.x through modern releases.
2. Record launch, rendering, storage, camera, audio, notification, location and networking results on Android 10-16.
3. Add per-API compatibility shims only for failures observed in that corpus.
4. Introduce split-APK/APKS import.
5. Add a permission broker that maps guest runtime permission requests to host Android prompts with clear guest attribution.
6. Investigate a distributable 32-bit translation tier for strict 64-bit-only devices.
7. Consider a full legacy guest only as the last fallback because it carries substantially higher storage, memory and startup cost.

That progression gives AppCompat a credible path toward broad compatibility without claiming an impossible universal guarantee.
