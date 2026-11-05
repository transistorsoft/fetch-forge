// `api` hooks — the conformance batch (WO-027, labs/README.md "API hooks").
//
// One source for every JavaScript driver: a <script> global in a WebView, a CommonJS module under
// Metro.  The Dart driver carries a port of this file; keep the two in step.
//
// The driver is told WHAT to call and WHICH paths to report.  It is never told what is expected:
// every comparator is evaluated on the host (labkit._check), so there is exactly one place a
// wrong result can be excused, and it is not on the device.
(function (root, factory) {
  if (typeof module === 'object' && module.exports) module.exports = factory();
  else root.LabApi = factory();
})(typeof self !== 'undefined' ? self : this, function () {
  const LIMIT = 200;

  // The harness's vocabulary, not the language's: string number boolean null undefined array
  // object function missing.  A Date is an `object` — which is the point when a string was promised.
  function typeOf(v) {
    if (v === null) return 'null';
    if (Array.isArray(v)) return 'array';
    return typeof v;
  }

  // `parent` separates "the key is not there" from "there was nowhere to look": `$.0.sample` must
  // not read as absent from an empty array.
  function walk(value, path) {
    const segs = path.split('.').slice(1);
    for (let i = 0; i < segs.length; i++) {
      const seg = segs[i], last = i === segs.length - 1;
      const walkable = value !== null && (typeof value === 'object' || typeof value === 'function');
      if (!walkable) return {type: 'missing', parent: 0};
      const has = Array.isArray(value) ? (seg === 'length' || (/^\d+$/.test(seg) && Number(seg) < value.length))
                                       : (seg in value);
      if (!has) return {type: 'missing', parent: last ? 1 : 0};
      value = value[seg];
    }
    return {type: typeOf(value), value: value, parent: 1};
  }

  // One `ret` line per declared path, never a serialised blob: the harness reads `key=(\S+)`, so a
  // space would silently truncate.  Only scalars carry a value; containers are asserted by path.
  // `parent=` always closes the line: the host reads a ret line without it as cut in transport.
  // (Tier C, R4) `n`: the delivery this line reports, on an `events:` declaration that asked for one past the
  // first (`delivery:`).  A return value has none, and neither has a declaration that did not ask.
  function ret(log, id, path, value, n) {
    const which = n === undefined ? '' : ' n=' + n;
    try {
      const r = walk(value, path);
      let line = 'ret id=' + id + which + ' path=' + path + ' type=' + r.type;
      if (r.type === 'string') {
        const clipped = r.value.length > LIMIT;
        let end = LIMIT;
        const last = r.value.charCodeAt(end - 1);
        if (clipped && last >= 0xD800 && last <= 0xDBFF) end--;   // never halve a surrogate pair
        line += ' value=' + encodeURIComponent(clipped ? r.value.slice(0, end) : r.value) + (clipped ? ' trunc=1' : '');
      } else if (r.type === 'number' || r.type === 'boolean') {
        line += ' value=' + String(r.value);
      }
      log(line + ' parent=' + r.parent);
    } catch (e) {
      // A throwing getter, or a string encodeURIComponent refuses (a lone surrogate): this path
      // goes `unreported` on the host.  It must not take the rest of the batch with it.
      log('ret-error id=' + id + which + ' path=' + path);
    }
  }

  // What the screen shows for an `api` scenario (WO-027).  OBSERVATION ONLY, like the events
  // view: the rows say what each call answered and what type it was, never whether that was
  // expected — every comparator lives on the host.
  const rows = [];
  const RULE = new Array(48).join('\u2500');

  function pad(s, n) {
    s = String(s);
    while (s.length < n) s += ' ';
    return s;
  }

  function shown(r) {
    if (!r || r.type === 'missing' || r.value === undefined || typeof r.value === 'object') return '';
    const text = String(r.value);
    return text.length > 10 ? text.slice(0, 9) + '\u2026' : text;
  }

  /// What the screen shows, as STRUCTURE rather than a finished string: one entry per line, each
  /// a list of {text, tone} segments.  The driver turns a tone into a colour, because that is a
  /// platform question — HTML spans, React Native <Text>, Flutter TextSpans — and because the
  /// same rows have to render on all four.
  ///
  /// Tones are 'plain' | 'dim' | 'mark' | 'pass' | 'fail'.  The device only knows what ARRIVED, so
  /// it never colours a verdict of its own (WO-027): a green tick for "the call answered" sat
  /// beside rows the host scored red.  Green and red are the HOST'S verdict, pushed by run.py and
  /// read back by the driver (setVerdict).  Until it arrives a row is `●` observed or `○` waiting,
  /// and 'mark' flags an observation worth a look.
  function lines(header) {
    const out = [
      [{text: 'BackgroundGeolocation api calls', tone: 'plain'}],
      [{text: header, tone: 'dim'}],
      [{text: RULE, tone: 'dim'}],
    ];
    let ok = 0, bad = 0;
    for (let i = 0; i < rows.length; i++) {
      const r = rows[i], good = r.outcome === 'ok';
      good ? ok++ : bad++;
      const v = judged(r.api);
      out.push([glyph(v, true),
                {text: pad(r.api, 26), tone: 'plain'},
                {text: failed(v) && v !== 'fail' ? v : good ? '' : r.outcome, tone: failed(v) ? 'fail' : 'mark'}]);
      // (WO-027, Tier C C1) A hook asserting more than COLLAPSE_AT paths draws one summary row —
      // the host's count once it has judged, what arrived `missing` before — and then only the
      // paths worth a look: failed on the host, or observed missing.  A generated 104-path State
      // check would otherwise bury every other row below the fold of the screenshot.
      // (Tier C C1) A hook the host has judged entirely green is its one row: only what failed,
      // or has not been judged yet, takes screen space.
      if (v === 'ok') continue;
      const many = r.paths.length > COLLAPSE_AT;
      if (many) out.push(tally(r));
      let group = null;
      for (let k = 0; k < r.paths.length; k++) {
        // A path the payload did not have is an OBSERVATION, and the one most worth seeing:
        // `missing` is marked, every other type is secondary detail.
        const entry = r.paths[k], pv = judged(r.api, entry.path);
        if (many && !failed(pv) && !(entry.type === 'missing' && pv !== 'ok')) continue;
        if (entry.path.length <= PATH_W) {
          out.push([{text: '       ' + pad(entry.path, PATH_W), tone: 'dim'},
                    {text: pad(entry.value, 11), tone: 'plain'},
                    typeCell(pv, entry.type)]);
          group = null;
          continue;
        }
        // (Tier C C1) A path longer than its column goes under its group (`geolocation.`), shared
        // by the paths after it in the same group, and its leaf carries the value and the type
        // when they fit the line, else they drop to the next one.  Nothing is cut.
        const cut = entry.path.lastIndexOf('.');
        const prefix = entry.path.slice(entry.path.indexOf('$.') === 0 ? 2 : 0, cut + 1);   // `$err.` keeps its name
        const leaf = entry.path.slice(cut + 1), cell = typeCell(pv, entry.type);
        if (prefix && prefix !== group) out.push([{text: '       ' + prefix, tone: 'dim'}]);
        group = prefix;
        const tail = (entry.value ? entry.value + '  ' : '') + cell.text;
        if (9 + leaf.length + 2 + tail.length <= LINE_W) {
          out.push([{text: '         ' + leaf + '  ', tone: 'dim'},
                    {text: entry.value ? entry.value + '  ' : '', tone: 'plain'}, cell]);
        } else {
          out.push([{text: '         ' + leaf, tone: 'dim'}]);
          out.push([{text: '           ' + pad(entry.value, 12), tone: 'plain'}, cell]);
        }
      }
    }
    out.push([{text: RULE, tone: 'dim'}]);
    out.push([{text: '  ' + ok + ' answered', tone: 'plain'},
              {text: bad ? '  \u00b7  ' + bad + ' rejected or absent' : '', tone: 'mark'}]);
    return out;
  }

  // (WO-027) The HOST's verdict: {rows: {id: v}, paths: {id: {path: v}}, final?, verdict?,
  // metrics?}, v = 'ok' | 'pending' | the failure reason (labkit.verdict_view).  run.py pushes it
  // into the app's storage, the driver reads it back (Lab.verdict()) and hands it here.  The
  // device only draws it: null — nothing pushed yet — leaves every row an observation.
  let verdict = null;

  function setVerdict(v) {
    verdict = v && typeof v === 'object' ? v : null;
  }

  /// A row's verdict (`path` omitted) or one path's: 'ok', 'pending', a reason, or undefined.
  function judged(id, path) {
    if (!verdict) return undefined;
    if (path === undefined) return (verdict.rows || {})[id];
    return ((verdict.paths || {})[id] || {})[path];
  }

  function failed(v) {
    return !!v && v !== 'ok' && v !== 'pending';
  }

  /// ✓ green where the host says the row holds, ✗ red where it failed, else what was observed.
  function glyph(v, observed) {
    if (v === 'ok') return {text: '  \u2713  ', tone: 'pass'};
    if (failed(v)) return {text: '  \u2717  ', tone: 'fail'};
    return observed ? {text: '  \u25CF  ', tone: 'plain'} : {text: '  \u25CB  ', tone: 'dim'};
  }

  /// (Tier C C1) The summary row of a collapsed hook: `N paths`, then the host's `ok` / `✗` /
  /// `pending` counts once it has judged the hook's paths, and what arrived `missing`.
  const COLLAPSE_AT = 8, PATH_W = 24, LINE_W = 47, HOST_LINES = 4;

  /// (Tier C C1) `text` in lines of at most `width`: breaks after `, ` where it can, then after a
  /// `.`, `=` or `-`, hard only as a last resort; past HOST_LINES the last line ends in `…`.
  function wrap(text, width) {
    const out = [];
    let line = '';
    text.split(', ').forEach((item, i, all) => {
      const piece = item + (i < all.length - 1 ? ',' : '');
      if (line && line.length + 1 + piece.length > width) { out.push(line); line = ''; }
      let rest = (line ? line + ' ' : '') + piece;
      while (rest.length > width) {
        // Break after a path's own `.`, `=` or `-` in the line's back half, so a segment stays whole.
        let at = width;
        for (let k = width; k > width / 2; k--) if ('.=-'.indexOf(rest[k - 1]) >= 0) { at = k; break; }
        out.push(rest.slice(0, at));
        rest = rest.slice(at);
      }
      line = rest;
    });
    if (line) out.push(line);
    if (out.length <= HOST_LINES) return out;
    const kept = out.slice(0, HOST_LINES);
    kept[HOST_LINES - 1] = kept[HOST_LINES - 1].slice(0, width - 1) + '\u2026';
    return kept;
  }
  function tally(r) {
    let ok = 0, bad = 0, wait = 0, missing = 0;
    r.paths.forEach((e) => {
      const v = judged(r.api, e.path);
      if (v === 'ok') ok++;                     // an `absent: true` path is missing AND ok: ok
      else if (e.type === 'missing') missing++; // each path counted once: missing is its own ✗
      else if (failed(v)) bad++;
      else if (v === 'pending') wait++;
    });
    const row = [{text: '       ' + pad(r.paths.length + ' paths', 24), tone: 'dim'}];
    if (ok) row.push({text: ok + ' ok  ', tone: 'pass'});
    if (bad) row.push({text: bad + ' \u2717  ', tone: 'fail'});
    if (wait) row.push({text: wait + ' pending  ', tone: 'dim'});
    if (missing) row.push({text: missing + ' missing', tone: 'mark'});
    return row;
  }

  /// A path's type column: green when it held, the host's reason in red when it did not.
  function typeCell(v, type) {
    if (v === 'ok') return {text: type, tone: 'pass'};
    if (failed(v)) return {text: v.length > 26 ? v.slice(0, 25) + '\u2026' : v, tone: 'fail'};
    return {text: type, tone: type === 'missing' ? 'mark' : 'dim'};
  }

  /// The host line under every view: scoring while nothing has been pushed, the live tally while
  /// the step runs, and once it is scored the verdict summary.txt records, with each failed metric.
  function host() {
    const label = {text: '  host   ', tone: 'dim'};
    if (!verdict) return [[label, {text: 'scoring\u2026', tone: 'dim'}]];
    if (verdict.final) {
      const v = verdict.verdict, broken = (verdict.metrics || []).filter((m) => !m[3]);
      // A must-fail (mutation) variant PASSES by breaking something: say so, and draw what it
      // broke as the expected outcome it is, not as a failure.
      const note = !verdict.inverted ? '' : v === 'PASS' ? '   must fail: it broke ' + broken.length
                                                         : '   must fail: nothing broke';
      const out = [[label, {text: (v || 'no expectations') + note,
                            tone: v === 'PASS' ? 'pass' : v ? 'fail' : 'dim'}]];
      if (verdict.contaminated) {
        out.push([{text: '     contaminated by ' + verdict.contaminated.join(', '), tone: 'fail'}]);
      }
      broken.forEach((m) => {
        // (Tier C C1) A reason that fits stays on the metric's line; a longer one WRAPS under it —
        // at its `, ` separators, then hard — up to HOST_LINES lines, never cut mid-screen.
        const actual = String(m[1]), tone = verdict.inverted ? 'mark' : 'fail';
        const head = '     \u2717 ' + pad(m[0], 16) + ' ';   // a long name still gets its gap
        if (head.length + actual.length <= LINE_W) {
          out.push([{text: head, tone: tone}, {text: actual, tone: tone}]);
          return;
        }
        out.push([{text: '     \u2717 ' + m[0], tone: tone}]);
        wrap(actual, LINE_W - 7).forEach((part) => out.push([{text: '       ' + part, tone: tone}]));
      });
      return out;
    }
    let ok = 0, bad = 0, waiting = 0;
    Object.keys(verdict.rows || {}).forEach((k) => {
      const v = verdict.rows[k];
      v === 'ok' ? ok++ : v === 'pending' ? waiting++ : bad++;
    });
    return [[label, {text: '\u2713 ' + ok, tone: ok ? 'pass' : 'dim'},
             {text: '   \u2717 ' + bad, tone: bad ? 'fail' : 'dim'},
             {text: '   \u00b7  ' + waiting + ' pending', tone: 'dim'}]];
  }

  /// (WO-027) The call schedule, drawn under every view's header: each api hook, `○` while it
  /// waits (with its slot when deferred, `+2s` — seconds after the first batch completes), `●`
  /// once it has run, and the host's `✓`/`✗` once judged.  A watcher could not tell "waiting for
  /// its slot" from "stuck" — events-geofence showed `onGeofence` silent for 8 s with nothing on
  /// screen saying addGeofence was still scheduled.
  /// On the events and title views the strip is the only place a hook's verdict appears.
  function schedule(hooks) {
    const done = {};
    for (let i = 0; i < rows.length; i++) done[rows[i].api] = true;
    const items = [];
    (hooks || []).forEach((h, i) => {
      if (!h || typeof h.api !== 'string') return;
      const ran = !!done[h.api + '#' + i], v = judged(h.api + '#' + i);
      // Once the host has judged a call it takes the verdict's glyph — the host line's tally
      // counts these rows, and on the events view the strip is the only place they appear.
      const g = v === 'ok' ? '\u2713 ' : failed(v) ? '\u2717 ' : ran ? '\u25CF ' : '\u25CB ';
      items.push({text: g + h.api + (h.at_s !== undefined ? ' +' + h.at_s + 's' : ''),
                  tone: v === 'ok' ? 'pass' : failed(v) ? 'fail' : ran ? 'plain' : 'dim'});
    });
    if (!items.length) return [];
    const out = [];
    let line = [{text: '  calls  ', tone: 'dim'}], width = 9;
    items.forEach((it) => {
      if (width + it.text.length + 3 > 56 && line.length > 1) {
        out.push(line);
        line = [{text: '         ', tone: 'dim'}];
        width = 9;
      }
      line.push({text: it.text + '   ', tone: it.tone});
      width += it.text.length + 3;
    });
    out.push(line);
    return out;
  }

  /// A view with the schedule and the host line inserted under its title and header — near the
  /// TOP: an api view of 14 hooks is taller than the screen, and the screenshot must carry the
  /// verdict (the HTML drivers never scroll).
  function frame(viewLines, hooks) {
    return viewLines.slice(0, 2).concat(schedule(hooks), host(), viewLines.slice(2));
  }

  /// The same rows as plain text, for a driver that cannot colour (and for tests).
  function view(header) {
    return lines(header).map((segs) => segs.map((s) => s.text).join('')).join('\n');
  }

  // (Tier C, R3) A member by its name: `getState` on the facade, `logger.getLog` on the facade's `logger`.  The
  // caller gets the OWNER too, and calls the member on it: React Native's deviceSettings is an instance whose
  // methods read `this`, and Capacitor's is a class of statics.  An owner that is not there leaves the member
  // undefined, which every caller already reports `absent`.
  function resolve(BG, name) {
    const segs = String(name).split('.');
    let owner = BG;
    for (let i = 0; i < segs.length - 1; i++) {
      owner = (owner === null || owner === undefined) ? undefined : owner[segs[i]];
    }
    const there = owner !== null && owner !== undefined;
    return {owner: owner, member: there ? owner[segs[segs.length - 1]] : undefined};
  }

  // Awaits the hooks due in this slot, in list order.  The id is <api>#<index> over the FULL
  // hook list, legacy hooks included — the host derives the same ids from run.json — so the
  // filter below SKIPS a hook that is not due and never re-indexes.
  //
  // `slot` is 'first' or a number (WO-027).  'first' is the first batch: every hook with no
  // `at_s`, queued by the driver at the end of main(), right after start().  A number is a slot:
  // the hooks whose `at_s` is that value, run that many seconds after the first batch COMPLETES
  // (startSlots).  A batch is awaited end to end with no pause in it, so "call, wait, call again"
  // needs a slot: measured on iOS, a stop() issued 13 ms after start() cancelled the location
  // request start()'s motionchange was waiting on, and the event never arrived.  The origin is
  // the batch's END, not its start or the launch: api-smoke's first batch takes 4.1 s on Android,
  // and a deferred hook must not be eaten by it.
  //
  // The slot is compared with `===` against the hooks' OWN values (slotTimes hands them back
  // unchanged), so nothing is ever rounded into the wrong slot, and 'first' can never equal a
  // number.  The 4 s tick runs no batch any more: `at:` counted ticks, so a deferred call came in
  // 4 s steps from the tick's origin rather than from the batch it follows.  The tick still fires
  // legacy `call` hooks (the driver's runHooks(n)); this never does.
  //
  // `slot` is PASSED IN, by main() or by the slot's own timer, never read from the driver after
  // an await (the hazard runHooks() had with its tick counter).  `ranSoFar` accumulates across slots so
  // `api-done n=` always names the total so far — the host reads the LAST one and compares it
  // with the number of hooks the scenario wrote.
  async function runBatch(BG, hooks, log, slot, ranSoFar) {
    let n = ranSoFar || 0;
    let ran = 0;
    const due = (hook) => (typeof slot === 'number' ? hook.at_s === slot
                                                    : slot === 'first' && hook.at_s === undefined);
    for (let i = 0; i < hooks.length; i++) {
      const hook = hooks[i];
      if (!hook || typeof hook.api !== 'string' || !due(hook)) continue;
      n++;
      ran++;
      const id = hook.api + '#' + i;
      const paths = hook.paths || [];
      // Reflective on purpose: there is no table to forget, so absence is observed, not declared.
      const at = resolve(BG, hook.api), member = at.member;
      const row = {api: id, outcome: 'ok', paths: []};
      rows.push(row);
      const record = (value) => paths.forEach((p) => {
        const r = walk(value, p);
        row.paths.push({path: p, type: r.type, value: shown(r)});
      });
      if (hook.probe) {
        if (member === undefined) { row.outcome = 'absent'; log('api id=' + id + ' outcome=absent'); continue; }
        log('api id=' + id + ' outcome=ok');
        paths.forEach((p) => ret(log, id, p, member));
        record(member);
        continue;
      }
      if (typeof member !== 'function') { row.outcome = 'absent'; log('api id=' + id + ' outcome=absent'); continue; }
      let outcome = 'ok', value, beside = null, pending;
      // (WO-107) A `generate:` hook's argument is built here, BEFORE t0: the clock times the bridge, never the
      // lab building its argument.  An `alongside:` call is issued right after this one is, before it settles.
      const arg = hook.generate ? geofencesFrom(hook.generate) : hook.args;
      const t0 = Date.now();
      try {
        // NEVER member(hook.args): Cordova's public wrapper sniffs arguments.length — getCount(undefined)
        // is length 1, takes the callback branch, and returns undefined over a discarded result.
        // (Tier C, R5) The call alone, apart from its settling: a member that throws HERE (a TurboModule
        // argument error does) handed back no promise, so a caller's `.catch()` would never see it.  That is
        // `threw`; `rejected` is a promise that settled so.  Called on its owner (R3).
        pending = (hook.generate || ('args' in hook)) ? member.call(at.owner, arg) : member.call(at.owner);
      } catch (e) {
        outcome = 'threw';
        value = e;
      }
      if (outcome === 'ok') {
        if (typeof hook.alongside === 'string') beside = callBeside(BG, hook.alongside, id, t0, log);
        try {
          value = await pending;
        } catch (e) {
          outcome = 'rejected';
          value = e;
        }
      }
      if (beside) await beside;
      log('api id=' + id + ' outcome=' + outcome);
      row.outcome = outcome;
      for (const p of paths) {
        // The error side is the thrown value as much as the rejection's.
        const wantsError = p === '$err' || p.indexOf('$err.') === 0;
        if (wantsError === (outcome !== 'ok')) ret(log, id, p, value);
      }
      record(value);
    }
    if (ran) log('api-done n=' + n);
    return n;
  }

  // (WO-107) `generate: {geofences, vertices, seed}`: that many polygons of `vertices` points, from a seeded generator
  // (mulberry32) so every bridge gets the same set, and the Dart port's _generated the same numbers.  Each is a ring
  // ~150-300 m across, the centres spread over half a degree; identifiers `wo107-<i>`.  The documented polygon form —
  // no latitude, longitude or radius — so the bridge computes each enclosing circle, as RN #2668's app's did (the
  // types still declare those three required: C4 decision 16).
  function geofencesFrom(spec) {
    let s = spec.seed >>> 0;
    const rand = () => {
      s = (s + 0x6D2B79F5) >>> 0;
      let t = s;
      t = Math.imul(t ^ (t >>> 15), t | 1);
      t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
      return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
    };
    const fences = [];
    for (let i = 0; i < spec.geofences; i++) {
      const lat = 45.5 + rand() * 0.5, lng = -73.6 + rand() * 0.5, r = 0.0015 + rand() * 0.0015;
      const vertices = [];
      for (let v = 0; v < spec.vertices; v++) {
        const a = 2 * Math.PI * v / spec.vertices;
        vertices.push([lat + r * Math.sin(a), lng + r * Math.cos(a)]);
      }
      fences.push({identifier: 'wo107-' + i, notifyOnEntry: true, notifyOnExit: true, vertices: vertices});
    }
    return fences;
  }

  // (WO-107) `alongside: <member>`: a zero-argument call issued beside the hook's, logged when it settles with the ms
  // since the hook's call was issued.  A bridge that holds its other calls while one runs — a shared plugin thread, a
  // busy main thread, a synchronous exec that returns only when the work is done — shows it here, where the awaited
  // hook chain cannot.
  function callBeside(BG, name, id, t0, log) {
    const settle = (outcome) => log('alongside id=' + id + ' api=' + name + ' outcome=' + outcome + ' ms=' + (Date.now() - t0));
    const at = resolve(BG, name), other = at.member;
    if (typeof other !== 'function') { settle('absent'); return null; }
    let pending;
    try {
      pending = other.call(at.owner);
    } catch (e) {
      settle('rejected');
      return null;
    }
    return Promise.resolve(pending).then(() => settle('ok'), () => settle('rejected'));
  }

  // (WO-027) The slots a step declares: every distinct `at_s` over its api hooks and its event
  // declarations, ascending.  One timer per value, so two hooks at `at_s: 2` share a slot and run
  // in list order, and 2 and 2.5 are two slots.  The values are returned as written, never
  // computed: runBatch and LabEvents.arm find their hooks with `===`.
  // (Tier C, R2) A declaration's `remove_at_s` is a slot too: LabEvents.remove runs in it.
  function slotTimes(hooks, events) {
    const times = [];
    const add = (v) => { if (typeof v === 'number' && isFinite(v) && times.indexOf(v) < 0) times.push(v); };
    (hooks || []).forEach((h) => { if (h && typeof h.api === 'string') add(h.at_s); });
    (events || []).forEach((d) => { if (d && typeof d.on === 'string') { add(d.at_s); add(d.remove_at_s); } });
    return times.sort((a, b) => a - b);
  }

  // (WO-027) The per-tick getCurrentPosition is DERIVED, never configured: on for a legacy
  // (window) step, whose foregroundLocations, duplicateLocations and httpFlushes feed on its
  // deliveries; off wherever the step declares an api hook or an event, because there it is an
  // undeclared cause (the Android geofence ENTER, every FGS start after the evidence).  The host
  // computes the same rule (labkit.tick_gcp) and checks it against the anchor line.
  function tickGcpFor(hooks, events) {
    return !((hooks || []).some((h) => h && typeof h.api === 'string') || (events || []).length);
  }

  // (WO-027) The line the host reads a step's schedule from: how long the first batch took, the
  // slots that follow it, and the per-tick getCurrentPosition as the driver resolved it.
  // Logged even when the batch ran nothing — on a legacy step it still names the driver that ran.
  // (Tier C, C7.6) The boot fix is gone, and its `bootGcp=` field with it; labkit still reads the
  // field in older captures.
  function anchorLine(batchMs, times, tickGcp) {
    return 'anchor batchMs=' + Math.round(batchMs) + ' slots=' + (times && times.length ? times.join(',') : '-') +
           ' tickGcp=' + (tickGcp ? 'on' : 'off');
  }

  // (WO-027) One timer per slot, every one from the SAME origin: the moment the first batch
  // completed.  onSlot(s, due) gets its due time so the driver can log `late_ms` — a slot queued
  // behind a long batch starts late and says so, rather than pushing the slots after it.  `now`
  // and `setTimer` are for the selftest (labs/harness/slots_selftest.js); a driver passes neither.
  function startSlots(times, onSlot, now, setTimer) {
    const t0 = (now || Date.now)();
    const later = setTimer || setTimeout;
    (times || []).forEach((s) => later(() => onSlot(s, t0 + s * 1000), s * 1000));
  }

  // `ret` is exported for labevents.js: an event payload is reported through exactly the same
  // line grammar as a return value, so the host reads both with one parser.
  return {runBatch: runBatch, walk: walk, typeOf: typeOf, ret: ret, view: view, lines: lines, rows: rows,
          schedule: schedule, frame: frame, setVerdict: setVerdict, judged: judged, glyph: glyph,
          typeCell: typeCell, failed: failed, slotTimes: slotTimes, tickGcpFor: tickGcpFor,
          anchorLine: anchorLine, startSlots: startSlots};
});
