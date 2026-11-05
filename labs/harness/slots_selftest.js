#!/usr/bin/env node
// selftest (WO-027 Tier B, B2) — the drivers' slot schedule, run offline against the shared
// JavaScript every JS driver loads: labs/platforms/_shared/labapi.js and labevents.js.
//
//   node labs/harness/slots_selftest.js
//
// It lives HERE, not beside the modules: every file under _shared/ is stamped into all 8 lab
// builds, so a test there would make every cell stale.  Plain Node, no dependencies.  A fake BG
// stands in for the SDK, and a fake clock and timer queue stand in for Date.now/setTimeout, so
// the slots fire in order, instantly, and the due times are exact.
//
// The Dart driver carries its own port of both modules; nothing here reaches it.
'use strict';

const path = require('path');
const shared = path.join(__dirname, '..', 'platforms', '_shared');
const LabApi = require(path.join(shared, 'labapi.js'));
const LabEvents = require(path.join(shared, 'labevents.js'));

let failures = 0;

function check(title, got, want) {
  const ok = JSON.stringify(got) === JSON.stringify(want);
  failures += ok ? 0 : 1;
  console.log((ok ? 'ok  ' : 'FAIL') + '  ' + title +
              (ok ? '' : '\n      got  ' + JSON.stringify(got) + '\n      want ' + JSON.stringify(want)));
}

// Members resolve values, sync and async; `bogus`, `getLog` and `onBogus` are absent.
const calls = [];
const subscribers = {};
const BG = {
  getState: async () => { calls.push('getState'); return {enabled: false}; },
  getCount: () => { calls.push('getCount'); return 3; },
  start: async () => { calls.push('start'); return {enabled: true}; },
  changePace: async (moving) => { calls.push('changePace(' + moving + ')'); return true; },
  stop: () => { calls.push('stop'); return Promise.resolve({enabled: false}); },
  onLocation: (cb) => { subscribers.loc = cb; return {remove() {}}; },
  onMotionChange: (cb) => { subscribers.mc = cb; return {remove() {}}; },
  watchPosition: (options, cb) => { subscribers.wp = {options: options, cb: cb}; return {remove() {}}; },
};

function collector() {
  const out = [];
  const log = (line) => out.push(line);
  log.lines = out;
  log.take = () => out.splice(0, out.length);
  return log;
}

// Only the lines that say what ran — the ret lines are labapi's own grammar, pinned elsewhere.
const outcomes = (lines) => lines.filter((l) => /^(api |api-done |event-arm |events-armed )/.test(l));

// The api hooks of one step, legacy `call` hooks at their list positions: ids are <api>#<index>
// over the FULL list.
const hooks = [
  {api: 'getState', paths: ['$.enabled']},     // #0  first
  {at: 3, call: 'reset', args: 'same'},        // #1  legacy: tick 3, never a slot
  {api: 'start', at_s: 2},                     // #2  slot 2
  {api: 'getCount'},                           // #3  first
  {api: 'stop', at_s: 2.5},                    // #4  slot 2.5
  {api: 'changePace', args: true, at_s: 2},    // #5  slot 2, after start
  {api: 'bogus'},                              // #6  first, absent
];

const events = [
  {on: 'onLocation', id: 'loc', paths: ['$.coords.latitude']},   // boot
  {on: 'onMotionChange', id: 'mc'},                              // boot
  {on: 'watchPosition', id: 'wp', args: {interval: 1000}, at_s: 2},
  {on: 'onBogus', id: 'ab', at_s: 2.5},                          // absent
];

async function main() {
  // --- runBatch -------------------------------------------------------------------------
  let log = collector();
  let n = await LabApi.runBatch(BG, hooks, log, 'first', 0);
  check("'first' runs only the hooks with no at_s, in list order",
        outcomes(log.take()),
        ['api id=getState#0 outcome=ok', 'api id=getCount#3 outcome=ok', 'api id=bogus#6 outcome=absent',
         'api-done n=3']);
  check("'first' returns the total so far", n, 3);

  n = await LabApi.runBatch(BG, hooks, log, 2, n);
  const slot2 = log.take();
  check('slot 2 runs its own hooks and never re-runs the first batch',
        outcomes(slot2), ['api id=start#2 outcome=ok', 'api id=changePace#5 outcome=ok', 'api-done n=5']);
  check('a hook with args is still called with them', calls.slice(-2), ['start', 'changePace(true)']);

  n = await LabApi.runBatch(BG, hooks, log, 2.5, n);
  check('2.5 is its own slot, and api-done n= accumulates across slots',
        outcomes(log.take()), ['api id=stop#4 outcome=ok', 'api-done n=6']);

  const before = n;
  n = await LabApi.runBatch(BG, hooks, log, 3, n);
  check('a slot with nothing due logs nothing — the legacy at: 3 is not a slot', log.take(), []);
  check('... and returns the total unchanged', n, before);

  for (const [title, slot] of [["'boot' runs nothing — it is labevents' sentinel", 'boot'],
                               ['an undefined slot runs nothing', undefined],
                               ['slot 0 runs nothing — at_s is always > 0', 0]]) {
    await LabApi.runBatch(BG, hooks, log, slot, n);
    check(title, log.take(), []);
  }
  check('each hook ran exactly once over the whole schedule',
        calls, ['getState', 'getCount', 'start', 'changePace(true)', 'stop']);

  // --- arm ------------------------------------------------------------------------------
  log = collector();
  let armed = LabEvents.arm(BG, events, log, LabApi.ret, LabApi.walk, null, 'boot', 0);
  check("arm('boot') arms only the declarations with no at_s",
        outcomes(log.take()), ['event-arm id=loc outcome=ok', 'event-arm id=mc outcome=ok', 'events-armed n=2']);
  check("arm('boot') returns the total so far", armed, 2);

  subscribers.loc({coords: {latitude: 45.5}});
  check('a boot subscription still delivers',
        log.take(), ['event id=loc n=1', 'ret id=loc path=$.coords.latitude type=number value=45.5 parent=1']);

  armed = LabEvents.arm(BG, events, log, LabApi.ret, LabApi.walk, null, 2, armed);
  check('arm(2) arms only at_s: 2, and never re-arms the boot declarations',
        outcomes(log.take()), ['event-arm id=wp outcome=ok', 'events-armed n=3']);
  check('arm(2) passes the declaration\'s args before the callback', subscribers.wp.options, {interval: 1000});

  armed = LabEvents.arm(BG, events, log, LabApi.ret, LabApi.walk, null, 2.5, armed);
  check('arm(2.5) is its own slot, and events-armed n= accumulates',
        outcomes(log.take()), ['event-arm id=ab outcome=absent', 'events-armed n=4']);

  for (const [title, slot] of [['arm(4) with nothing due logs nothing', 4],
                               ["arm('first') arms nothing — it is labapi's sentinel", 'first'],
                               ['arm(0), the old boot value, arms nothing — a number is only ever an at_s', 0]]) {
    const total = LabEvents.arm(BG, events, log, LabApi.ret, LabApi.walk, null, slot, armed);
    check(title, [log.take(), total], [[], armed]);
  }

  // --- slotTimes, tickGcpFor, anchorLine ------------------------------------------------
  check('slotTimes: hooks and events, sorted, deduped; 2 and 2.5 are distinct',
        LabApi.slotTimes(hooks, events), [2, 2.5]);
  check('slotTimes sorts numerically, not as text, and a legacy at: is never a slot',
        LabApi.slotTimes([{api: 'a', at_s: 10}, {api: 'b', at_s: 2}, {call: 'reset', at: 3}, {api: 'c'}],
                         [{on: 'onX', id: 'x', at_s: 4}, {on: 'onY', id: 'y', at_s: 10}, {on: 'onZ', id: 'z'}]),
        [2, 4, 10]);
  check('slotTimes of a legacy step, and of nothing, is empty',
        [LabApi.slotTimes([{at: 3, call: 'reset'}], []), LabApi.slotTimes(undefined, undefined)], [[], []]);

  check('tickGcpFor: on for a legacy step and an empty one',
        [LabApi.tickGcpFor([{at: 3, call: 'reset'}], []), LabApi.tickGcpFor([], []),
         LabApi.tickGcpFor(undefined, undefined)], [true, true, true]);
  check('tickGcpFor: off with any api hook, or any event declaration',
        [LabApi.tickGcpFor(hooks, []), LabApi.tickGcpFor([], events),
         LabApi.tickGcpFor([{at: 2, call: 'stop'}], [{on: 'onLocation', id: 'loc'}])], [false, false, false]);

  check('anchorLine formats slots as JavaScript prints them',
        LabApi.anchorLine(4123.4, [2, 2.5, 8], false),
        'anchor batchMs=4123 slots=2,2.5,8 tickGcp=off');
  check("anchorLine writes '-' when there are no slots",
        LabApi.anchorLine(0, [], true), 'anchor batchMs=0 slots=- tickGcp=on');

  // --- startSlots -----------------------------------------------------------------------
  const timers = [], fired = [];
  const result = LabApi.startSlots([2, 2.5, 8], (s, due) => fired.push([s, due]), () => 1000,
                                   (fn, ms) => timers.push({fn: fn, ms: ms}));
  check('startSlots returns nothing', result, undefined);
  check('startSlots schedules one timer per slot at s * 1000 ms', timers.map((t) => t.ms), [2000, 2500, 8000]);
  timers.forEach((t) => t.fn());
  check('each timer calls onSlot(s, t0 + s * 1000)', fired, [[2, 3000], [2.5, 3500], [8, 9000]]);
  const none = [];
  LabApi.startSlots([], () => none.push('slot'), () => 0, (fn, ms) => none.push(ms));
  check('startSlots with no slots schedules nothing', none, []);

  // --- the strip ------------------------------------------------------------------------
  const strip = LabApi.schedule([{api: 'getOdometer', at_s: 2}, {api: 'getLog', at_s: 2.5},
                                 {call: 'reset', at: 3}, {api: 'getProviderState'}])
    .map((segs) => segs.map((s) => s.text).join('')).join('\n');
  check("the strip labels a deferred hook '+<at_s>s', an undeferred one not at all, a legacy one never",
        strip, '  calls  ○ getOdometer +2s   ○ getLog +2.5s   \n         ○ getProviderState   ');

  // --- a driver's schedule, end to end --------------------------------------------------
  // The contract's main()/onSlot, over the same fakes: the first batch queued on batchChain,
  // the anchor after it, then each slot's batch and arming in that order.
  LabApi.rows.length = 0;
  LabEvents.rows.length = 0;
  log = collector();
  let clock = 5000;
  const queue = [];
  let batchChain = Promise.resolve(), apiRan = 0, eventsArmed = 0;
  eventsArmed = LabEvents.arm(BG, events, log, LabApi.ret, LabApi.walk, null, 'boot', eventsArmed);
  const onSlot = (s, due) => {
    batchChain = batchChain.then(async () => {
      log('slot at_s=' + s + ' late_ms=' + Math.max(0, Math.round(clock - due)));
      apiRan = await LabApi.runBatch(BG, hooks, log, s, apiRan);
      eventsArmed = LabEvents.arm(BG, events, log, LabApi.ret, LabApi.walk, null, s, eventsArmed);
    });
  };
  const t0 = clock;
  batchChain = batchChain.then(async () => {
    apiRan = await LabApi.runBatch(BG, hooks, log, 'first', apiRan);
    clock += 120;                                   // the first batch took 120 ms
    const times = LabApi.slotTimes(hooks, events);
    log(LabApi.anchorLine(clock - t0, times, LabApi.tickGcpFor(hooks, events)));
    LabApi.startSlots(times, onSlot, () => clock, (fn, ms) => queue.push({fn: fn, at: clock + ms}));
  });
  await batchChain;
  clock += 2000; queue.shift().fn(); await batchChain;
  clock += 530;  queue.shift().fn(); await batchChain;  // 30 ms late
  check('the driver schedule: boot arm, first batch, anchor, then each slot as batch then arm',
        log.lines.filter((l) => !/^(ret|event) /.test(l)),
        ['event-arm id=loc outcome=ok', 'event-arm id=mc outcome=ok', 'events-armed n=2',
         'api id=getState#0 outcome=ok', 'api id=getCount#3 outcome=ok', 'api id=bogus#6 outcome=absent',
         'api-done n=3',
         'anchor batchMs=120 slots=2,2.5 tickGcp=off',
         'slot at_s=2 late_ms=0',
         'api id=start#2 outcome=ok', 'api id=changePace#5 outcome=ok', 'api-done n=5',
         'event-arm id=wp outcome=ok', 'events-armed n=3',
         'slot at_s=2.5 late_ms=30',
         'api id=stop#4 outcome=ok', 'api-done n=6',
         'event-arm id=ab outcome=absent', 'events-armed n=4']);

  log = collector();
  batchChain = Promise.resolve();
  const legacy = [{at: 3, call: 'reset', args: 'same'}];
  const legacyStart = clock;
  batchChain = batchChain.then(async () => {
    const ran = await LabApi.runBatch(BG, legacy, log, 'first', 0);
    const times = LabApi.slotTimes(legacy, []);
    log(LabApi.anchorLine(clock - legacyStart, times, LabApi.tickGcpFor(legacy, [])));
    LabApi.startSlots(times, () => log('slot'), () => clock, () => log('timer'));
    return ran;
  });
  await batchChain;
  check('a legacy step: the batch runs nothing, the anchor is still logged, no slot is scheduled',
        log.lines, ['anchor batchMs=0 slots=- tickGcp=on']);

  // --- an overrun: slot 2's call is still pending when slot 2.5 falls due ------------------
  // The chain, not the timer, orders the slots: slot 2.5 waits for slot 2's last call to return,
  // never overlaps it, and reports how late it started.
  log = collector();
  const order = [];
  let release;
  const BG2 = {
    slow: () => { order.push('slow>'); return new Promise((res) => { release = () => { clock += 900; order.push('<slow'); res(true); }; }); },
    quick: () => { order.push('quick>'); return true; },
  };
  const hooks2 = [{api: 'slow', at_s: 2}, {api: 'quick', at_s: 2.5}];
  const due2 = [];
  batchChain = Promise.resolve();
  apiRan = 0;
  const onSlot2 = (s, due) => {
    batchChain = batchChain.then(async () => {
      log('slot at_s=' + s + ' late_ms=' + Math.max(0, Math.round(clock - due)));
      apiRan = await LabApi.runBatch(BG2, hooks2, log, s, apiRan);
    });
  };
  const base = clock;
  LabApi.startSlots([2, 2.5], onSlot2, () => clock, (fn) => due2.push(fn));
  const settle = () => new Promise((r) => setImmediate(r));
  clock = base + 2000; due2[0](); await settle();       // slot 2 starts; its call is pending
  clock = base + 2500; due2[1](); await settle();       // slot 2.5 falls due meanwhile
  check('an overrun: slot 2.5 has not started while slot 2\'s call is pending', order, ['slow>']);
  release();                                            // returns at base + 3400
  await batchChain;
  check('... it starts once that call returns, never alongside it', order, ['slow>', '<slow', 'quick>']);
  check('... and reports how late it started',
        log.lines, ['slot at_s=2 late_ms=0', 'api id=slow#0 outcome=ok', 'api-done n=1',
                    'slot at_s=2.5 late_ms=900', 'api id=quick#1 outcome=ok', 'api-done n=2']);

  // --- (WO-107) generate: and alongside: ----------------------------------------------------
  // `generate` hands the member a seeded set of polygons in the documented polygon form; `alongside` issues a
  // second call before the first settles and logs the ms since the first was issued.  Real time here: a
  // synchronous member (Cordova's exec returns only when the work is done) must show its blocking time, an
  // asynchronous one must not.
  const generated = [];
  const busy = (ms) => { const end = Date.now() + ms; while (Date.now() < end) { /* the bridge's work */ } };
  const BG3 = {
    addGeofences(fences) { generated.push(fences); return new Promise((r) => setTimeout(() => r(true), 120)); },
    getCount() { return Promise.resolve(0); },
  };
  const BG4 = {
    addGeofences(fences) { busy(150); return Promise.resolve(true); },
    getCount() { return Promise.resolve(0); },
  };
  const bulk = (seed, beside) => [{api: 'addGeofences', generate: {geofences: 3, vertices: 5, seed: seed},
                                   alongside: beside, paths: ['$']}];
  const out3 = [];
  const log3 = (line) => out3.push(line);
  await LabApi.runBatch(BG3, bulk(7, 'getCount'), log3, 'first', 0);
  await LabApi.runBatch(BG3, bulk(7, 'getCount'), log3, 'first', 0);
  await LabApi.runBatch(BG3, bulk(8, 'getCount'), log3, 'first', 0);
  const first = generated[0];
  check('(WO-107) generate: the member is called with that many polygons, `vertices` points each, wo107-<i>',
        [first.length, first.map((f) => f.identifier), first.map((f) => f.vertices.length)],
        [3, ['wo107-0', 'wo107-1', 'wo107-2'], [5, 5, 5]]);
  check('(WO-107) ... in the documented polygon form: no latitude, longitude or radius',
        first.some((f) => 'latitude' in f || 'longitude' in f || 'radius' in f), false);
  check('(WO-107) ... the same seed gives the same set, another seed another',
        [JSON.stringify(generated[1]) === JSON.stringify(first), JSON.stringify(generated[2]) === JSON.stringify(first)],
        [true, false]);
  const ms = (lines) => Number((lines.find((l) => /^alongside /.test(l)) || '').replace(/.* ms=/, ''));
  check('(WO-107) alongside: an asynchronous member does not hold the second call — it settles before the first',
        [ms(out3) < 100, out3.indexOf(out3.find((l) => /^alongside .* outcome=ok/.test(l))) <
                          out3.indexOf('api id=addGeofences#0 outcome=ok')], [true, true]);
  const out4 = [];
  await LabApi.runBatch(BG4, bulk(7, 'getCount'), (line) => out4.push(line), 'first', 0);
  check('(WO-107) ... a synchronous one does: the second call cannot even be issued until the first returns',
        [ms(out4) >= 150, out4.filter((l) => /^(alongside|api) /.test(l)).map((l) => l.replace(/ ms=\d+/, ''))],
        [true, ['alongside id=addGeofences#0 api=getCount outcome=ok', 'api id=addGeofences#0 outcome=ok']]);
  const out5 = [];
  await LabApi.runBatch(BG3, bulk(7, 'getCounts'), (line) => out5.push(line), 'first', 0);
  check('(WO-107) ... and a member the bridge lacks reads absent, the hook\'s own call unaffected',
        out5.filter((l) => /^(alongside|api) /.test(l)).map((l) => l.replace(/ ms=\d+/, '')),
        ['alongside id=addGeofences#0 api=getCounts outcome=absent', 'api id=addGeofences#0 outcome=ok']);

  // --- (Tier C) R: dotted members, a synchronous throw, per-delivery paths, error callbacks, removal ---
  const said = (lines) => lines.filter((l) => /^(api |ret |ret-error |alongside )/.test(l));
  class Settings { static isPowerSaveMode() { return this === Settings; } }
  const BG6 = {
    logger: {prefix: 'L', ORDER_ASC: 1, getLog(q) { return Promise.resolve(this.prefix + q.limit); }},
    deviceSettings: Settings,
    getState: async () => ({enabled: true}),
    boom: () => { throw new TypeError('bad argument'); },
    reject: () => Promise.reject({code: 404}),
    asyncBoom: async () => { throw new Error('inside'); },
  };
  let out6 = [];
  await LabApi.runBatch(BG6, [{api: 'logger.getLog', args: {limit: 5}, paths: ['$']},
                              {api: 'deviceSettings.isPowerSaveMode', paths: ['$']},
                              {api: 'logger.bogus'}, {api: 'nothing.there'}, {api: 'logger.getLog.deeper'},
                              {api: 'logger.ORDER_ASC', probe: true, paths: ['$']},
                              {api: 'logger', probe: true, paths: ['$.getLog']}],
                        (line) => out6.push(line), 'first', 0);
  check('(R3) a dotted member is found on its owner and CALLED on it: an instance reads its own `this`, a class ' +
        'of statics its own; a member or an owner that is not there reads absent; a probe takes a dotted name too',
        said(out6),
        ['api id=logger.getLog#0 outcome=ok', 'ret id=logger.getLog#0 path=$ type=string value=L5 parent=1',
         'api id=deviceSettings.isPowerSaveMode#1 outcome=ok',
         'ret id=deviceSettings.isPowerSaveMode#1 path=$ type=boolean value=true parent=1',
         'api id=logger.bogus#2 outcome=absent', 'api id=nothing.there#3 outcome=absent',
         'api id=logger.getLog.deeper#4 outcome=absent',
         'api id=logger.ORDER_ASC#5 outcome=ok', 'ret id=logger.ORDER_ASC#5 path=$ type=number value=1 parent=1',
         'api id=logger#6 outcome=ok', 'ret id=logger#6 path=$.getLog type=function parent=1']);

  out6 = [];
  await LabApi.runBatch(BG6, [{api: 'boom', paths: ['$', '$err', '$err.message']},
                              {api: 'reject', paths: ['$', '$err.code']},
                              {api: 'asyncBoom', paths: ['$err.message']},
                              {api: 'boom', alongside: 'getState'}],
                        (line) => out6.push(line), 'first', 0);
  check('(R5) a member that throws as it is called is `threw`, apart from a promise that rejects (an async ' +
        'function\'s own throw is one); the error side is reported for both and the value side for neither; and ' +
        'nothing is issued alongside a call that never returned',
        said(out6),
        ['api id=boom#0 outcome=threw', 'ret id=boom#0 path=$err type=object parent=1',
         'ret id=boom#0 path=$err.message type=string value=bad%20argument parent=1',
         'api id=reject#1 outcome=rejected', 'ret id=reject#1 path=$err.code type=number value=404 parent=1',
         'api id=asyncBoom#2 outcome=rejected',
         'ret id=asyncBoom#2 path=$err.message type=string value=inside parent=1',
         'api id=boom#3 outcome=threw']);

  const got = {}, removedIds = [];
  const sub = (name) => ({remove() { removedIds.push(name); }});
  const BG7 = {
    onLocation: function () { got.loc = Array.prototype.slice.call(arguments); return sub('loc'); },
    onGeofence: function () { got.geo = Array.prototype.slice.call(arguments); return sub('geo'); },
    watchPosition: function () {
      got.wp = Array.prototype.slice.call(arguments);
      return Promise.resolve(sub('wp'));
    },
    onHttp: function () { return {remove() { throw new Error('no'); }}; },
    onHeartbeat: function () { return {remove() { return Promise.reject(new Error('later')); }}; },
    onSchedule: function () { return undefined; },
    onAuthorization: function () { return Promise.reject(new Error('never subscribed')); },
  };
  const decls = [
    {on: 'onLocation', id: 'loc@0', paths: ['$.v', '$err.code'], errors: true, remove_at_s: 8},
    {on: 'onGeofence', id: 'geo@1', paths: ['$.action'], delivery: 2},
    {on: 'watchPosition', id: 'wp@2', args: {interval: 1}, paths: ['$.v', '$err'], errors: true, remove_at_s: 8},
    {on: 'onHttp', id: 'http@3', remove_at_s: 8},
    {on: 'onHeartbeat', id: 'hb@4', remove_at_s: 9},
    {on: 'onSchedule', id: 'sch@5', remove_at_s: 9},
    {on: 'onBogus', id: 'ab@6', remove_at_s: 9},
    {on: 'onAuthorization', id: 'auth@7', remove_at_s: 9},
  ];
  log = collector();
  LabEvents.arm(BG7, decls, log, LabApi.ret, LabApi.walk, null, 'boot', 0);
  log.take();
  check('(R6) `errors: true` passes the failure callback after the success one, and after `args` where there ' +
        'are any; without it the member gets the one callback',
        [got.loc.map((a) => typeof a), got.wp.map((a) => typeof a), got.geo.map((a) => typeof a)],
        [['function', 'function'], ['object', 'function', 'function'], ['function']]);
  got.loc[0]({v: 1});
  got.loc[1]({code: 408});
  got.loc[1]({code: 1});
  got.loc[0]({v: 2});
  check('(R6) a delivery reports the value paths and an error the `$err` paths, each from its first only; every ' +
        'error is counted on its own line',
        log.take(),
        ['event id=loc@0 n=1', 'ret id=loc@0 path=$.v type=number value=1 parent=1',
         'event-error id=loc@0 n=1', 'ret id=loc@0 path=$err.code type=number value=408 parent=1',
         'event-error id=loc@0 n=2', 'event id=loc@0 n=2']);
  ['ENTER', 'EXIT', 'DWELL'].forEach((action) => got.geo[0]({action: action}));
  check('(R4) `delivery: 2` reports the paths for deliveries 1 and 2, each line naming its own, and no later one',
        log.take(),
        ['event id=geo@1 n=1', 'ret id=geo@1 n=1 path=$.action type=string value=ENTER parent=1',
         'event id=geo@1 n=2', 'ret id=geo@1 n=2 path=$.action type=string value=EXIT parent=1',
         'event id=geo@1 n=3']);
  check('(R4) ... and the screen row shows the delivery the host reads',
        LabEvents.rows.find((r) => r.id === 'geo@1').seen.map((e) => e.value), ['EXIT']);

  await LabEvents.remove(decls, log, 'boot');
  await LabEvents.remove(decls, log, 7);
  check('(R1) nothing is removed outside its slot, and never at boot', [log.take(), removedIds], [[], []]);
  await LabEvents.remove(decls, log, 8);
  check('(R1) in its slot each declaration\'s Subscription is removed, in list order, with the deliveries it ' +
        'had seen: a thenable of one is awaited first, and a remove() that throws reads `threw`',
        [log.take(), removedIds],
        [['event-removed id=loc@0 n=2 outcome=ok', 'event-removed id=wp@2 n=0 outcome=ok',
          'event-removed id=http@3 n=0 outcome=threw'], ['loc', 'wp']]);
  await LabEvents.remove(decls, log, 9);
  check('(R1) a remove() that rejects, and a subscription that itself rejected, read `threw`; nothing handed ' +
        'back, and a declaration that never armed, read `absent`',
        log.take(),
        ['event-removed id=hb@4 n=0 outcome=threw', 'event-removed id=sch@5 n=0 outcome=absent',
         'event-removed id=ab@6 n=0 outcome=absent', 'event-removed id=auth@7 n=0 outcome=threw']);
  check('(R2) a declaration\'s `remove_at_s` is a slot beside its `at_s`',
        LabApi.slotTimes([{api: 'a', at_s: 2}], [{on: 'onX', id: 'x', at_s: 4, remove_at_s: 9},
                                                 {on: 'onY', id: 'y', remove_at_s: 2}]), [2, 4, 9]);

  console.log('\nslots selftest: ' + (failures ? 'FAIL (' + failures + ')' : 'PASS'));
  process.exit(failures ? 1 : 0);
}

main().catch((e) => {
  console.log('FAIL  the selftest threw: ' + (e && e.stack || e));
  process.exit(1);
});
