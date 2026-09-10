import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

void main() {
  WidgetsFlutterBinding.ensureInitialized();
  runApp(const AppCompatApp());
}

class AppCompatApp extends StatelessWidget {
  const AppCompatApp({super.key});

  @override
  Widget build(BuildContext context) {
    const ink = Color(0xFF101828);
    const blue = Color(0xFF175CD3);
    return MaterialApp(
      title: 'AppCompat',
      debugShowCheckedModeBanner: false,
      theme: ThemeData(
        useMaterial3: true,
        colorScheme: ColorScheme.fromSeed(
          seedColor: blue,
          brightness: Brightness.light,
          surface: Colors.white,
        ),
        scaffoldBackgroundColor: const Color(0xFFF8FAFC),
        textTheme: const TextTheme(
          headlineMedium: TextStyle(
            color: ink,
            fontSize: 27,
            height: 1.12,
            fontWeight: FontWeight.w700,
            letterSpacing: -0.7,
          ),
          titleLarge: TextStyle(
            color: ink,
            fontSize: 18,
            fontWeight: FontWeight.w700,
            letterSpacing: -0.2,
          ),
          bodyLarge: TextStyle(color: Color(0xFF344054), height: 1.45),
          bodyMedium: TextStyle(color: Color(0xFF475467), height: 1.4),
        ),
        cardTheme: const CardThemeData(
          color: Colors.white,
          elevation: 0,
          margin: EdgeInsets.zero,
          shape: RoundedRectangleBorder(
            side: BorderSide(color: Color(0xFFE4E7EC)),
            borderRadius: BorderRadius.all(Radius.circular(12)),
          ),
        ),
        filledButtonTheme: FilledButtonThemeData(
          style: FilledButton.styleFrom(
            backgroundColor: blue,
            foregroundColor: Colors.white,
            minimumSize: const Size(0, 48),
            padding: const EdgeInsets.symmetric(horizontal: 18),
            shape: RoundedRectangleBorder(
              borderRadius: BorderRadius.circular(8),
            ),
            textStyle: const TextStyle(fontWeight: FontWeight.w700),
          ),
        ),
        outlinedButtonTheme: OutlinedButtonThemeData(
          style: OutlinedButton.styleFrom(
            foregroundColor: ink,
            minimumSize: const Size(0, 48),
            padding: const EdgeInsets.symmetric(horizontal: 18),
            side: const BorderSide(color: Color(0xFFD0D5DD)),
            shape: RoundedRectangleBorder(
              borderRadius: BorderRadius.circular(8),
            ),
            textStyle: const TextStyle(fontWeight: FontWeight.w700),
          ),
        ),
      ),
      home: const HomePage(),
    );
  }
}

class NativeBridge {
  static const _channel = MethodChannel('com.appcompat.runtime/bridge');

  static Future<ApkReport?> selectAndAnalyze() async {
    final raw = await _channel.invokeMapMethod<dynamic, dynamic>('selectAndAnalyze');
    if (raw == null) return null;
    return ApkReport.fromMap(raw);
  }

  static Future<Map<String, dynamic>> runVirtual(ApkReport report) async {
    final raw = await _channel.invokeMapMethod<dynamic, dynamic>(
      'runVirtual',
      <String, dynamic>{
        'path': report.path,
        'packageName': report.packageName,
      },
    );
    return Map<String, dynamic>.from(raw ?? const {});
  }

  static Future<void> installNormally(ApkReport report) {
    return _channel.invokeMethod<void>(
      'installNormally',
      <String, dynamic>{'uri': report.uri},
    );
  }

  static Future<List<VirtualApp>> listApps() async {
    final raw = await _channel.invokeListMethod<dynamic>('listVirtualApps') ?? const [];
    return raw
        .map((item) => VirtualApp.fromMap(Map<dynamic, dynamic>.from(item as Map)))
        .toList();
  }

  static Future<bool> launch(String packageName) async {
    return await _channel.invokeMethod<bool>(
          'launchVirtual',
          <String, dynamic>{'packageName': packageName},
        ) ??
        false;
  }

  static Future<void> remove(String packageName) {
    return _channel.invokeMethod<void>(
      'removeVirtual',
      <String, dynamic>{'packageName': packageName},
    );
  }

  static Future<Map<String, dynamic>> engineStatus() async {
    final raw = await _channel.invokeMapMethod<dynamic, dynamic>('engineStatus');
    return Map<String, dynamic>.from(raw ?? const {});
  }
}

class ApkReport {
  const ApkReport({
    required this.name,
    required this.packageName,
    required this.versionName,
    required this.path,
    required this.uri,
    required this.targetSdk,
    required this.minSdk,
    required this.hostSdk,
    required this.sizeBytes,
    required this.abis,
    required this.dexCount,
    required this.hasNativeCode,
    required this.abiCompatible,
    required this.lowTargetBlocked,
    required this.route,
    required this.confidence,
    required this.issues,
  });

  factory ApkReport.fromMap(Map<dynamic, dynamic> map) => ApkReport(
        name: '${map['name'] ?? 'Unknown app'}',
        packageName: '${map['packageName'] ?? ''}',
        versionName: '${map['versionName'] ?? '—'}',
        path: '${map['path'] ?? ''}',
        uri: '${map['uri'] ?? ''}',
        targetSdk: (map['targetSdk'] as num?)?.toInt() ?? 0,
        minSdk: (map['minSdk'] as num?)?.toInt() ?? 0,
        hostSdk: (map['hostSdk'] as num?)?.toInt() ?? 0,
        sizeBytes: (map['sizeBytes'] as num?)?.toInt() ?? 0,
        abis: (map['abis'] as List?)?.map((e) => '$e').toList() ?? const [],
        dexCount: (map['dexCount'] as num?)?.toInt() ?? 0,
        hasNativeCode: map['hasNativeCode'] == true,
        abiCompatible: map['abiCompatible'] != false,
        lowTargetBlocked: map['lowTargetBlocked'] == true,
        route: '${map['route'] ?? 'virtual'}',
        confidence: '${map['confidence'] ?? 'Medium'}',
        issues: (map['issues'] as List?)?.map((e) => '$e').toList() ?? const [],
      );

  final String name;
  final String packageName;
  final String versionName;
  final String path;
  final String uri;
  final int targetSdk;
  final int minSdk;
  final int hostSdk;
  final int sizeBytes;
  final List<String> abis;
  final int dexCount;
  final bool hasNativeCode;
  final bool abiCompatible;
  final bool lowTargetBlocked;
  final String route;
  final String confidence;
  final List<String> issues;

  String get sizeLabel {
    final mb = sizeBytes / (1024 * 1024);
    return mb >= 1 ? '${mb.toStringAsFixed(1)} MB' : '${(sizeBytes / 1024).round()} KB';
  }

  String get routeLabel {
    switch (route) {
      case 'native':
        return 'Native capable';
      case 'limited':
        return 'Limited';
      default:
        return 'Compatibility runtime';
    }
  }
}

class VirtualApp {
  const VirtualApp({required this.name, required this.packageName});

  factory VirtualApp.fromMap(Map<dynamic, dynamic> map) => VirtualApp(
        name: '${map['name'] ?? map['packageName'] ?? 'Legacy app'}',
        packageName: '${map['packageName'] ?? ''}',
      );

  final String name;
  final String packageName;
}

class HomePage extends StatefulWidget {
  const HomePage({super.key});

  @override
  State<HomePage> createState() => _HomePageState();
}

class _HomePageState extends State<HomePage> {
  ApkReport? _report;
  List<VirtualApp> _apps = const [];
  bool _busy = false;
  bool _engineReady = false;
  String? _notice;

  @override
  void initState() {
    super.initState();
    _refresh();
  }

  Future<void> _refresh() async {
    try {
      final status = await NativeBridge.engineStatus();
      final apps = await NativeBridge.listApps();
      if (!mounted) return;
      setState(() {
        _engineReady = status['ready'] == true;
        _apps = apps;
      });
    } catch (_) {
      if (mounted) setState(() => _engineReady = false);
    }
  }

  Future<void> _selectApk() async {
    if (_busy) return;
    setState(() {
      _busy = true;
      _notice = null;
    });
    try {
      final report = await NativeBridge.selectAndAnalyze();
      if (!mounted || report == null) return;
      setState(() => _report = report);
    } on PlatformException catch (e) {
      if (mounted) setState(() => _notice = e.message ?? 'Could not read this APK.');
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  Future<void> _runVirtual() async {
    final report = _report;
    if (report == null || !report.abiCompatible || _busy) return;
    setState(() {
      _busy = true;
      _notice = 'Preparing compatibility runtime…';
    });
    try {
      final result = await NativeBridge.runVirtual(report);
      if (!mounted) return;
      final launched = result['launched'] == true;
      setState(() {
        _notice = launched
            ? 'App launched in the compatibility runtime.'
            : '${result['message'] ?? 'The app could not be launched.'}';
      });
      await _refresh();
    } on PlatformException catch (e) {
      if (mounted) setState(() => _notice = e.message ?? 'Compatibility launch failed.');
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  Future<void> _normalInstall() async {
    final report = _report;
    if (report == null || _busy) return;
    try {
      await NativeBridge.installNormally(report);
    } on PlatformException catch (e) {
      if (mounted) setState(() => _notice = e.message ?? 'Android could not open the installer.');
    }
  }

  Future<void> _launch(VirtualApp app) async {
    if (_busy) return;
    setState(() {
      _busy = true;
      _notice = null;
    });
    try {
      final ok = await NativeBridge.launch(app.packageName);
      if (mounted && !ok) setState(() => _notice = 'The compatibility runtime could not start ${app.name}.');
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  Future<void> _remove(VirtualApp app) async {
    if (_busy) return;
    setState(() => _busy = true);
    try {
      await NativeBridge.remove(app.packageName);
      await _refresh();
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(
        backgroundColor: Colors.white,
        surfaceTintColor: Colors.white,
        elevation: 0,
        scrolledUnderElevation: 0,
        titleSpacing: 20,
        title: const Row(
          children: [
            _Mark(),
            SizedBox(width: 10),
            Text('AppCompat', style: TextStyle(fontWeight: FontWeight.w800, fontSize: 18)),
          ],
        ),
        bottom: const PreferredSize(
          preferredSize: Size.fromHeight(1),
          child: Divider(height: 1, color: Color(0xFFE4E7EC)),
        ),
      ),
      body: SafeArea(
        child: ListView(
          padding: const EdgeInsets.fromLTRB(20, 24, 20, 40),
          children: [
            Text('Run older Android apps', style: Theme.of(context).textTheme.headlineMedium),
            const SizedBox(height: 9),
            const Text(
              'Inspect an APK first, then use the lightest compatible execution path available on this device.',
              style: TextStyle(fontSize: 15, color: Color(0xFF475467), height: 1.45),
            ),
            const SizedBox(height: 17),
            _RuntimeStatus(ready: _engineReady),
            const SizedBox(height: 20),
            if (_report == null) _ImportCard(busy: _busy, onPressed: _selectApk) else _ReportCard(
              report: _report!,
              busy: _busy,
              onChooseAnother: _selectApk,
              onRun: _runVirtual,
              onInstall: _normalInstall,
            ),
            if (_notice != null) ...[
              const SizedBox(height: 12),
              _Notice(text: _notice!),
            ],
            const SizedBox(height: 30),
            Row(
              children: [
                Expanded(child: Text('Compatibility library', style: Theme.of(context).textTheme.titleLarge)),
                if (_apps.isNotEmpty)
                  Text('${_apps.length}', style: const TextStyle(color: Color(0xFF667085), fontWeight: FontWeight.w600)),
              ],
            ),
            const SizedBox(height: 10),
            if (_apps.isEmpty)
              const _EmptyLibrary()
            else
              ..._apps.map((app) => Padding(
                    padding: const EdgeInsets.only(bottom: 8),
                    child: _LibraryRow(
                      app: app,
                      disabled: _busy,
                      onLaunch: () => _launch(app),
                      onRemove: () => _remove(app),
                    ),
                  )),
            const SizedBox(height: 24),
            const Text(
              'APK analysis and virtual app data stay on this device. Some legacy apps cannot be recovered when their server, DRM, signing dependency, required hardware, or CPU runtime no longer exists.',
              style: TextStyle(fontSize: 12.5, color: Color(0xFF667085), height: 1.5),
            ),
          ],
        ),
      ),
    );
  }
}

class _Mark extends StatelessWidget {
  const _Mark();
  @override
  Widget build(BuildContext context) => Container(
        width: 28,
        height: 28,
        decoration: BoxDecoration(
          color: const Color(0xFF101828),
          borderRadius: BorderRadius.circular(7),
        ),
        alignment: Alignment.center,
        child: const Text('A', style: TextStyle(color: Colors.white, fontWeight: FontWeight.w800)),
      );
}

class _RuntimeStatus extends StatelessWidget {
  const _RuntimeStatus({required this.ready});
  final bool ready;
  @override
  Widget build(BuildContext context) => Row(
        children: [
          Container(
            width: 8,
            height: 8,
            decoration: BoxDecoration(
              shape: BoxShape.circle,
              color: ready ? const Color(0xFF12B76A) : const Color(0xFFF79009),
            ),
          ),
          const SizedBox(width: 8),
          Text(
            ready ? 'Compatibility engine ready' : 'Compatibility engine initializing',
            style: const TextStyle(fontSize: 13, color: Color(0xFF475467), fontWeight: FontWeight.w600),
          ),
        ],
      );
}

class _ImportCard extends StatelessWidget {
  const _ImportCard({required this.busy, required this.onPressed});
  final bool busy;
  final VoidCallback onPressed;
  @override
  Widget build(BuildContext context) => Card(
        child: Padding(
          padding: const EdgeInsets.all(18),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.stretch,
            children: [
              const Text('Choose a legacy APK', style: TextStyle(fontSize: 16, fontWeight: FontWeight.w700, color: Color(0xFF101828))),
              const SizedBox(height: 6),
              const Text('AppCompat checks SDK level, native CPU libraries and common compatibility risks before running it.'),
              const SizedBox(height: 17),
              FilledButton.icon(
                onPressed: busy ? null : onPressed,
                icon: busy
                    ? const SizedBox(width: 17, height: 17, child: CircularProgressIndicator(strokeWidth: 2, color: Colors.white))
                    : const Icon(Icons.file_open_outlined, size: 20),
                label: Text(busy ? 'Inspecting APK…' : 'Select APK'),
              ),
            ],
          ),
        ),
      );
}

class _ReportCard extends StatelessWidget {
  const _ReportCard({
    required this.report,
    required this.busy,
    required this.onChooseAnother,
    required this.onRun,
    required this.onInstall,
  });
  final ApkReport report;
  final bool busy;
  final VoidCallback onChooseAnother;
  final VoidCallback onRun;
  final VoidCallback onInstall;

  @override
  Widget build(BuildContext context) {
    final limited = !report.abiCompatible;
    return Card(
      child: Padding(
        padding: const EdgeInsets.all(18),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            Row(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Expanded(
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      Text(report.name, style: const TextStyle(fontSize: 18, fontWeight: FontWeight.w800, color: Color(0xFF101828))),
                      const SizedBox(height: 3),
                      Text(report.packageName, maxLines: 1, overflow: TextOverflow.ellipsis, style: const TextStyle(fontSize: 12.5, color: Color(0xFF667085))),
                    ],
                  ),
                ),
                const SizedBox(width: 10),
                _Pill(label: report.routeLabel, limited: limited),
              ],
            ),
            const SizedBox(height: 17),
            Wrap(
              spacing: 8,
              runSpacing: 8,
              children: [
                _Metric(label: 'Target', value: 'API ${report.targetSdk}'),
                _Metric(label: 'Minimum', value: 'API ${report.minSdk}'),
                _Metric(label: 'Size', value: report.sizeLabel),
                _Metric(label: 'DEX', value: '${report.dexCount}'),
              ],
            ),
            const SizedBox(height: 14),
            _DetailLine(
              title: 'CPU',
              value: report.abis.isEmpty ? 'No native libraries detected' : report.abis.join(', '),
            ),
            _DetailLine(title: 'Recommended path', value: report.routeLabel),
            _DetailLine(title: 'Compatibility outlook', value: report.confidence),
            if (report.issues.isNotEmpty) ...[
              const Divider(height: 28),
              ...report.issues.take(4).map((issue) => Padding(
                    padding: const EdgeInsets.only(bottom: 8),
                    child: Row(
                      crossAxisAlignment: CrossAxisAlignment.start,
                      children: [
                        const Icon(Icons.info_outline, size: 17, color: Color(0xFF667085)),
                        const SizedBox(width: 8),
                        Expanded(child: Text(issue, style: const TextStyle(fontSize: 13, color: Color(0xFF475467)))),
                      ],
                    ),
                  )),
            ],
            const SizedBox(height: 11),
            FilledButton(
              onPressed: busy || limited ? null : onRun,
              child: Text(limited ? 'CPU bridge unavailable on this device' : (busy ? 'Preparing…' : 'Run with AppCompat')),
            ),
            if (!report.lowTargetBlocked && report.abiCompatible) ...[
              const SizedBox(height: 8),
              OutlinedButton(onPressed: busy ? null : onInstall, child: const Text('Install normally instead')),
            ],
            const SizedBox(height: 4),
            TextButton(onPressed: busy ? null : onChooseAnother, child: const Text('Choose another APK')),
          ],
        ),
      ),
    );
  }
}

class _Metric extends StatelessWidget {
  const _Metric({required this.label, required this.value});
  final String label;
  final String value;
  @override
  Widget build(BuildContext context) => Container(
        padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 8),
        decoration: BoxDecoration(
          color: const Color(0xFFF9FAFB),
          border: Border.all(color: const Color(0xFFEAECF0)),
          borderRadius: BorderRadius.circular(7),
        ),
        child: Text('$label  $value', style: const TextStyle(fontSize: 12.5, color: Color(0xFF344054), fontWeight: FontWeight.w600)),
      );
}

class _Pill extends StatelessWidget {
  const _Pill({required this.label, required this.limited});
  final String label;
  final bool limited;
  @override
  Widget build(BuildContext context) => Container(
        padding: const EdgeInsets.symmetric(horizontal: 9, vertical: 5),
        decoration: BoxDecoration(
          color: limited ? const Color(0xFFFFFAEB) : const Color(0xFFEFF8FF),
          borderRadius: BorderRadius.circular(99),
        ),
        child: Text(
          label,
          style: TextStyle(
            fontSize: 11.5,
            fontWeight: FontWeight.w700,
            color: limited ? const Color(0xFFB54708) : const Color(0xFF175CD3),
          ),
        ),
      );
}

class _DetailLine extends StatelessWidget {
  const _DetailLine({required this.title, required this.value});
  final String title;
  final String value;
  @override
  Widget build(BuildContext context) => Padding(
        padding: const EdgeInsets.only(top: 7),
        child: Row(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            SizedBox(width: 128, child: Text(title, style: const TextStyle(fontSize: 12.5, color: Color(0xFF667085)))),
            Expanded(child: Text(value, style: const TextStyle(fontSize: 12.5, color: Color(0xFF344054), fontWeight: FontWeight.w600))),
          ],
        ),
      );
}

class _Notice extends StatelessWidget {
  const _Notice({required this.text});
  final String text;
  @override
  Widget build(BuildContext context) => Container(
        padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 10),
        decoration: BoxDecoration(
          color: const Color(0xFFF2F4F7),
          borderRadius: BorderRadius.circular(8),
        ),
        child: Text(text, style: const TextStyle(fontSize: 13, color: Color(0xFF344054))),
      );
}

class _EmptyLibrary extends StatelessWidget {
  const _EmptyLibrary();
  @override
  Widget build(BuildContext context) => const Card(
        child: Padding(
          padding: EdgeInsets.all(17),
          child: Text('Apps you import into the compatibility runtime will appear here.', style: TextStyle(fontSize: 13.5)),
        ),
      );
}

class _LibraryRow extends StatelessWidget {
  const _LibraryRow({required this.app, required this.disabled, required this.onLaunch, required this.onRemove});
  final VirtualApp app;
  final bool disabled;
  final VoidCallback onLaunch;
  final VoidCallback onRemove;
  @override
  Widget build(BuildContext context) => Card(
        child: Padding(
          padding: const EdgeInsets.fromLTRB(14, 10, 8, 10),
          child: Row(
            children: [
              Container(
                width: 35,
                height: 35,
                alignment: Alignment.center,
                decoration: BoxDecoration(color: const Color(0xFFF2F4F7), borderRadius: BorderRadius.circular(8)),
                child: Text(app.name.isEmpty ? '?' : app.name.substring(0, 1).toUpperCase(), style: const TextStyle(fontWeight: FontWeight.w800, color: Color(0xFF344054))),
              ),
              const SizedBox(width: 11),
              Expanded(
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Text(app.name, maxLines: 1, overflow: TextOverflow.ellipsis, style: const TextStyle(fontWeight: FontWeight.w700, color: Color(0xFF101828))),
                    Text(app.packageName, maxLines: 1, overflow: TextOverflow.ellipsis, style: const TextStyle(fontSize: 11.5, color: Color(0xFF667085))),
                  ],
                ),
              ),
              IconButton(onPressed: disabled ? null : onLaunch, tooltip: 'Run', icon: const Icon(Icons.play_arrow_rounded)),
              PopupMenuButton<String>(
                enabled: !disabled,
                onSelected: (value) {
                  if (value == 'remove') onRemove();
                },
                itemBuilder: (_) => const [PopupMenuItem(value: 'remove', child: Text('Remove from AppCompat'))],
              ),
            ],
          ),
        ),
      );
}
