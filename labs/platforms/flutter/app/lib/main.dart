import 'dart:async';
import 'dart:convert';

import 'package:background_fetch/background_fetch.dart';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

// Flutter fetch-lab driver (WO-002) — bg-forge's lab protocol (labs/README.md), driving the Flutter plugin
// background_fetch.  Ported from bg-forge addc50501's labs/platforms/flutter/app/lib/main.dart, the Dart port of
// labs/platforms/_shared/labapi.js and labevents.js.  Its generic half is carried over as it stands, kept in step
// with the JavaScript: _log and the Log tab, the verdict poll and the whole view, _armEvents/_removeEvents,
// _runApiBatch, _ret, _typeOf, _thrown, the refusals (_Unexpressible, _as, _options), _callBeside, the slots,
// the beat and the tick.  The fetch half is the Capacitor driver's (labs/platforms/capacitor/app/www/index.html): the
// same LAB lines, in the same order.  The header reads 'Fetch Flutter Lab: <scenario>'.
//
// What differs from bg-forge's Flutter driver:
//  - configure(config, onFetch, onTimeout) is the boot call (fetch has no ready() or start() to make), and fetch events
//    arrive through its two callbacks, not through an on* member.  Dart's configure() listens to the event channel once
//    per isolate and KEEPS ITS FIRST CALLBACKS (background_fetch.dart:442), so every configure() here passes the same
//    pair, _fgFetch / _fgTimeout.  They log the delivery, hand it to every armed `events:` declaration of `configure`
//    (the facade in _events, Capacitor's EventBG), then finish(taskId) per the launch's knobs.
//  - A plain FlutterActivity is destroyed with its engine, so a recreation or a restart runs main() again, in a new
//    isolate whose event stream nothing listens to until it calls configure().  So configure() runs on EVERY boot, as
//    Capacitor's page does, while the first api batch, the anchor line and the slots run on boot 1 only.
//  - BackgroundFetchConfig and TaskConfig are typed classes with named parameters, with no raw map to fill (bg-forge's
//    Config.set()): the scenario's map is marshalled field by field, and the echo is the typed object's toMap(), which
//    is what reaches the SDK.
//  - The headless task, labHeadlessTask, runs in the plugin's own background engine, not main().  It reaches the lab's
//    native side through `lab/probe`, which the app's lab_probe plugin registers on every engine, that one included.
//  - The api table is background_fetch's.  There is no dart_members.g.dart: fetch's eight constants are a hand-written
//    table (_constants).
//  - `args` carries the process-wide boot counter (no separate `boot` call), so it is called once per engine boot.
//  - No per-tick getCurrentPosition, and no legacy `call` hooks (lab.yaml supports.calls: []).
// Every line is `LAB app=…` (the headless task's `LAB point=…`), printed through the Dart `flutter` logcat tag; the
// harness matches on substrings, never on line starts.
const _tag = 'LAB';
const _probe = MethodChannel('lab/probe');

/// (Tier C, C7.6) The Log tab: the last 200 strings passed to _log, newest last.  It only
/// appends, and nothing listens to _logCount unless the Log tab is on screen, so the buffer fills
/// while Test is selected without a rebuild.
const _logCap = 200;
final _logLines = <String>[];
final _logCount = ValueNotifier<int>(0);
final _tab = ValueNotifier<String>('Test');

void _log(String s) {
  print('$_tag app=$s');
  _logLines.add(s);
  if (_logLines.length > _logCap) _logLines.removeAt(0);
  _logCount.value++;
}

/// A value for a `key=value` field: the host reads `key=(\S+)`, so whitespace would cut it.
String _field(Object? v) => Uri.encodeComponent(v is PlatformException ? (v.message ?? v.code) : '$v');

int _msSince(DateTime t0) => DateTime.now().difference(t0).inMilliseconds;

// --- finishing a delivery: shared by the foreground callbacks and the headless task ----------------------------

/// The launch's knobs: the foreground's from `args`, the headless task's from `knobs` (the last foreground launch's,
/// persisted natively: a job started with no Activity has no launch intent).
typedef _Knobs = ({Object? finishDelayMs, bool withholdFinish, bool finishOnTimeout});
const _Knobs _defaultKnobs = (finishDelayMs: 1000, withholdFinish: false, finishOnTimeout: true);

_Knobs _knobsFrom(Map m) => (
      finishDelayMs: m.containsKey('finishDelayMs') ? m['finishDelayMs'] : _defaultKnobs.finishDelayMs,
      withholdFinish: m['withholdFinish'] == true,
      finishOnTimeout: m['finishOnTimeout'] != false,
    );

/// ms until finish(taskId), or null to withhold it: a fetch after finishDelayMs unless withholdFinish; a timeout at
/// once unless finishOnTimeout is false.  The JavaScript drivers' finishAfter().
int? _finishAfter(bool timeout, _Knobs knobs) {
  if (timeout) return knobs.finishOnTimeout ? 0 : null;
  if (knobs.withholdFinish) return null;
  final d = knobs.finishDelayMs;
  final ms = d is num ? d : d is String ? num.tryParse(d) : null;
  return ms != null && ms.isFinite && ms > 0 ? ms.round() : 0;
}

/// Calls finish(taskId) as the knobs say and reports it through `say(outcome, afterMs[, error])`.  Dart's finish() is
/// `async`, so a failure is its Future's (`rejected`); `threw` is a throw before it hands one back, which it cannot do,
/// kept so the line has the JavaScript drivers' shape.  `withheld` is said synchronously, before the first await.
Future<void> _finishPer(String taskId, bool timeout, _Knobs knobs,
    void Function(String outcome, Object after, [Object? error]) say) async {
  final after = _finishAfter(timeout, knobs);
  if (after == null) {
    say('withheld', '-');
    return;
  }
  await Future<void>.delayed(Duration(milliseconds: after));
  Future<void> pending;
  try {
    pending = BackgroundFetch.finish(taskId);
  } catch (e) {
    say('threw', after, e);
    return;
  }
  try {
    await pending;
    say('ok', after);
  } catch (e) {
    say('rejected', after, e);
  }
}

// --- the headless task ------------------------------------------------------------------------------------------

/// A job JobScheduler started with no Activity: the plugin's HeadlessTask starts a background FlutterEngine that runs
/// background_fetch's _headlessCallbackDispatcher, never main(), and it calls this.  So it is top-level, with the
/// entry-point pragma, and it touches no app state: nothing of the foreground driver exists in this isolate.
/// The dispatcher does not await it; the plugin ends the job on our finish(taskId).
///
/// Its lines are the Capacitor host's Java task's (BackgroundFetchHeadlessTask.java), text for text: `point=headless`,
/// `point=task-probe`, `point=task-api api=finish`, finishing per the knobs the LAST foreground launch persisted, on
/// the same rules as the foreground.  Nothing here logs `point=headless-…`: a headless counter reads every
/// `point=headless` line as a delivery.
@pragma('vm:entry-point')
Future<void> labHeadlessTask(HeadlessEvent event) async {
  final taskId = event.taskId;
  final timeout = event.timeout;
  print('$_tag point=headless event=${timeout ? 'timeout' : 'fetch'} taskId=$taskId timeout=$timeout');
  try {
    print('$_tag point=task-probe ${await _probe.invokeMethod<String>('probe')}');
  } catch (e) {
    print('$_tag point=task-probe probeError=${_field(e)}');
  }
  var knobs = _defaultKnobs;
  try {
    knobs = _knobsFrom(await _probe.invokeMethod<Map>('knobs') ?? const {});
  } catch (_) {
    // the defaults
  }
  final after = _finishAfter(timeout, knobs);
  if (after == null) {
    print('$_tag point=task-api api=finish args=$taskId outcome=withheld after_ms=-');
    return;
  }
  await Future<void>.delayed(Duration(milliseconds: after));
  try {
    await BackgroundFetch.finish(taskId);
    print('$_tag point=task-api api=finish args=$taskId outcome=ok after_ms=$after');
  } catch (e) {
    // The Java task's `message=<class>`: one word, whatever the failure carried.
    print('$_tag point=task-api api=finish args=$taskId outcome=threw after_ms=$after message=${e.runtimeType}');
  }
}

// --- the foreground driver --------------------------------------------------------------------------------------

final _status = ValueNotifier<String>('booting');
/// (WO-027) One screen line as coloured segments, mirroring the JavaScript drivers' {text, tone}.  Green and red are
/// the HOST's verdict, pushed by run.py and read back by _pollVerdict: a green tick for "the call answered" once sat
/// beside rows the host scored red.
typedef LabSegment = ({String text, String tone});
typedef LabLine = List<LabSegment>;

const _labTone = {
  'dim': Color(0xFF8A8A8E),
  'plain': Color(0xFF1C1C1E),
  'mark': Color(0xFF3D6FA3),   // an observation worth a look
  'pass': Color(0xFF1A7F37),   // the host says it holds
  'fail': Color(0xFFC0392B),   // the host says it does not
};

final _viewLines = ValueNotifier<List<LabLine>>([
  [(text: 'configure pending', tone: 'dim')]
]);

/// A plain-text line, for a view that has nothing to colour.
LabLine _plain(String text) => [(text: text, tone: 'plain')];
final _headline = ValueNotifier<String>('Fetch Flutter Lab');
String _scenario = 'lab';
String _nativeLabel = '';   // the native snapshot, the header's second line
int _ticks = 0, _boot = 0, _beats = 0, _fetches = 0, _timeouts = 0, _finished = 0;
int _apiRan = 0;   // (WO-027) hooks run so far, across batches
List _hooks = const [];
_Knobs _knobs = _defaultKnobs;
Future<int?>? _configuring;   // the last configure(): the first batch waits for it

void main() {
  WidgetsFlutterBinding.ensureInitialized();
  // Every boot, before runApp: the plugin persists the two callback handles natively, and its HeadlessTask reads them
  // when a job starts with no Activity.
  unawaited(BackgroundFetch.registerHeadlessTask(labHeadlessTask));
  runApp(const LabApp());
  unawaited(_driver().catchError((Object e) => _log('error ${_field(e)}')));
}

Future<void> _driver() async {
  final t0 = DateTime.now();
  final args = Map<String, dynamic>.from(await _probe.invokeMethod<Map>('args') ?? const {});
  final argsMs = _msSince(t0);
  _boot = (args['boot'] as num?)?.toInt() ?? 0;
  _log('boot n=$_boot args=${jsonEncode(args)}');
  // The beat: every 0.5 s from the boot line, touching neither the SDK nor the lab channel.  main() runs once per
  // engine, so there is one beat per engine.
  Timer.periodic(const Duration(milliseconds: 500), (_) => _log('beat n=${++_beats}'));
  // The host's verdict, every 0.25 s: run.py waits that plus a frame before its screenshot.  Android only for now, so
  // it never pauses while hidden (Capacitor's iOS rule: a periodic native call keeps an iOS app from suspending).
  Timer.periodic(const Duration(milliseconds: 250), (_) => _pollVerdict());
  final scenario = args['scenario'];
  _scenario = scenario is String && scenario.isNotEmpty ? scenario : 'lab';
  _nativeLabel = args['nativeLabel'] is String ? args['nativeLabel'] as String : '';
  _headline.value = 'Fetch Flutter Lab: $_scenario';

  final hooks = _json(args['hooks'], '[]');
  _hooks = hooks is List ? hooks : const [];
  _knobs = _knobsFrom(args);
  final events = _json(args['events'], '[]');
  _labEvents = events is List ? events : [];
  // labkit passes the view; with none, or one this driver does not draw, the fallback is its default: events if
  // declared, else api if there is an api hook, else the title.
  final view = args['view'];
  _labView = const ['api', 'events', 'title'].contains(view)
      ? view as String
      : _labEvents.isNotEmpty
          ? 'events'
          : _hooks.any((h) => h is Map && h['api'] is String) ? 'api' : 'title';
  _labHeader = 'boot=$_boot';
  _eventsHeader = '$_labHeader  armed ${_labEvents.length}';
  _redraw();

  // Every boot: the arm, and the configure() it may make or the driver makes instead.  The declarations armed at boot
  // come BEFORE the configure() that causes their deliveries; a boot-armed `configure` declaration with args makes
  // that configure() itself (the facade in _events), so the driver then makes none of its own.
  _labEventsArmed = _armEvents(_labEvents, 'boot', 0);
  final armConfigured =
      _labEvents.any((d) => d is Map && d['on'] == 'configure' && d['at_s'] == null && d.containsKey('args'));
  if (args['noConfigure'] == true) {
    _log('configure source=boot outcome=skipped reason=noConfigure');
  } else if (armConfigured) {
    _log('configure source=boot outcome=skipped reason=event-arm');
  } else {
    // BackgroundFetchConfig is flat: the config's keys are merged over the lab's defaults.
    final extra = _json(args['config'], '{}');
    final wanted = <String, dynamic>{
      'minimumFetchInterval': 15,
      'stopOnTerminate': args['stopOnTerminate'],
      'startOnBoot': false,
      'enableHeadless': true,
      if (extra is Map) ...extra.cast<String, dynamic>(),
    };
    BackgroundFetchConfig? config;
    try {
      config = _fetchConfigFrom(wanted, strict: false);
    } on _Unexpressible catch (e) {
      // Nothing was called, so nothing is echoed: configureOutcome reads `boot=unexpressible`, configDropped no-echo.
      _log('configure source=boot outcome=unexpressible why=${Uri.encodeComponent(e.why)}');
    }
    if (config != null) _configureWith(config, 'boot');
  }
  final tickGcp = _tickGcpFor(_hooks, _labEvents);   // the protocol's derived flag; no per-tick cause here

  final tConf = DateTime.now();
  if (_configuring != null) await _configuring;
  _log('timing argsMs=$argsMs configureMs=${_msSince(tConf)}');

  // (WO-027) The first batch — every `api` hook with no `at_s` — once configure() has settled, then the anchor line
  // and one timer per distinct `at_s`: `at_s` is seconds after THIS batch completes.  It is queued on the chain, never
  // awaited.  Boot 1 only: a recreated engine runs no batch.
  if (_boot == 1) {
    final tBatch = DateTime.now();
    _batchChain = _batchChain.then((_) async {
      _apiRan = await _runApiBatch('first', _apiRan);
      // (WO-027) The call schedule is on every view: redraw whichever is showing.
      _redraw();
      final times = _slotTimes(_hooks, _labEvents);
      _log(_anchorLine(_msSince(tBatch), times, tickGcp));
      _startSlots(times, _onSlot);
    });
  }
  Timer.periodic(const Duration(seconds: 4), (_) => _tick());
}

/// A JSON launch extra, or [fallback] for an empty one: JSON.parse(args.x || fallback).
Object? _json(Object? text, String fallback) => jsonDecode(text is String && text.isNotEmpty ? text : fallback);

// --- deliveries -------------------------------------------------------------------------------------------------

/// An armed `events:` declaration of `configure`: handed every delivery from then on.
class _Subscriber {
  final void Function(dynamic) onEvent;
  final void Function(dynamic)? onTimeout;
  _Subscriber(this.onEvent, this.onTimeout);
}

final _subscribers = <_Subscriber>[];

/// What the facade hands back, as the JavaScript facade's `{remove}`: `remove_at_s` unsubscribes the declaration.
class _Subscription {
  final void Function() _drop;
  _Subscription(this._drop);
  Future<void> remove() async => _drop();
}

/// One delivery from configure()'s callbacks.  Logged first, then handed to the declarations, then finished: the
/// `event` line always precedes the `finish` line it causes.  A declaration is handed what the plugin hands a
/// customer's callbacks, the bare taskId String (background_fetch.dart:451-457; the vocabulary's `emits: string`).  A
/// timeout goes to a declaration's onTimeout alone, which it passes with `errors: true`; onEvent never sees one.
void _onDelivery(String taskId, bool timeout) {
  final n = timeout ? ++_timeouts : ++_fetches;
  _log('event=${timeout ? 'timeout' : 'fetch'} n=$n taskId=${_field(taskId)}');
  for (final s in List.of(_subscribers)) {
    try {
      if (!timeout) {
        s.onEvent(taskId);
      } else {
        s.onTimeout?.call(taskId);
      }
    } catch (e) {
      _log('event-dispatch outcome=threw message=${_field(e)}');
    }
  }
  unawaited(_finishPer(taskId, timeout, _knobs, (outcome, after, [e]) {
    if (outcome == 'ok') _finished++;
    _log('finish taskId=${_field(taskId)} timeout=$timeout after_ms=$after outcome=$outcome'
        '${e != null ? ' message=${_field(e)}' : ''}');
  }));
}

// The one pair every configure() passes: Dart binds the first pair an isolate hands it, and only that one.
void _fgFetch(String taskId) => _onDelivery(taskId, false);
void _fgTimeout(String taskId) => _onDelivery(taskId, true);

/// The real configure(), always with the driver's own callbacks, echoing the config first (configDropped reads the
/// last `config json=` line, which must END with the JSON).  The echo is the typed config's toMap(): what the plugin
/// sends the SDK, requiredNetworkType as its index.
Future<int?> _configureWith(BackgroundFetchConfig config, String source) {
  _log('config json=${jsonEncode(config.toMap())}');
  final t0 = DateTime.now();
  final done = () async {
    try {
      final status = await BackgroundFetch.configure(config, _fgFetch, _fgTimeout);
      _log('configure source=$source outcome=ok status=$status ms=${_msSince(t0)}');
      return status;
    } catch (e) {
      _log('configure source=$source outcome=rejected ms=${_msSince(t0)} message=${_field(e)}');
      return null;
    }
  }();
  _configuring = done;
  return done;
}

/// (WO-027) The `events:` port of labevents.js.  The JavaScript drivers reach a member reflectively; Dart cannot, so
/// this table is the Flutter half, and a member missing from it reports the same `outcome=absent` a JavaScript driver
/// would observe.  For fetch the only member that delivers is configure, whose callbacks are the driver's own: so
/// `configure` here SUBSCRIBES the declaration to the driver's deliveries (`fail`, which `errors: true` asks for,
/// takes the timeouts) and, when the declaration carries `args`, makes the real configure() with them, as given: no
/// defaults merged.  `args` is [_noArgs] when the declaration has no `args` key, which only subscribes.
typedef _Subscribe = Object? Function(void Function(dynamic) emit, Object? args, void Function(dynamic)? fail);

/// A declaration with no `args` key, apart from one whose `args` is null: the same rule as labapi.js.
const Object _noArgs = Object();

final Map<String, _Subscribe> _events = {
  'configure': (emit, args, fail) {
    // Marshalled first: a config Dart cannot express subscribes nothing (`event-arm … outcome=unexpressible`).
    final config = identical(args, _noArgs) ? null : _fetchConfigFrom(args, strict: true);
    final sub = _Subscriber(emit, fail);
    _subscribers.add(sub);
    if (config != null) _configureWith(config, 'event-arm');
    return _Subscription(() => _subscribers.remove(sub));
  },
};

// --- marshalling: the typed config classes ----------------------------------------------------------------------

const _configKeys = {
  'minimumFetchInterval', 'stopOnTerminate', 'startOnBoot', 'enableHeadless', 'forceAlarmManager',
  'requiredNetworkType', 'requiresBatteryNotLow', 'requiresStorageNotLow', 'requiresCharging', 'requiresDeviceIdle',
};

/// A BackgroundFetchConfig from the scenario's flat map, field by field (background_fetch.dart:215-235).
/// minimumFetchInterval is a required int in Dart where the types have it optional, so a map without one cannot be
/// expressed.  `strict` is a declaration's `args`, the scenario's argument to configure(): a key Dart has no parameter
/// for is the driver's refusal (`unexpressible`), never dropped.  The boot config is not strict: a key Dart has no
/// field for is left out, so the echo (toMap()) lacks it and configDropped names it, the metric bg-forge wrote for a
/// driver that names the keys it maps.  A value of the wrong type is a refusal either way.
BackgroundFetchConfig _fetchConfigFrom(Object? args, {required bool strict}) {
  final given = _as<Map>(args, 'BackgroundFetchConfig');
  final m = strict ? _options(given, 'BackgroundFetchConfig', _configKeys) : given;
  return BackgroundFetchConfig(
      minimumFetchInterval: _as<int>(m['minimumFetchInterval'], 'minimumFetchInterval'),
      stopOnTerminate: _as<bool?>(m['stopOnTerminate'], 'stopOnTerminate'),
      startOnBoot: _as<bool?>(m['startOnBoot'], 'startOnBoot'),
      enableHeadless: _as<bool?>(m['enableHeadless'], 'enableHeadless'),
      forceAlarmManager: _as<bool?>(m['forceAlarmManager'], 'forceAlarmManager'),
      requiredNetworkType: _networkType(m['requiredNetworkType']),
      requiresBatteryNotLow: _as<bool?>(m['requiresBatteryNotLow'], 'requiresBatteryNotLow'),
      requiresStorageNotLow: _as<bool?>(m['requiresStorageNotLow'], 'requiresStorageNotLow'),
      requiresCharging: _as<bool?>(m['requiresCharging'], 'requiresCharging'),
      requiresDeviceIdle: _as<bool?>(m['requiresDeviceIdle'], 'requiresDeviceIdle'));
}

const _taskKeys = {
  'taskId', 'delay', 'periodic', 'requiresNetworkConnectivity', 'stopOnTerminate', 'startOnBoot', 'enableHeadless',
  'forceAlarmManager', 'requiredNetworkType', 'requiresBatteryNotLow', 'requiresStorageNotLow', 'requiresCharging',
  'requiresDeviceIdle',
};

/// A TaskConfig from the hook's map, field by field (background_fetch.dart:263-287): the types' keys, each a named
/// parameter.  taskId and delay are required.  periodic and requiresNetworkConnectivity are non-nullable with a default
/// of false: left out, they take it, and an explicit null cannot be expressed.  `type` (TaskType) is Dart's alone and
/// not the types', so a scenario cannot send it and it is not taken.
TaskConfig _taskConfigFrom(Object? args) {
  final m = _options(_as<Map>(args, 'TaskConfig'), 'TaskConfig', _taskKeys);
  return TaskConfig(
      taskId: _as<String>(m['taskId'], 'taskId'),
      delay: _as<int>(m['delay'], 'delay'),
      periodic: _given<bool>(m, 'periodic', false),
      requiresNetworkConnectivity: _given<bool>(m, 'requiresNetworkConnectivity', false),
      stopOnTerminate: _as<bool?>(m['stopOnTerminate'], 'stopOnTerminate'),
      startOnBoot: _as<bool?>(m['startOnBoot'], 'startOnBoot'),
      enableHeadless: _as<bool?>(m['enableHeadless'], 'enableHeadless'),
      forceAlarmManager: _as<bool?>(m['forceAlarmManager'], 'forceAlarmManager'),
      requiredNetworkType: _networkType(m['requiredNetworkType']),
      requiresBatteryNotLow: _as<bool?>(m['requiresBatteryNotLow'], 'requiresBatteryNotLow'),
      requiresStorageNotLow: _as<bool?>(m['requiresStorageNotLow'], 'requiresStorageNotLow'),
      requiresCharging: _as<bool?>(m['requiresCharging'], 'requiresCharging'),
      requiresDeviceIdle: _as<bool?>(m['requiresDeviceIdle'], 'requiresDeviceIdle'));
}

/// The wire int as Dart's NetworkType: the plugin sends `.index` (background_fetch.dart:170), so index i is
/// NetworkType.values[i].  An index the enum lacks cannot be expressed.
NetworkType? _networkType(Object? v) {
  final i = _as<int?>(v, 'requiredNetworkType');
  if (i == null) return null;
  if (i < 0 || i >= NetworkType.values.length) throw _Unexpressible('no NetworkType at index $i');
  return NetworkType.values[i];
}

/// A non-nullable named parameter with a default: [fallback] when the key is not there, else the value as a [T].
T _given<T>(Map m, String key, T fallback) => m.containsKey(key) ? _as<T>(m[key], key) : fallback;

/// One reported path as the screen shows it: the same traversal `_retUnguarded` logs, returning
/// the type and a value clipped to its column.  Kept beside it deliberately — if the traversal
/// ever changes, both halves have to change together.
Map<String, String> _eventSeen(String path, dynamic value) {
  final segs = path.split('.').skip(1).toList();
  var type = '';
  for (var i = 0; i < segs.length; i++) {
    final seg = segs[i];
    final index = int.tryParse(seg);
    bool has;
    if (value is List) {
      has = seg == 'length' || (index != null && index >= 0 && index < value.length);
    } else if (value is Map) {
      has = value.containsKey(seg);
    } else {
      type = 'missing';
      break;
    }
    if (!has) {
      type = 'missing';
      break;
    }
    value = (value is List) ? (seg == 'length' ? value.length : value[index!]) : (value as Map)[seg];
  }
  if (type.isEmpty) type = _typeOf(value);
  var shown = '';
  if (type != 'missing' && type != 'object' && type != 'array' && value != null) {
    shown = '$value';
    if (shown.length > 10) shown = '${shown.substring(0, 9)}\u2026';
  }
  return {'path': path, 'type': type, 'value': shown};
}

/// (WO-027) The rows the `view: api` screen shows — what each call answered, observation only,
/// mirroring labapi.js `view()`.  Keep the two in step.
final List<Map<String, dynamic>> _apiRows = [];

/// (Tier C C1) A collapsed hook's summary row: `N paths`, the host's ok / ✗ / pending counts once
/// it has judged the paths, and what arrived `missing`.  Mirrors labapi.js `tally()`.
const _collapseAt = 8, _pathW = 24, _lineW = 47, _hostLines = 4;

/// (Tier C C1) Lines of at most [width]: after `, ` where it can, then after a `.`, `=` or `-`, hard
/// only as a last resort; past _hostLines the last line ends in `…`.  Mirrors labapi.js `wrap()`.
List<String> _wrap(String text, int width) {
  final out = <String>[];
  var line = '';
  final items = text.split(', ');
  for (var i = 0; i < items.length; i++) {
    final piece = items[i] + (i < items.length - 1 ? ',' : '');
    if (line.isNotEmpty && line.length + 1 + piece.length > width) {
      out.add(line);
      line = '';
    }
    var rest = (line.isNotEmpty ? '$line ' : '') + piece;
    while (rest.length > width) {
      var at = width;   // after a `.`, `=` or `-` in the line's back half, so a segment stays whole
      for (var k = width; k > width / 2; k--) {
        if ('.=-'.contains(rest[k - 1])) {
          at = k;
          break;
        }
      }
      out.add(rest.substring(0, at));
      rest = rest.substring(at);
    }
    line = rest;
  }
  if (line.isNotEmpty) out.add(line);
  if (out.length <= _hostLines) return out;
  final kept = out.sublist(0, _hostLines);
  final last = kept[_hostLines - 1];   // clamp, as JS slice does: substring throws past the end
  kept[_hostLines - 1] = '${last.length > width - 1 ? last.substring(0, width - 1) : last}\u2026';
  return kept;
}
LabLine _tally(String id, List paths) {
  var ok = 0, bad = 0, wait = 0, missing = 0;
  for (final e in paths) {
    final v = _judged(id, e['path'] as String);
    if (v == 'ok') {
      ok++;        // an `absent: true` path is missing AND ok: ok
    } else if (e['type'] == 'missing') {
      missing++;   // each path counted once: missing is its own ✗
    } else if (_failed(v)) {
      bad++;
    } else if (v == 'pending') {
      wait++;
    }
  }
  return [
    (text: '       ${'${paths.length} paths'.padRight(24)}', tone: 'dim'),
    if (ok > 0) (text: '$ok ok  ', tone: 'pass'),
    if (bad > 0) (text: '$bad \u2717  ', tone: 'fail'),
    if (wait > 0) (text: '$wait pending  ', tone: 'dim'),
    if (missing > 0) (text: '$missing missing', tone: 'mark'),
  ];
}

List<LabLine> _apiView() {
  final rule = '\u2500' * 47;
  final out = <LabLine>[
    _plain('BackgroundGeolocation api calls'),   // labapi.js's title, which fetch's JavaScript hosts draw too
    [(text: _labHeader, tone: 'dim')],
    [(text: rule, tone: 'dim')],
  ];
  var ok = 0, bad = 0;
  for (final r in _apiRows) {
    final outcome = r['outcome'] as String, good = outcome == 'ok';
    good ? ok++ : bad++;
    final id = r['api'] as String, v = _judged(id);
    out.add([
      _glyph(v, true),
      (text: id.padRight(26), tone: 'plain'),
      (text: _failed(v) && v != 'fail' ? v! : good ? '' : outcome, tone: _failed(v) ? 'fail' : 'mark'),
    ]);
    // (WO-027, Tier C C1) Past _collapseAt paths: one summary row, then only the paths worth a
    // look — failed on the host, or observed missing.  labapi.js `tally()`; keep the two in step.
    final paths = r['paths'] as List, many = paths.length > _collapseAt;
    if (v == 'ok') continue;   // (Tier C C1) judged entirely green: its one row
    if (many) out.add(_tally(id, paths));
    String? group;
    for (final seen in paths) {
      final path = seen['path'] as String, value = seen['value'] as String;
      final pv = _judged(id, path);
      if (many && !_failed(pv) && !(seen['type'] == 'missing' && pv != 'ok')) continue;
      // A path the payload did not have is the observation most worth seeing.
      final cell = _typeCell(pv, seen['type'] as String);
      if (path.length <= _pathW) {
        out.add([
          (text: '       ${path.padRight(_pathW)}', tone: 'dim'),
          (text: value.padRight(11), tone: 'plain'),
          cell,
        ]);
        group = null;
        continue;
      }
      // (Tier C C1) Longer than its column: under its group, leaf with value and type when they fit,
      // else on the next line.  labapi.js `lines()`; keep the two in step.
      final cut = path.lastIndexOf('.'), leaf = path.substring(cut + 1);
      final prefix = path.substring(path.startsWith(r'$.') ? 2 : 0, cut + 1);   // `$err.` keeps its name
      if (prefix.isNotEmpty && prefix != group) out.add([(text: '       $prefix', tone: 'dim')]);
      group = prefix;
      final tail = (value.isNotEmpty ? '$value  ' : '') + cell.text;
      if (9 + leaf.length + 2 + tail.length <= _lineW) {
        out.add([
          (text: '         $leaf  ', tone: 'dim'),
          (text: value.isNotEmpty ? '$value  ' : '', tone: 'plain'),
          cell,
        ]);
      } else {
        out.add([(text: '         $leaf', tone: 'dim')]);
        out.add([(text: '           ${value.padRight(12)}', tone: 'plain'), cell]);
      }
    }
  }
  out.add([(text: rule, tone: 'dim')]);
  out.add([
    (text: '  $ok answered', tone: 'plain'),
    (text: bad > 0 ? '  \u00b7  $bad rejected or absent' : '', tone: 'mark'),
  ]);
  return out;
}

/// The rows the events view shows — OBSERVATION only, never a verdict: `min` and every
/// comparator live on the host.  Mirrors labevents.js `view()`; keep the two in step.
final List<Map<String, dynamic>> _eventRows = [];
String _eventsHeader = '';
String _labHeader = '';   // (WO-027) the api and title views' header: no `armed N`
String _labView = 'title';       // (WO-027) api | events | title, chosen by the scenario
List _labEvents = [];
int _labEventsArmed = 0;
Future<void> _batchChain = Future.value();   // (WO-027) one batch at a time; a tick waits on it

List<LabLine> _eventsView() {
  final rule = '\u2500' * 47;
  final out = <LabLine>[
    _plain('BackgroundGeolocation `on*` events'),
    [(text: _eventsHeader, tone: 'dim')],
    [(text: rule, tone: 'dim')],
  ];
  var fired = 0, quiet = 0, deliveries = 0;
  for (final r in _eventRows) {
    final n = r['n'] as int;
    deliveries += n;
    final good = r['outcome'] == 'ok' && n > 0;
    good ? fired++ : quiet++;
    final state = r['outcome'] != 'ok'
        ? '${r['outcome']}'
        : n == 0
            ? 'waiting'
            : '$n ${n == 1 ? 'delivery' : 'deliveries'}';
    final id = r['id'] as String, v = _judged(id);
    out.add([
      _glyph(v, n > 0 || r['outcome'] != 'ok'),
      (text: (r['on'] as String).padRight(26), tone: 'plain'),
      // A row-level failure (never-fired, fired-1/2) is the host's reason; 'fail' means a path.
      (text: _failed(v) && v != 'fail' ? v! : state,
          tone: _failed(v) ? 'fail' : r['outcome'] != 'ok' ? 'mark' : 'dim'),
    ]);
    for (final seen in (r['seen'] as List)) {
      out.add([
        (text: '       ${(seen['path'] as String).padRight(24)}', tone: 'dim'),
        (text: (seen['value'] as String).padRight(11), tone: 'plain'),
        _typeCell(_judged(id, seen['path'] as String), seen['type'] as String),
      ]);
    }
  }
  out.add([(text: rule, tone: 'dim')]);
  out.add([
    (text: '  $fired delivered', tone: 'plain'),
    (text: quiet > 0 ? '  \u00b7  $quiet waiting' : '', tone: 'dim'),
    (text: '  \u00b7  $deliveries ${deliveries == 1 ? 'delivery' : 'deliveries'}', tone: 'dim'),
  ]);
  return out;
}

/// (WO-027) The call schedule, drawn under every view's header: each api hook, `○` while it waits
/// (with its slot when deferred, `+2s`), `●` once it has run, and the host's `✓`/`✗` once judged —
/// so "waiting for its slot" does not look like "stuck".  The port of labapi.js schedule(); keep
/// the two in step.
List<LabLine> _schedule() {
  final done = {for (final r in _apiRows) r['api'] as String};
  final items = <LabSegment>[];
  for (var i = 0; i < _hooks.length; i++) {
    final h = _hooks[i];
    if (h is! Map || h['api'] is! String) continue;
    final ran = done.contains('${h['api']}#$i'), v = _judged('${h['api']}#$i');
    // Once the host has judged a call it takes the verdict's glyph — the host line's tally counts
    // these rows, and on the events view the strip is the only place they appear.
    final g = v == 'ok' ? '\u2713' : _failed(v) ? '\u2717' : ran ? '\u25CF' : '\u25CB';
    items.add((
      text: '$g ${h['api']}${h['at_s'] != null ? ' +${_jsNum(h['at_s'])}s' : ''}',
      tone: v == 'ok' ? 'pass' : _failed(v) ? 'fail' : ran ? 'plain' : 'dim'
    ));
  }
  if (items.isEmpty) return const [];
  final out = <LabLine>[];
  var line = <LabSegment>[(text: '  calls  ', tone: 'dim')];
  var width = 9;
  for (final it in items) {
    if (width + it.text.length + 3 > 56 && line.length > 1) {
      out.add(line);
      line = [(text: '         ', tone: 'dim')];
      width = 9;
    }
    line.add((text: '${it.text}   ', tone: it.tone));
    width += it.text.length + 3;
  }
  out.add(line);
  return out;
}

/// A view with the schedule and the host line inserted under its title and header — near the TOP:
/// an api view of 14 hooks is taller than the screen, and the screenshot must carry the verdict.
List<LabLine> _frame(List<LabLine> view) =>
    [...view.take(2), ..._schedule(), ..._host(), ...view.skip(2)];

/// (WO-027) The HOST's verdict — {rows: {id: v}, paths: {id: {path: v}}, final?, verdict?,
/// metrics?}, v = 'ok' | 'pending' | the failure reason (labkit.verdict_view) — pushed by run.py
/// and read back by _pollVerdict.  The port of labapi.js's setVerdict/judged/glyph/typeCell/host;
/// keep the two in step.  null, before the first push, leaves every row an observation.
Map<String, dynamic>? _verdict;
String _lastVerdict = '';

String? _judged(String id, [String? path]) {
  final v = _verdict;
  if (v == null) return null;
  if (path == null) return (v['rows'] as Map?)?[id] as String?;
  return ((v['paths'] as Map?)?[id] as Map?)?[path] as String?;
}

bool _failed(String? v) => v != null && v != 'ok' && v != 'pending';

LabSegment _glyph(String? v, bool observed) {
  if (v == 'ok') return (text: '  \u2713  ', tone: 'pass');
  if (_failed(v)) return (text: '  \u2717  ', tone: 'fail');
  return observed ? (text: '  \u25CF  ', tone: 'plain') : (text: '  \u25CB  ', tone: 'dim');
}

LabSegment _typeCell(String? v, String type) {
  if (v == 'ok') return (text: type, tone: 'pass');
  if (_failed(v)) return (text: v!.length > 26 ? '${v.substring(0, 25)}\u2026' : v, tone: 'fail');
  return (text: type, tone: type == 'missing' ? 'mark' : 'dim');
}

List<LabLine> _host() {
  const label = (text: '  host   ', tone: 'dim');
  final v = _verdict;
  if (v == null) return [[label, (text: 'scoring\u2026', tone: 'dim')]];
  if (v['final'] == true) {
    final verdict = v['verdict'] as String?, inverted = v['inverted'] == true;
    final broken = [for (final m in (v['metrics'] as List? ?? const [])) if (m[3] != true) m];
    // A must-fail (mutation) variant PASSES by breaking something: say so, and draw what it broke
    // as the expected outcome it is, not as a failure.
    final note = !inverted
        ? ''
        : verdict == 'PASS'
            ? '   must fail: it broke ${broken.length}'
            : '   must fail: nothing broke';
    final out = <LabLine>[
      [label, (text: '${verdict ?? 'no expectations'}$note',
               tone: verdict == 'PASS' ? 'pass' : verdict != null ? 'fail' : 'dim')]
    ];
    if (v['contaminated'] != null) {
      out.add([(text: '     contaminated by ${(v['contaminated'] as List).join(', ')}', tone: 'fail')]);
    }
    for (final m in broken) {
      // (Tier C C1) Fits: on the metric's line.  Longer: wrapped under it.  labapi.js; keep in step.
      final actual = '${m[1]}', tone = inverted ? 'mark' : 'fail';
      final head = '     \u2717 ${'${m[0]}'.padRight(16)} ';   // a long name still gets its gap
      if (head.length + actual.length <= _lineW) {
        out.add([(text: head, tone: tone), (text: actual, tone: tone)]);
        continue;
      }
      out.add([(text: '     \u2717 ${m[0]}', tone: tone)]);
      for (final part in _wrap(actual, _lineW - 7)) {
        out.add([(text: '       $part', tone: tone)]);
      }
    }
    return out;
  }
  var ok = 0, bad = 0, waiting = 0;
  for (final r in ((v['rows'] as Map?) ?? const {}).values) {
    r == 'ok' ? ok++ : r == 'pending' ? waiting++ : bad++;
  }
  return [
    [label, (text: '\u2713 $ok', tone: ok > 0 ? 'pass' : 'dim'),
     (text: '   \u2717 $bad', tone: bad > 0 ? 'fail' : 'dim'),
     (text: '   \u00b7  $waiting pending', tone: 'dim')]
  ];
}

/// Draw the current view again — the verdict changed under it.
void _redraw() {
  if (_labView == 'api') {
    _viewLines.value = _frame(_apiView());
  } else if (_labView == 'events') {
    _viewLines.value = _frame(_eventsView());
  } else {
    // (Tier C, C7.6) The title view: the scenario and the platform/boot line, and under them the
    // schedule and the host line every view carries.
    _viewLines.value = _frame([_plain(_scenario), [(text: _labHeader, tone: 'dim')]]);
  }
}

/// The host's verdict, as run.py last pushed it.  Only a change redraws, and nothing is logged:
/// the verdict is the host's, and a `LAB app=` line about it would be evidence it is not.  A
/// `final` one selects the Test tab first (Tier C, C7.6): the host's screenshot follows it.
Future<void> _pollVerdict() async {
  try {
    final text = await _probe.invokeMethod<String>('verdict') ?? '';
    if (text == _lastVerdict) return;
    _lastVerdict = text;
    Map<String, dynamic>? v;
    try {
      final parsed = text.isEmpty ? null : jsonDecode(text);
      v = parsed is Map<String, dynamic> ? parsed : null;
    } catch (_) {
      v = null;
    }
    _verdict = v;
    if (v?['final'] == true) _tab.value = 'Test';
    _redraw();
  } catch (_) {
    // display only
  }
}

/// Arms the declarations due in one slot, logging the same lines labevents.js logs.  `slot` is
/// 'boot' — the declarations with no `at_s`, armed before the configure() and the first batch that
/// cause them — or a number: those whose `at_s` it is (WO-027).  A slot is always a declared
/// `at_s` (_slotTimes), never a computed one, so `==` matches it; Dart's 2 == 2.0 holds.
/// `armedSoFar` accumulates so `events-armed n=` names the total.  The port of labevents.js arm().
int _armEvents(List declarations, Object slot, int armedSoFar) {
  var n = armedSoFar, armed = 0;
  _labHeader = 'boot=$_boot';          // (Tier C) platform and OS are in the header now
  _eventsHeader = '$_labHeader  armed ${declarations.length}';
  for (final decl in declarations) {
    final id = decl['id'] as String, on = decl['on'] as String;
    final atS = decl['at_s'];
    if (slot == 'boot' ? atS != null : atS != slot) continue;
    final paths = (decl['paths'] as List?) ?? [];
    n++;
    armed++;
    final subscribe = _events[on];
    final row = <String, dynamic>{'on': on, 'id': id, 'outcome': 'ok', 'n': 0, 'seen': []};
    _eventRows.add(row);
    if (subscribe == null) {
      row['outcome'] = 'absent';
      _log('event-arm id=$id outcome=absent');
      continue;
    }
    var seen = 0, errors = 0;
    // (Tier C, R4) `delivery: N`: the paths are reported for deliveries 1..N, each line naming its own (`n=`),
    // and the host reads the Nth.  Without it: the first delivery only, and no `n=`, as before.
    final delivery = decl['delivery'];
    final upTo = delivery is int ? delivery : 1;
    try {
      // (emit, args, fail): only a member that takes values before its callback reads `args`, and no `args` key
      // means the callbacks are its only arguments (_noArgs), as labevents.js has it.
      final subscription = subscribe((dynamic payload) {
        seen++;
        _log('event id=$id n=$seen');
        row['n'] = seen;
        if (seen <= upTo) {
          for (final path in paths) {
            if (_errorPath(path as String)) continue;   // the failure callback's (below)
            _ret(id, path, payload, delivery is int ? seen : null);
            if (seen != upTo) continue;                 // the screen shows the delivery the host reads
            (row['seen'] as List).add(_eventSeen(path, payload));
          }
        }
        // Only the events view redraws as events arrive (Tier C, C7.6): under a view the step
        // named instead, a delivery would draw the events rows over it.
        if (_labView == 'events') _viewLines.value = _frame(_eventsView());
      }, decl.containsKey('args') ? decl['args'] : _noArgs, decl['errors'] != true ? null : (dynamic error) {
        // (Tier C, R6) Each error is counted on its own line, and the first reports the `$err` paths.
        errors++;
        _log('event-error id=$id n=$errors');
        if (errors != 1) return;
        for (final path in paths) {
          if (_errorPath(path as String)) _ret(id, path, error);
        }
      });
      // (Tier C, R1) Kept, not discarded: `remove_at_s` removes it.  A Future that fails is _removeEvents' to
      // report (`threw`); the handler here only keeps it from being an unhandled error until then.
      if (subscription is Future) subscription.then((_) {}, onError: (Object _) {});
      _held[id] = (subscription: subscription, seen: () => seen);
      _log('event-arm id=$id outcome=ok');
    } on _Unexpressible catch (e) {
      // (Tier C, C4.2) The driver's own refusal to marshal the declaration's args: nothing was subscribed.
      row['outcome'] = 'unexpressible';
      _log('event-arm id=$id outcome=unexpressible why=${Uri.encodeComponent(e.why)}');
    } catch (e) {
      row['outcome'] = 'threw';
      _log('event-arm id=$id outcome=threw');
    }
  }
  if (armed > 0) {
    _log('events-armed n=$n');
    if (_labView == 'events') _viewLines.value = _frame(_eventsView());
  }
  return n;
}

/// (Tier C, R1) What each armed declaration holds, by id: what its member handed back and its delivery count.
final Map<String, ({Object? subscription, int Function() seen})> _held = {};

bool _errorPath(String p) => p == r'$err' || p.startsWith(r'$err.');

/// (Tier C, R1) The removals due in one slot: every declaration whose `remove_at_s` is this slot has the
/// Subscription it armed removed, and says how that went with the deliveries it had seen.  A Future of one is
/// awaited first.  `absent`: nothing to call — the declaration never armed, or handed back no Subscription.
/// The port of labevents.js remove(); keep the two in step.  The Subscription is the facade's (_Subscription).
Future<void> _removeEvents(List declarations, Object slot) async {
  if (slot is! num) return;
  for (final decl in declarations) {
    if (decl['remove_at_s'] != slot) continue;
    final id = decl['id'] as String;
    final mine = _held[id];
    var outcome = 'absent';
    try {
      final held = mine?.subscription;
      final subscription = held is Future ? await held : held;
      if (subscription is _Subscription) {
        await subscription.remove();
        outcome = 'ok';
      }
    } catch (_) {
      outcome = 'threw';
    }
    _log('event-removed id=$id n=${mine == null ? 0 : mine.seen()} outcome=$outcome');
  }
}

// --- `api` hooks: the conformance batch (WO-027) --------------------------------------------
// A port of labs/platforms/_shared/labapi.js — keep the two in step.  The driver is told what
// to call and which paths to report, never what is expected: the host scores it.

/// Dart cannot look a static member up by name, so the published API is spelt out once, here: background_fetch's
/// members over the vocabulary (labs/harness/vocabulary.yaml).  A name with no entry is reported `outcome=absent`, the
/// same word the JavaScript drivers report for a member that is not there.  An ALIAS is an entry whose Dart spelling
/// or form differs from the shared API's; the `api` line carries `alias=` so a capture shows which was taken.
final Map<String, Function> _api = {
  // Probed, never called: configure takes two callbacks after its config and registerHeadlessTask one, which a hook
  // cannot pass (an `events:` declaration subscribes to configure: the facade in _events).  Each row refuses any call
  // itself, as bg-forge's registerHeadlessTask row does; a probe reads the row, a function.
  'configure': (args) =>
      throw _Unexpressible('configure takes two callbacks after its config, which a hook cannot pass'),
  'registerHeadlessTask': (args) =>
      throw _Unexpressible('registerHeadlessTask takes a callback, which a hook cannot pass'),
  'start': () => BackgroundFetch.start(),
  // Future<int> where the types declare Promise<boolean>: both natives answer stop with the status
  // (BackgroundFetchModule.java:166-170, BackgroundFetchPlugin.m:131-140).  Reported as delivered, so the divergence is
  // what shows (api-smoke reads it red on purpose).  No argument stops every task; a null one is the same call.
  'stop': ([args]) => BackgroundFetch.stop(_as<String?>(args, 'taskId')),
  // A GETTER in Dart (background_fetch.dart:534), not a method: read, not called, and it takes no callback.
  'status': () => BackgroundFetch.status,
  'scheduleTask': (args) => BackgroundFetch.scheduleTask(_taskConfigFrom(args)),
  // Future<void> in Dart, but the Future completes with the channel's `true` (`return await invokeMethod`), and this
  // driver awaits through `dynamic`.  A typed Dart caller is handed nothing, so the row reports what the DECLARED type
  // hands one (_declared): null today, the nearest Dart has to the types' `void`.  Were finish() to declare a value,
  // that value would be reported, and api-smoke would read it red, as it reads stop()'s int.
  'finish': (args) => _declared(BackgroundFetch.finish(_as<String>(args, 'taskId'))),
};

/// The constants of the default export, which a probe reports by VALUE (labapi.js reports the member itself, and a
/// constant's is a number; fetch_pack lets a constant's probe assert `type` and `equals`).  STATUS_* are
/// BackgroundFetch's own `static const int`s.  Dart has no NETWORK_TYPE_*: its surface is `enum NetworkType`, sent on
/// the wire as `.index` (background_fetch.dart:170), so each is answered from the enum member's index — an alias row,
/// so a reorder of the Dart enum reads red rather than `absent`.  Probes only: a call of one is `absent`, as a
/// JavaScript driver finds a number that is not a function.
final Map<String, int> _constants = {
  'STATUS_RESTRICTED': BackgroundFetch.STATUS_RESTRICTED,
  'STATUS_DENIED': BackgroundFetch.STATUS_DENIED,
  'STATUS_AVAILABLE': BackgroundFetch.STATUS_AVAILABLE,
  'NETWORK_TYPE_NONE': NetworkType.NONE.index,
  'NETWORK_TYPE_ANY': NetworkType.ANY.index,
  'NETWORK_TYPE_UNMETERED': NetworkType.UNMETERED.index,
  'NETWORK_TYPE_NOT_ROAMING': NetworkType.NOT_ROAMING.index,
  'NETWORK_TYPE_CELLULAR': NetworkType.CELLULAR.index,
};
const _aliases = {'status': 'status',   // the getter: the same name, read rather than called
                  'NETWORK_TYPE_NONE': 'NetworkType.NONE.index', 'NETWORK_TYPE_ANY': 'NetworkType.ANY.index',
                  'NETWORK_TYPE_UNMETERED': 'NetworkType.UNMETERED.index',
                  'NETWORK_TYPE_NOT_ROAMING': 'NetworkType.NOT_ROAMING.index',
                  'NETWORK_TYPE_CELLULAR': 'NetworkType.CELLULAR.index'};

/// (Tier C, C4.2) The driver's own refusal: the scenario's argument has no form this Dart surface takes — a
/// value of another type, a key a named-parameter list lacks, a value no enum has, an arity the member does not
/// take.  Thrown only while MARSHALLING, before the SDK is called, and logged `outcome=unexpressible`, never
/// `rejected`, which is the SDK's own answer (the C4 plan §2 E, harness item 7).  labkit fails every outcome on it.
class _Unexpressible implements Exception {
  final String why;
  _Unexpressible(this.why);
  @override
  String toString() => why;
}

/// (Tier C, C4.2) [v] as a [T], or the driver's refusal: every cast of a scenario's argument goes through here, so
/// a wrong type reads `unexpressible` instead of a TypeError the batch would have scored `rejected`.
T _as<T>(dynamic v, String what) => v is T ? v : throw _Unexpressible('$what is ${_typeOf(v)}, not $T');

/// (Tier C, C4.2) An options object for a Dart member that takes NAMED PARAMETERS: none (no argument, which is
/// how Dart spells a null one too), or a map whose every key is one of [named].  A key Dart has no parameter for
/// cannot be passed, and dropping it in silence is the argument-forwarding defect the lab exists to catch.
Map _options(dynamic args, String what, Set<String> named) {
  if (args == null) return const {};
  final m = _as<Map>(args, what);
  final unknown = m.keys.where((k) => !named.contains(k)).toList();
  if (unknown.isNotEmpty) throw _Unexpressible('$what: Dart has no parameter ${unknown.join(',')}');
  return m;
}

/// What a member's Future hands a typed Dart caller: for a `Future<void>` nothing (null), whatever it completes with at
/// runtime; for any other `Future<T>` its value.  [T] is inferred at the call site from the member's own signature, so
/// it follows the plugin: a `Future<void>` member that came to declare a value reports that value instead of being
/// masked here (the review of 2026-11-13: a fixed `Future<void>` parameter accepted any Future and always read null).
Future<Object?> _declared<T>(Future<T> f) async {
  final v = await f;
  return T == _type<void>() ? null : v;
}

Type _type<X>() => X;

/// The harness's vocabulary, not Dart's: int and double are both `number`, and a Dart `void`
/// arrives as `null` — there is no `undefined` to report.
String _typeOf(dynamic v) {
  if (v == null) return 'null';
  if (v is bool) return 'boolean';
  if (v is num) return 'number';
  if (v is String) return 'string';
  if (v is List) return 'array';
  if (v is Function) return 'function';
  return 'object';
}

/// `parent` separates "the key is not there" from "there was nowhere to look", and always closes
/// the line: the host reads a ret line without it as cut in transport.  Channel maps are
/// `Map<Object?, Object?>`: index them, never cast them.
/// (Tier C, R4) `n`: the delivery this line reports, on an `events:` declaration that asked for one past the
/// first (`delivery:`).  A return value has none, and neither has a declaration that did not ask.
void _ret(String id, String path, dynamic value, [int? n]) {
  final which = n == null ? '' : ' n=$n';
  try {
    _retUnguarded('$id$which', path, value);
  } catch (e) {
    // This path goes `unreported` on the host; it must not take the rest of the batch with it.
    _log('ret-error id=$id$which path=$path');
  }
}

void _retUnguarded(String id, String path, dynamic value) {
  final segs = path.split('.').skip(1).toList();
  var type = '', parent = 1;
  for (var i = 0; i < segs.length; i++) {
    final seg = segs[i], last = i == segs.length - 1;
    final index = int.tryParse(seg);
    bool has;
    if (value is List) {
      has = seg == 'length' || (index != null && index >= 0 && index < value.length);
    } else if (value is Map) {
      has = value.containsKey(seg);
    } else {
      type = 'missing';
      parent = 0;
      break;
    }
    if (!has) {
      type = 'missing';
      parent = last ? 1 : 0;
      break;
    }
    value = (value is List) ? (seg == 'length' ? value.length : value[index!]) : (value as Map)[seg];
  }
  if (type.isEmpty) type = _typeOf(value);
  var line = 'ret id=$id path=$path type=$type';
  if (type == 'string') {
    final text = value as String;
    final clipped = text.length > 200;
    var end = 200;
    if (clipped && (text.codeUnitAt(end - 1) & 0xFC00) == 0xD800) end--; // never halve a surrogate pair
    line += ' value=${Uri.encodeComponent(clipped ? text.substring(0, end) : text)}${clipped ? ' trunc=1' : ''}';
  } else if (type == 'number' || type == 'boolean') {
    line += ' value=$value';
  }
  _log('$line parent=$parent');
}

/// Awaits the hooks due in one slot, in list order.  The id is `<api>#<index>` over the FULL hook
/// list, legacy hooks included — the host derives the same ids from run.json — so the filter
/// SKIPS a hook that is not due and never re-indexes.
///
/// `slot` is 'first' — the hooks with no `at_s`, run at the end of _driver() — or a number: the
/// hooks whose `at_s` it is, run that many seconds after the first batch COMPLETES (WO-027).  A
/// batch is awaited end to end with no pause in it, so "call, wait, call again" needs a slot:
/// measured on iOS, a stop() issued 13 ms after start() cancelled the location request
/// start()'s motionchange was waiting on.  A slot is always a declared `at_s` (_slotTimes), never
/// a computed one, so `==` matches it; Dart's 2 == 2.0 holds.  `ranSoFar` accumulates so
/// `api-done n=` names every hook run so far and a deferred hook cannot look like a dropped one.
/// The port of labapi.js runBatch; keep the two in step.
Future<int> _runApiBatch(Object slot, int ranSoFar) async {
  var n = ranSoFar, ran = 0;
  for (var i = 0; i < _hooks.length; i++) {
    final hook = _hooks[i];
    if (hook is! Map || hook['api'] is! String) continue;
    final atS = hook['at_s'];
    if (slot == 'first' ? atS != null : atS != slot) continue;
    n++;
    ran++;
    final name = hook['api'] as String;
    final id = '$name#$i';
    final paths = ((hook['paths'] as List?) ?? const []).cast<String>();
    final alias = _aliases.containsKey(name) ? ' alias=${_aliases[name]}' : '';
    // A probe of a constant reads its VALUE from _constants (bg-forge's dartMembers fallback, by hand), so `$` is
    // the number a JavaScript driver's probe reports, never a closure.
    final member = _api[name] ?? (hook['probe'] == true ? _constants[name] : null);
    final row = <String, dynamic>{'api': id, 'outcome': 'ok', 'paths': []};
    _apiRows.add(row);
    void record(dynamic value) {
      for (final path in paths) {
        (row['paths'] as List).add(_eventSeen(path, value));
      }
    }
    if (member == null) {
      row['outcome'] = 'absent';
      _log('api id=$id outcome=absent');
      continue;
    }
    if (hook['probe'] == true) {
      _log('api id=$id outcome=ok$alias');
      for (final p in paths) {
        _ret(id, p, member);
      }
      record(member);
      continue;
    }
    var outcome = 'ok';
    dynamic value;
    String? why;
    Future<void>? beside;
    try {
      // The same rule as labapi.js: no `args` key means ZERO arguments, never a null one.  (Tier C, C4.2) An
      // arity the entry does not take is Dart's own refusal — the call cannot be written — as is a marshal step
      // that fails (_Unexpressible): neither reached the SDK, so neither is `rejected`.  The arity is read from the
      // entry's TYPE before the call (a one-argument call needs `Function(Never)`, a zero-argument one
      // `Function()`), so an error thrown inside a call that was made stays `rejected`.
      // (WO-107) A `generate:` hook builds bg-geo geofences, which no background_fetch member takes: refused, not
      // dropped.  An `alongside:` call is issued right after this one is, before it settles.
      if (hook['generate'] != null) throw _Unexpressible('generate: builds geofences, which $name does not take');
      final withArg = hook.containsKey('args');
      if (withArg ? member is! Function(Never) : member is! Function()) {
        throw _Unexpressible('$name ${withArg ? 'takes no argument' : 'requires its argument'} in Dart');
      }
      final arg = hook['args'];
      final t0 = DateTime.now().millisecondsSinceEpoch;
      dynamic pending;
      try {
        // (Tier C, R5) The call alone, apart from its settling: a member that throws HERE handed back no
        // Future, so a caller's `.catchError()` would never see it.  That is `threw`; `rejected` is a Future
        // that completed with an error.  The driver's own refusal is neither, and passes through.  (R's review)
        // Every background_fetch member a row calls hands back a Future without throwing first (stop, status,
        // scheduleTask and finish are `async`; start chains invokeMethod's), so a `threw` on Flutter is a row's own
        // marshalling gone wrong, and each such site must refuse (_Unexpressible) instead.
        pending = Function.apply(member as Function, withArg ? [arg] : const []);   // a call's is an _api row
      } on _Unexpressible {
        rethrow;
      } catch (e) {
        outcome = 'threw';
        value = _thrown(e);
      }
      if (outcome == 'ok') {
        final besideName = hook['alongside'];
        if (besideName is String) beside = _callBeside(besideName, id, t0);
        value = await pending;
      }
    } on _Unexpressible catch (e) {
      outcome = 'unexpressible';
      why = e.why;
    } catch (e) {
      outcome = 'rejected';
      value = _thrown(e);
    }
    if (beside != null) await beside;
    // (Tier C, C4.2) `why=` only on the driver's refusal, %-encoded: `outcome=` is still the field labkit reads.
    _log('api id=$id outcome=$outcome$alias${why == null ? '' : ' why=${Uri.encodeComponent(why)}'}');
    row['outcome'] = outcome;
    if (outcome != 'unexpressible') {   // (Tier C, C4.2) nothing was called, so nothing is reported
      for (final p in paths) {
        // The error side is the thrown value as much as the rejection's.
        if (_errorPath(p) == (outcome != 'ok')) _ret(id, p, value);
      }
      record(value);
    }
    if (_labView == 'api') _viewLines.value = _frame(_apiView());
  }
  if (ran > 0) _log('api-done n=$n');
  return n;
}

/// (WO-027, permissions) What a call threw, or its Future failed with, as the `$err` side reports it.  A thrown
/// scalar is kept as thrown, as labapi.js keeps `value = e`: background_fetch's start() rejects with a bare String
/// (the PlatformException's details, background_fetch.dart:482-487), which `$err` reads as the string it is.
dynamic _thrown(Object e) => (e is PlatformException)
    ? {'code': e.code, 'message': e.message}
    : (e is num || e is String || e is bool) ? e : e.toString();

/// (WO-107) The port of labapi.js callBeside; keep the two in step.  A zero-argument call issued beside the hook's,
/// logged when it settles with the ms since the hook's call was issued.
Future<void>? _callBeside(String name, String id, int t0) {
  void settle(String outcome) =>
      _log('alongside id=$id api=$name outcome=$outcome ms=${DateTime.now().millisecondsSinceEpoch - t0}');
  final other = _api[name];
  if (other is! Function()) {
    settle('absent');
    return null;
  }
  return Future.sync(() => other()).then((_) => settle('ok'), onError: (_) => settle('rejected'));
}

/// (WO-027) The distinct `at_s` values over the api hooks and the `events:` declarations, sorted:
/// one timer each.  The values are the scenario's own, so a slot matches its hooks with `==`.
/// The port of labapi.js slotTimes().
List<num> _slotTimes(List hooks, List events) {
  final out = <num>[];
  void add(dynamic v) {
    if (v is num && v.isFinite && !out.contains(v)) out.add(v);
  }
  for (final h in hooks) {
    if (h is Map && h['api'] is String) add(h['at_s']);
  }
  for (final d in events) {
    if (d is! Map || d['on'] is! String) continue;
    add(d['at_s']);
    add(d['remove_at_s']);   // (Tier C, R2) a removal is a slot too: _removeEvents runs in it
  }
  return out..sort();
}

/// (WO-027) The per-tick fix is derived, not configured: on only where there is no api hook and
/// no `events:` declaration.  The host computes the same rule (labkit.tick_gcp) and checks it
/// against the anchor line.  The port of labapi.js tickGcpFor().
bool _tickGcpFor(List hooks, List events) =>
    !hooks.any((h) => h is Map && h['api'] is String) && events.isEmpty;

/// A number as JavaScript prints it, `2` and never Dart's `2.0`, so the anchor, slot and strip
/// text read the same on every driver.
String _jsNum(Object? v) =>
    (v is double && v.isFinite && v == v.truncateToDouble()) ? '${v.toInt()}' : '$v';

/// (WO-027) `anchor batchMs=<ms> slots=<s,s,…|-> tickGcp=<on|off>`: how long the first batch
/// took, which slots follow it, and whether the per-tick fix is on.  No `bootGcp=` since the boot
/// fix went (Tier C, C7.6).  The port of labapi.js anchorLine().
String _anchorLine(int batchMs, List<num> times, bool tickGcp) =>
    'anchor batchMs=$batchMs slots=${times.isEmpty ? '-' : times.map(_jsNum).join(',')} '
    'tickGcp=${tickGcp ? 'on' : 'off'}';

/// (WO-027) One timer per slot, `at_s` seconds from now — the first batch's completion — each
/// handed the time it was due, so its slot line can say how late it fired.  The port of
/// labapi.js startSlots().
void _startSlots(List<num> times, void Function(num s, DateTime due) onSlot) {
  final t0 = DateTime.now();
  for (final s in times) {
    final after = Duration(milliseconds: (s * 1000).round());
    Timer(after, () => onSlot(s, t0.add(after)));
  }
}

/// (WO-027) A slot: its batch, then its arming — the order a tick used — then (Tier C, R1) the removals its
/// declarations ask for, queued on the chain so it never overlaps another batch or a tick's reads.
void _onSlot(num s, DateTime due) {
  _batchChain = _batchChain.then((_) async {
    final lateMs = (DateTime.now().difference(due).inMicroseconds / 1000).round();
    _log('slot at_s=${_jsNum(s)} late_ms=${lateMs < 0 ? 0 : lateMs}');
    _apiRan = await _runApiBatch(s, _apiRan);
    // (WO-027) The call schedule is on every view: redraw whichever is showing, as after the first batch.
    _redraw();
    // Arming comes after the batch, the order labapi.js and labevents.js keep on every driver.
    if (_labEvents.isNotEmpty) {
      _labEventsArmed = _armEvents(_labEvents, s, _labEventsArmed);
      await _removeEvents(_labEvents, s);
    }
  });
}

/// Legacy tick-counted `call` hooks: fetch has none (lab.yaml supports.calls: []), so any is reported unsupported.
/// The tick is passed in (WO-027 hazard 6): `_ticks`, read after the tick's awaits, could already be the next tick's.
Future<void> _runHooks(int tick) async {
  for (final hook in _hooks.where((h) => h is Map && h.containsKey('call') && h['at'] == tick)) {
    _log('call=${hook['call']} outcome=unsupported');
  }
}

/// Every 4 s: the SDK's status and the native probe, the Capacitor driver's tick line.  (WO-027) The tick runs no
/// batch: `api` hooks and `events:` arming run in the first batch and its `at_s` slots, on their own clock.  It waits
/// for whichever is running when it starts.
Future<void> _tick() async {
  final n = ++_ticks;
  await _batchChain;
  Object status = '-';
  try {
    status = await BackgroundFetch.status;
  } catch (_) {
    status = 'rejected';
  }
  var probe = '';
  try {
    probe = await _probe.invokeMethod<String>('probe') ?? '';
  } catch (e) {
    probe = 'probeError=${_field(e)}';
  }
  final line = 'tick n=$n boot=$_boot status=$status fetches=$_fetches timeouts=$_timeouts finished=$_finished $probe';
  _log(line);
  _status.value = line;
  await _runHooks(n);
}

class LabApp extends StatelessWidget {
  const LabApp({super.key});

  @override
  Widget build(BuildContext context) {
    // Monospace, so a view lines up in a screenshot the way it does in a terminal.  (WO-027)
    // 'monospace' is an ANDROID family alias; iOS has no family by that name and Flutter falls
    // back to the proportional default, which is what left the table ragged on the Simulator while
    // the three JavaScript drivers stayed aligned.  The OS is the theme's (defaultTargetPlatform): dart:io's
    // Platform is not imported here.
    final ios = Theme.of(context).platform.name == 'iOS';
    final mono = TextStyle(
        fontFamily: ios ? 'Menlo' : 'monospace', fontSize: 10, height: 1.35,
        color: _labTone['plain']);
    return MaterialApp(
      home: Scaffold(
        body: SafeArea(
          top: false,
          child: Padding(
            padding: const EdgeInsets.fromLTRB(0, 0, 0, 12),
            child: ValueListenableBuilder<String>(
              valueListenable: _tab,
              builder: (context, tab, _) => Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  ValueListenableBuilder<String>(
                    valueListenable: _headline,
                    // (Tier C) Layout C: the logo, the title, then platform, OS and native snapshot.
                    builder: (context, value, _) => Container(
                      width: double.infinity,
                      color: const Color(0xFFFEDD1E),
                      padding: const EdgeInsets.fromLTRB(10, 52, 10, 8),
                      child: Row(children: [
                        ClipRRect(
                            borderRadius: BorderRadius.circular(6),
                            // bg-forge's asset; a host without it draws the space, not an error.
                            child: Image.asset('assets/transistor.png', width: 30, height: 30,
                                errorBuilder: (context, error, stack) => const SizedBox(width: 30, height: 30))),
                        const SizedBox(width: 9),
                        Expanded(
                          child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
                            Text(value,
                                style: const TextStyle(
                                    color: Colors.black, fontWeight: FontWeight.w600, fontSize: 13)),
                            Text(
                                ['flutter', ios ? 'ios' : 'android', _nativeLabel]
                                    .where((s) => s.isNotEmpty)
                                    .join(' \u00b7 '),
                                style: const TextStyle(color: Color(0xFF5A4A00), fontSize: 10)),
                          ]),
                        ),
                      ]),
                    ),
                  ),
                  // (Tier C, C7.6) Two tabs under the header bar.  Test, selected at launch and
                  // by a final verdict, is the step's view over the tick line; Log is the
                  // driver's own lines.
                  Container(
                    width: double.infinity,
                    decoration: const BoxDecoration(
                        border: Border(bottom: BorderSide(color: Color(0xFFE5E5EA)))),
                    child: Row(children: [
                      for (final name in const ['Test', 'Log'])
                        GestureDetector(
                          behavior: HitTestBehavior.opaque,
                          onTap: () => _tab.value = name,
                          child: Container(
                            padding: const EdgeInsets.symmetric(vertical: 8, horizontal: 14),
                            decoration: BoxDecoration(
                                border: Border(
                                    bottom: BorderSide(
                                        color: name == tab ? _labTone['plain']! : Colors.transparent,
                                        width: 2))),
                            child: Text(name,
                                style: TextStyle(
                                    fontSize: 12,
                                    fontWeight: name == tab ? FontWeight.w600 : FontWeight.normal,
                                    color: name == tab ? _labTone['plain'] : _labTone['dim'])),
                          ),
                        ),
                    ]),
                  ),
                  Expanded(
                    child: tab == 'Test'
                        ? SingleChildScrollView(
                            child: Column(
                              crossAxisAlignment: CrossAxisAlignment.start,
                              children: [
                                const SizedBox(height: 10),
                                ValueListenableBuilder<List<LabLine>>(
                                  valueListenable: _viewLines,
                                  builder: (context, value, _) => Padding(
                                    padding: const EdgeInsets.symmetric(horizontal: 10),
                                    child: Text.rich(
                                        TextSpan(children: [
                                          for (final line in value) ...[
                                            for (final seg in line)
                                              TextSpan(
                                                  text: seg.text,
                                                  style: TextStyle(
                                                      color: _labTone[seg.tone] ?? _labTone['plain'])),
                                            const TextSpan(text: '\n'),
                                          ],
                                        ]),
                                        style: mono),
                                  ),
                                ),
                                const SizedBox(height: 14),
                                ValueListenableBuilder<String>(
                                  valueListenable: _status,
                                  builder: (context, value, _) => Padding(
                                    padding: const EdgeInsets.symmetric(horizontal: 10),
                                    child: Text(value,
                                        style: const TextStyle(fontSize: 9, color: Color(0xFF777777))),
                                  ),
                                ),
                              ],
                            ),
                          )
                        // (Tier C, C7.6) One Text per line, never one span tree of them all (C1's
                        // React Native span flood).  `reverse` puts the newest last, at the bottom,
                        // and keeps it in view as lines arrive; only the visible lines are built.
                        // shrinkWrap under a top Align starts a short log at the top, as the other
                        // three drivers do, rather than pinned to the bottom.
                        : ValueListenableBuilder<int>(
                            valueListenable: _logCount,
                            builder: (context, _, _) => Align(
                              alignment: Alignment.topCenter,
                              child: ListView.builder(
                                reverse: true,
                                shrinkWrap: true,
                                padding: const EdgeInsets.fromLTRB(10, 8, 10, 0),
                                itemCount: _logLines.length,
                                itemBuilder: (context, i) =>
                                    Text(_logLines[_logLines.length - 1 - i], style: mono),
                              ),
                            ),
                          ),
                  ),
                ],
              ),
            ),
          ),
        ),
      ),
    );
  }
}
