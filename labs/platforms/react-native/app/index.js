/**
 * React Native fetch-lab driver (WO-002) — bg-forge's lab protocol (bg-forge labs/README.md "The lab protocol"),
 * modelled on bg-forge's labs/platforms/react-native/app/index.js at 49d38d47f, driving react-native-background-fetch.
 *
 * What differs from the background-geolocation driver:
 *  - configure(config, onEvent, onTimeout) is the boot call (ready() + start() in one), and fetch events arrive
 *    through its two callbacks, not through an on* member.  The driver's own callbacks log every delivery, hand it
 *    to the `events:` declarations of `configure` (EventBG below), then finish(taskId) per the launch's knobs.
 *  - The headless task is AppRegistry's 'BackgroundFetch' (registerHeadlessTask), registered at module load.
 *
 * React Native keeps its JavaScript context across an Activity recreation, but the React root re-mounts: main()
 * runs again.  Everything that must happen once per JS context (configure, arming, timers) is guarded.
 */
import {AppRegistry, Image, NativeModules, Platform, Pressable, ScrollView, Text, View} from 'react-native';
import React from 'react';
import BackgroundFetch from 'react-native-background-fetch';
import {name as appName} from './app.json';
// The api-hook reporter and the events arm every JavaScript driver shares (labs/platforms/_shared, vendored
// byte-identical from bg-forge; metro.config.js watches the folder).
import LabApi from '../../_shared/labapi.js';
import LabEvents from '../../_shared/labevents.js';
import LabLogo from '../../_shared/logo.js';

const TAG = 'LAB';
const LOG_KEEP = 200;
const logLines = [];
const logListeners = new Set();
let logSeq = 0;
const log = (s) => {
  console.log(TAG + ' app=' + s);
  logLines.push({n: ++logSeq, text: s});
  if (logLines.length > LOG_KEEP) logLines.shift();
  logListeners.forEach((fn) => fn());
};
const Lab = NativeModules.Lab;

// A value for a `key=value` field: the host reads `key=(\S+)`, so whitespace would cut it.
const field = (v) => encodeURIComponent(String(v && v.message ? v.message : v));

// ---------------------------------------------------------------------------------------------------------------
// Finishing a delivery — shared by the foreground callbacks and the headless task.
// ---------------------------------------------------------------------------------------------------------------

const DEFAULT_KNOBS = {finishDelayMs: 1000, withholdFinish: false, finishOnTimeout: true};

/** ms until finish(taskId), or null to withhold it: a fetch after finishDelayMs unless withholdFinish; a timeout
 *  at once unless finishOnTimeout is false (the bridge's own default onTimeout finishes too). */
function finishAfter(timeout, knobs) {
  if (timeout) return knobs.finishOnTimeout === false ? null : 0;
  if (knobs.withholdFinish) return null;
  const ms = Number(knobs.finishDelayMs);
  return isFinite(ms) && ms > 0 ? ms : 0;
}

/** Calls finish(taskId) as the knobs say and reports it through `say(outcome, afterMs)`. */
function finishPer(taskId, timeout, knobs, say) {
  const after = finishAfter(timeout, knobs);
  if (after === null) { say('withheld', '-'); return Promise.resolve(); }
  return new Promise((resolve) => setTimeout(() => {
    try {
      BackgroundFetch.finish(taskId);
      say('ok', after);
    } catch (e) {
      say('threw', after, e);
    }
    resolve();
  }, after));
}

// ---------------------------------------------------------------------------------------------------------------
// The headless task: a process JobScheduler started with no Activity.  It has no launch intent, so it reads the
// knobs the LAST launch persisted.  Nothing here logs `point=headless-…`: a headless counter reads every
// `point=headless` line as a delivery.
// ---------------------------------------------------------------------------------------------------------------

BackgroundFetch.registerHeadlessTask(async (event) => {
  const taskId = event && event.taskId;
  const timeout = !!(event && event.timeout);
  // The lab's headless names (labs/harness/vocabulary.yaml enums.Event): `fetch` for a HeadlessEvent whose timeout
  // is false, `timeout` otherwise — headlessEvents.fetch / headlessEvents.timeout count them apart.
  console.log(TAG + ' point=headless event=' + (timeout ? 'timeout' : 'fetch') + ' taskId=' + field(taskId) +
              ' timeout=' + timeout);
  let knobs = DEFAULT_KNOBS;
  try {
    knobs = Object.assign({}, DEFAULT_KNOBS, await Lab.knobs());
  } catch (e) { /* defaults */ }
  try {
    console.log(TAG + ' point=task-probe ' + (await Lab.probe()));
  } catch (e) { /* display only */ }
  // Awaited: React Native ends the headless task when this promise settles, and the plugin's HeadlessTask finishes
  // ITS task on our finish(taskId) — so the delay must run inside the task.
  await finishPer(taskId, timeout, knobs, (outcome, after, e) => console.log(
    TAG + ' point=task-api api=finish args=' + field(taskId) + ' outcome=' + outcome + ' after_ms=' + after +
    (e ? ' message=' + field(e) : '')));
});

// ---------------------------------------------------------------------------------------------------------------
// The foreground driver
// ---------------------------------------------------------------------------------------------------------------

let ticks = 0, boot = 0, beats = 0, fetches = 0, timeouts = 0, finished = 0;
let hooks = [], knobs = DEFAULT_KNOBS;
let armed = false, ticking = false, apiRan = 0, firstQueued = false, beating = false;
let configuring = null;            // the last configure() promise: the first batch waits for it
let testView = 'configure pending';
let labEvents = [], labEventsArmed = 0, labEventsRender = null;
let labView = null, labHeader = '';
const LAB_TONE = {dim: '#8a8a8e', plain: '#1c1c1e', mark: '#3d6fa3', pass: '#1a7f37', fail: '#c0392b'};
const LAB_MONO = {fontFamily: Platform.OS === 'ios' ? 'Menlo' : 'monospace',
                  fontSize: 10, lineHeight: 14, color: LAB_TONE.plain};
let lastVerdict = '', pollingVerdict = false;
let batchChain = Promise.resolve();   // one batch at a time, slots and the first batch alike
let scenarioName = 'lab';
let nativeLabelArg = '';
let statusBarHeightArg = 0;
const viewListeners = new Set();
const setTestView = (text) => { testView = text; viewListeners.forEach((fn) => fn(text)); };
let statusLine = 'booting';
const statusListeners = new Set();
const setStatusLine = (text) => { statusLine = text; statusListeners.forEach((fn) => fn(text)); };
let tab = 'test';
const tabListeners = new Set();
const selectTab = (t) => { tab = t; tabListeners.forEach((fn) => fn(t)); };

function redraw() {
  if (labView === 'api') setTestView(LabApi.frame(LabApi.lines(labHeader), hooks));
  else if (labView === 'events') { if (labEventsRender) labEventsRender(); }
  else if (labView) setTestView(LabApi.frame([[{text: scenarioName, tone: 'plain'}], [{text: labHeader, tone: 'dim'}]], hooks));
}

// The host's verdict, as run.py last pushed it (bg-forge README §9): display only, never logged.
async function pollVerdict() {
  try {
    const text = await Lab.verdict();
    if (text === lastVerdict) return;
    lastVerdict = text;
    let v = null;
    try { v = text ? JSON.parse(text) : null; } catch (e) { v = null; }
    LabApi.setVerdict(v);
    if (v && v.final) selectTab('test');
    redraw();
  } catch (e) { /* display only */ }
}

// --- deliveries ------------------------------------------------------------------------------------------------

// The `events:` declarations of `configure` armed so far: {onEvent, onTimeout|null}, each handed every delivery.
const subscribers = [];

/** One delivery from configure()'s callbacks.  Logged first, then handed to the declarations, then finished: the
 *  `event` line always precedes the `finish` line it causes.  A declaration is handed what the bridge hands a
 *  customer's callbacks: the bare taskId string (index.js configure: onEvent(event.taskId) / onTimeout(event.taskId);
 *  the vocabulary's `emits: string`).  A timeout goes to the declaration's onTimeout alone, which it passes with
 *  `errors: true`; without one the bridge's default onTimeout takes it, and onEvent never sees it — nor does the
 *  declaration here. */
function onDelivery(taskId, timeout) {
  const n = timeout ? ++timeouts : ++fetches;
  log('event=' + (timeout ? 'timeout' : 'fetch') + ' n=' + n + ' taskId=' + field(taskId));
  for (const s of subscribers.slice()) {
    try {
      if (!timeout) s.onEvent(taskId);
      else if (s.onTimeout) s.onTimeout(taskId);
    } catch (e) {
      log('event-dispatch outcome=threw message=' + field(e));
    }
  }
  finishPer(taskId, timeout, knobs, (outcome, after, e) => {
    if (outcome === 'ok') finished++;
    log('finish taskId=' + field(taskId) + ' timeout=' + timeout + ' after_ms=' + after + ' outcome=' + outcome +
        (e ? ' message=' + field(e) : ''));
  });
}

const fgEvent = (taskId) => onDelivery(taskId, false);
const fgTimeout = (taskId) => onDelivery(taskId, true);

/** The real configure(), always with the driver's own callbacks, echoing the config first (configDropped reads
 *  the last `config json=` line, which must END with the JSON). */
function configureWith(config, source) {
  log('config json=' + JSON.stringify(config));
  const t0 = Date.now();
  const done = (async () => {
    try {
      const status = await BackgroundFetch.configure(config, fgEvent, fgTimeout);
      log('configure source=' + source + ' outcome=ok status=' + status + ' ms=' + (Date.now() - t0));
      return status;
    } catch (e) {
      log('configure source=' + source + ' outcome=rejected ms=' + (Date.now() - t0) + ' message=' + field(e));
      return null;
    }
  })();
  configuring = done;
  return done;
}

// labevents.js arm() calls BG[decl.on](args?, delivered[, failed]).  For fetch the only member that delivers is
// configure, whose callbacks are configure()'s own: so `configure` here SUBSCRIBES the declaration to the driver's
// deliveries (delivered, and failed for `errors: true`) and, when the declaration carries `args`, makes the real
// configure() with them, as given.  A declaration with no `args` only subscribes.  Anything else a declaration
// names is not on this facade and reads `absent`, as an unknown member does.
const EventBG = {
  configure: function (...given) {
    const fns = given.filter((a) => typeof a === 'function');
    if (!fns.length) throw new Error('configure: no callback to deliver to');
    const sub = {onEvent: fns[0], onTimeout: fns[1] || null};
    subscribers.push(sub);
    if (typeof given[0] !== 'function') configureWith(given[0], 'event-arm');
    return {remove: () => {
      const i = subscribers.indexOf(sub);
      if (i >= 0) subscribers.splice(i, 1);
    }};
  },
};

// --- the schedule ----------------------------------------------------------------------------------------------

// One `at_s` slot (bg-forge README §5): its api hooks, then the declarations armed in it, then its removals.
function onSlot(s, due) {
  batchChain = batchChain.then(async () => {
    log('slot at_s=' + s + ' late_ms=' + Math.max(0, Math.round(Date.now() - due)));
    apiRan = await LabApi.runBatch(BackgroundFetch, hooks, log, s, apiRan);
    redraw();
    if (labEvents.length) {
      labEventsArmed = LabEvents.arm(EventBG, labEvents, log, LabApi.ret, LabApi.walk, labEventsRender, s, labEventsArmed);
      await LabEvents.remove(labEvents, log, s);
    }
  });
}

// Legacy tick-counted `call` hooks: fetch has none (lab.yaml supports.calls: []), so any is reported unsupported.
async function runHooks(n) {
  for (const hook of hooks.filter((h) => 'call' in h && h.at === n)) {
    log('call=' + hook.call + ' outcome=unsupported');
  }
}

async function tick() {
  const n = ++ticks;
  await batchChain;
  let status = '-';
  try { status = await BackgroundFetch.status(); } catch (e) { status = 'rejected'; }
  let probe = '';
  try { probe = await Lab.probe(); } catch (e) { probe = 'probeError=' + field(e); }
  const line = 'tick n=' + n + ' boot=' + boot + ' status=' + status + ' fetches=' + fetches + ' timeouts=' + timeouts +
      ' finished=' + finished + ' ' + probe;
  log(line);
  setStatusLine(line);
  await runHooks(n);
}

async function main() {
  const t0 = Date.now();
  const args = await Lab.args();
  const argsMs = Date.now() - t0;
  boot = args.boot;
  scenarioName = args.scenario || 'lab';
  nativeLabelArg = args.nativeLabel || '';
  statusBarHeightArg = args.statusBarHeight || 0;
  setTestView(testView);
  log('boot n=' + boot + ' args=' + JSON.stringify(args));
  // The beat: every 0.5 s from the boot line, touching neither the SDK nor the native module.
  if (!beating) {
    beating = true;
    setInterval(() => log('beat n=' + (++beats)), 500);
  }
  if (!pollingVerdict) {
    pollingVerdict = true;
    setInterval(pollVerdict, 250);
  }

  // Once per JS context: the arm, and the configure() it may make or the driver makes instead.
  if (!armed) {
    armed = true;
    hooks = JSON.parse(args.hooks || '[]');
    knobs = {finishDelayMs: args.finishDelayMs, withholdFinish: !!args.withholdFinish,
             finishOnTimeout: args.finishOnTimeout !== false};
    labEvents = JSON.parse(args.events || '[]');
    labView = ['api', 'events', 'title'].includes(args.view) ? args.view
      : labEvents.length ? 'events' : hooks.some((h) => h && typeof h.api === 'string') ? 'api' : 'title';
    labHeader = 'boot=' + boot;
    if (labView === 'events') {
      labEventsRender = () => setTestView(LabApi.frame(LabEvents.lines(labEventsRender.header, LabApi), hooks));
      labEventsRender.header = labHeader + '  armed ' + labEvents.length;
    }
    redraw();
    // The declarations armed at boot come BEFORE the configure() that causes their deliveries.  A boot-armed
    // `configure` declaration makes that configure() itself (EventBG), so the driver then makes none of its own.
    labEventsArmed = LabEvents.arm(EventBG, labEvents, log, LabApi.ret, LabApi.walk, labEventsRender, 'boot', 0);
    const armConfigured = labEvents.some((d) => d && d.on === 'configure' && d.at_s === undefined && 'args' in d);
    if (args.noConfigure) {
      log('configure source=boot outcome=skipped reason=noConfigure');
    } else if (armConfigured) {
      log('configure source=boot outcome=skipped reason=event-arm');
    } else {
      // BackgroundFetchConfig is flat: configB64's keys are merged over the lab's defaults.
      const extra = JSON.parse(args.config || '{}');
      configureWith(Object.assign({minimumFetchInterval: 15, stopOnTerminate: args.stopOnTerminate,
                                   startOnBoot: false, enableHeadless: true}, extra), 'boot');
    }
  }
  const tickGcp = LabApi.tickGcpFor(hooks, labEvents);   // the protocol's derived flag; no per-tick cause here

  const tConf = Date.now();
  if (configuring) await configuring;
  log('timing argsMs=' + argsMs + ' configureMs=' + (Date.now() - tConf));

  // The first batch — every `api` hook with no `at_s` — once configure() has settled, then the anchor line and
  // one timer per distinct `at_s` (labapi.js startSlots).  Once per JS context.
  if (!firstQueued) {
    firstQueued = true;
    const tBatch = Date.now();
    batchChain = batchChain.then(async () => {
      apiRan = await LabApi.runBatch(BackgroundFetch, hooks, log, 'first', apiRan);
      redraw();
      const times = LabApi.slotTimes(hooks, labEvents);
      log(LabApi.anchorLine(Date.now() - tBatch, times, tickGcp));
      LabApi.startSlots(times, onSlot);
    });
  }
  if (!ticking) {
    ticking = true;
    setInterval(tick, 4000);
  }
}

// The Log tab: the driver's own lines, newest last, one <Text> per line.
const LabLog = React.memo(() => {
  const [lines, setLines] = React.useState(() => logLines.slice());
  const scroller = React.useRef(null), follow = React.useRef(true);
  React.useEffect(() => {
    const update = () => setLines(logLines.slice());
    logListeners.add(update);
    update();
    return () => { logListeners.delete(update); };
  }, []);
  return (
    <ScrollView ref={scroller} style={{flex: 1}} contentContainerStyle={{padding: 10}} scrollEventThrottle={100}
                onScroll={({nativeEvent: e}) => {
                  follow.current = e.contentOffset.y + e.layoutMeasurement.height >= e.contentSize.height - 20;
                }}
                onContentSizeChange={() => { if (follow.current && scroller.current) scroller.current.scrollToEnd({animated: false}); }}>
      {lines.map((l) => <Text key={l.n} style={LAB_MONO}>{l.text}</Text>)}
    </ScrollView>
  );
});

const App = () => {
  const [view, setView] = React.useState(testView);
  const [status, setStatus] = React.useState(statusLine);
  const [shown, setShown] = React.useState(tab);
  React.useEffect(() => {
    viewListeners.add(setView);
    statusListeners.add(setStatus);
    tabListeners.add(setShown);
    main().catch((e) => log('error ' + field(e)));
    return () => { viewListeners.delete(setView); statusListeners.delete(setStatus); tabListeners.delete(setShown); };
  }, []);
  return (
    <View style={{flex: 1, backgroundColor: '#fff'}}>
      <View style={{backgroundColor: '#fedd1e', flexDirection: 'row', alignItems: 'center',
                    paddingTop: Platform.OS === 'ios' ? 56 : (statusBarHeightArg || 24) + 8,
                    paddingBottom: 8, paddingHorizontal: 10}}>
        <Image source={{uri: LabLogo}} style={{width: 30, height: 30, borderRadius: 6, marginRight: 9}} />
        <View>
          <Text style={{color: '#000', fontWeight: '600', fontSize: 13}}>{'Fetch RN Lab: ' + scenarioName}</Text>
          <Text style={{color: '#5a4a00', fontSize: 10}}>
            {['react-native', Platform.OS, nativeLabelArg].filter(Boolean).join(' · ')}
          </Text>
        </View>
      </View>
      <View style={{flexDirection: 'row', borderBottomWidth: 1, borderBottomColor: '#e5e5ea'}}>
        {[['test', 'Test'], ['log', 'Log']].map(([id, label]) => (
          <Pressable key={id} onPress={() => selectTab(id)}
                     style={{paddingVertical: 8, paddingHorizontal: 14, borderBottomWidth: 2,
                             borderBottomColor: shown === id ? LAB_TONE.plain : 'transparent'}}>
            <Text style={{fontSize: 12, fontWeight: shown === id ? '600' : '400',
                          color: shown === id ? LAB_TONE.plain : LAB_TONE.dim}}>{label}</Text>
          </Pressable>
        ))}
      </View>
      {shown === 'log' ? <LabLog /> : (
        <ScrollView style={{flex: 1}} contentContainerStyle={{padding: 10}}>
          {/* One <Text> per LINE (bg-forge WO-027 C1): one tree of the whole view passes Android's span limit. */}
          {Array.isArray(view)
            ? view.map((segments, i) => (
                <Text key={i} style={LAB_MONO}>
                  {segments.some((seg) => seg.text) ? segments.map((seg, j) => (
                    <Text key={j} style={{color: LAB_TONE[seg.tone] || LAB_TONE.plain}}>{seg.text}</Text>
                  )) : ' '}
                </Text>
              ))
            : <Text style={LAB_MONO}>{view}</Text>}
          <Text style={{fontFamily: LAB_MONO.fontFamily, fontSize: 9, color: '#777', marginTop: 14}}>{status}</Text>
        </ScrollView>
      )}
    </View>
  );
};

AppRegistry.registerComponent(appName, () => App);
