# AppCompat

AppCompat is an on-device legacy Android compatibility runtime with a Flutter interface and an Android-native virtualized execution backend.

> Status: active prototype. Compatibility is best-effort; no Android compatibility layer can guarantee that every historical APK will run, especially apps with dead servers/DRM, signature-bound services, obsolete hardware dependencies, or 32-bit-only native code on a strict 64-bit-only device.

## Architecture

- **Flutter UI** — minimal APK import, analysis, compatibility report, library and launch controls.
- **APK Analyzer** — reads manifest/package metadata and ZIP native libraries entirely on-device.
- **Native install path** — available for APKs the host Android version can install normally.
- **Virtual runtime** — uses the Apache-2.0 NewBlackbox engine pinned by `scripts/fetch_engine.sh` for legacy framework/service compatibility.
- **Safety boundary** — imported apps run in AppCompat's virtual namespace; AppCompat does not expose anti-detection or device-identity spoofing controls.

## Build

```bash
./scripts/fetch_engine.sh
flutter pub get
flutter build apk --debug
```

The GitHub Actions workflow builds an APK and publishes it as a workflow artifact.

See `docs/RESEARCH.md` for compatibility constraints and design rationale.