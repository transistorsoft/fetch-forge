// `events` declarations — the conformance batch's sibling for the 15 `on*` members
// (WO-027, labs/README.md "Event assertions").
//
// One source for every JavaScript driver, like labapi.js; the Dart driver carries a port.
//
// The driver is told WHICH member to subscribe to, under which id, and WHICH paths to report
// from the first delivery.  It is never told how many deliveries are expected, and never what a
// payload should contain: `min` and every comparator are evaluated on the host (labkit's
// eventAssertions), because a comparator compiled into four drivers is four places a wrong
// result can pass itself.
//
// Subscriptions are armed at boot, BEFORE the first api batch runs — the batch is what CAUSES
// the events (ready/start/changePace/stop), so the ordering the metric needs already exists.
// A declaration with `at_s` arms later, in its slot (arm() below).
(function (root, factory) {
  if (typeof module === 'object' && module.exports) module.exports = factory();
  else root.LabEvents = factory();
})(typeof self !== 'undefined' ? self : this, function () {

  // What the screen shows (WO-027).  OBSERVATION ONLY: the rows say what arrived and what its
  // type was, never whether it was expected — `min` and every comparator live on the host, and a
  // driver that rendered a verdict would be a second place a wrong result could pass itself.
  const rows = [];
  const RULE = new Array(48).join('\u2500');

  // A value has one column; an ISO timestamp is twice as wide as it, so it is clipped the way
  // the host clips a failure message rather than being allowed to push the type column over.
  function clip(r) {
    if (!r || r.type === 'missing' || typeof r.value === 'object' || r.value === undefined) return '';
    const text = String(r.value);
    return text.length > 10 ? text.slice(0, 9) + '\u2026' : text;
  }

  function pad(s, n) {
    s = String(s);
    while (s.length < n) s += ' ';
    return s;
  }

  /// The rows as STRUCTURE — {text, tone} segments per line — so each driver can colour them the
  /// way its platform allows.
  /// Green and red are the HOST'S verdict (WO-027), never the device's: `api` is LabApi, whose
  /// judged/glyph/typeCell draw what run.py pushed.  Until it arrives a row is `●` delivered or
  /// `○` waiting — the device knows what ARRIVED, never whether it was the expected thing.
  // Without LabApi (the plain-text view below) every row is an observation, as before any verdict.
  const OBSERVED = {judged: () => undefined, failed: () => false,
                    glyph: (v, seen) => (seen ? {text: '  \u25CF  ', tone: 'plain'} : {text: '  \u25CB  ', tone: 'dim'}),
                    typeCell: (v, type) => ({text: type, tone: type === 'missing' ? 'mark' : 'dim'})};

  function lines(header, api) {
    api = api || OBSERVED;
    const out = [
      [{text: 'BackgroundGeolocation `on*` events', tone: 'plain'}],
      [{text: header, tone: 'dim'}],
      [{text: RULE, tone: 'dim'}],
    ];
    let fired = 0, quiet = 0, deliveries = 0;
    for (let i = 0; i < rows.length; i++) {
      const r = rows[i];
      deliveries += r.n;
      const good = r.outcome === 'ok' && r.n > 0;
      good ? fired++ : quiet++;
      const state = r.outcome !== 'ok' ? r.outcome
                  : r.n === 0 ? 'waiting'
                  : r.n + (r.n === 1 ? ' delivery' : ' deliveries');
      const v = api.judged(r.id);
      out.push([api.glyph(v, r.n > 0 || r.outcome !== 'ok'),
                {text: pad(r.on, 26), tone: 'plain'},
                // A row-level failure (never-fired, fired-1/2) is the host's reason; 'fail' means a path.
                {text: api.failed(v) && v !== 'fail' ? v : state,
                 tone: api.failed(v) ? 'fail' : r.outcome !== 'ok' ? 'mark' : 'dim'}]);
      for (let k = 0; k < r.seen.length; k++) {
        // A path the payload did not have is an OBSERVATION, and the one most worth seeing:
        // `missing` is marked, every other type is secondary detail.
        const entry = r.seen[k];
        out.push([{text: '       ' + pad(entry.path, 24), tone: 'dim'},
                  {text: pad(entry.value, 11), tone: 'plain'},
                  api.typeCell(api.judged(r.id, entry.path), entry.type)]);
      }
    }
    out.push([{text: RULE, tone: 'dim'}]);
    out.push([{text: '  ' + fired + ' delivered', tone: 'plain'},
              {text: quiet ? '  \u00b7  ' + quiet + ' waiting' : '', tone: 'dim'},
              {text: '  \u00b7  ' + deliveries + (deliveries === 1 ? ' delivery' : ' deliveries'), tone: 'dim'}]);
    return out;
  }

  /// The same rows as plain text, for a driver that cannot colour (and for tests).
  function viewText(header, api) {
    return lines(header, api).map((segs) => segs.map((s) => s.text).join('')).join('\n');
  }

  /// The monospace block a human reads in the screenshot: one line per declaration, then one
  /// indented line per reported path with the value and the type as the driver saw them.
  function view(header) {
    const out = ['BackgroundGeolocation `on*` events', header, new Array(56).join('-')];
    let fired = 0, deliveries = 0;
    for (let i = 0; i < rows.length; i++) {
      const r = rows[i];
      deliveries += r.n;
      if (r.n) fired++;
      const state = r.outcome !== 'ok' ? '\u25CF ' + r.outcome
                  : r.n === 0 ? '\u25CB waiting'
                  : '\u25CF ' + r.n + (r.n === 1 ? ' delivery' : ' deliveries');
      out.push(pad(r.on, 29) + state);
      for (let k = 0; k < r.seen.length; k++) {
        out.push('  ' + pad(r.seen[k].path, 27) + pad(r.seen[k].value, 11) + r.seen[k].type);
      }
    }
    out.push(new Array(56).join('-'));
    out.push(rows.length + ' armed \u00b7 ' + fired + ' delivered \u00b7 ' + deliveries +
             (deliveries === 1 ? ' delivery' : ' deliveries'));
    return out.join('\n');
  }

  // (Tier C, R1) What each armed declaration holds, by id: what its member handed back — a Subscription, or a
  // thenable of one (React Native's watchPosition is async) — and its delivery count, for remove() below.
  const held = {};

  function errorPath(p) {
    return p === '$err' || p.indexOf('$err.') === 0;
  }

  // `ret` comes from labapi.js: one line grammar for a returned value and for a delivered
  // payload, so the host parses both with the same reader.  `walk` is the same reader, used here
  // to put the value on screen — the line the host scores is still written by `ret`.
  // `slot` is 'boot' or a number (WO-027), the same slots as labapi.js's runBatch.  A
  // declaration with no `at_s` arms at BOOT ('boot'), before the api batch that causes it;
  // `at_s: 2` arms in slot 2 — 2 s after the first api batch completes, after that slot's api
  // hooks — which is what a member like watchPosition needs: it cannot be called before ready()
  // has configured the SDK.  The slot is the declaration's own value, compared with `===`, and
  // 'boot' is a string so it can never equal one.  `armedSoFar` accumulates so
  // `events-armed n=` names the total, as `api-done n=` does.
  function arm(BG, events, log, ret, walk, render, slot, armedSoFar) {
    let n = armedSoFar || 0;
    const due = (decl) => (typeof slot === 'number' ? decl.at_s === slot
                                                    : slot === 'boot' && decl.at_s === undefined);
    let armed = 0;
    for (let i = 0; i < (events || []).length; i++) {
      const decl = events[i];
      if (!decl || typeof decl.on !== 'string' || !due(decl)) continue;
      n++;
      armed++;
      // Reflective, like labapi.js: there is no table to forget, so an absent member is
      // OBSERVED rather than declared.
      const member = BG[decl.on];
      const row = {on: decl.on, id: decl.id, outcome: 'ok', n: 0, seen: []};   // id: what the host judges
      rows.push(row);
      if (typeof member !== 'function') {
        row.outcome = 'absent';
        log('event-arm id=' + decl.id + ' outcome=absent');
        continue;
      }
      let seen = 0, errors = 0;
      const paths = decl.paths || [];
      // (Tier C, R4) `delivery: N`: the paths are reported for deliveries 1..N, each line naming its own
      // (`n=`), and the host reads the Nth.  Without it: the first delivery only, and no `n=`, as before.
      const upTo = decl.delivery === undefined ? 1 : decl.delivery;
      try {
        const delivered = function (payload) {
          seen++;
          // One short line per delivery: the count is the metric's `min` evidence and the
          // timestamp is the only latency record a capture keeps.
          log('event id=' + decl.id + ' n=' + seen);
          // Paths from the FIRST delivery only (or up to the one asked for): the host keeps one ret
          // line per (id, path) anyway, and an event can fire 14 times in a 30 s step (measured on iOS).
          row.n = seen;
          if (seen <= upTo) {
            for (let k = 0; k < paths.length; k++) {
              if (errorPath(paths[k])) continue;     // the failure callback's (below)
              ret(log, decl.id, paths[k], payload, decl.delivery === undefined ? undefined : seen);
              if (seen !== upTo) continue;           // the screen shows the delivery the host reads
              // The same walk the reported line is built from, kept for the screen.
              const r = walk ? walk(payload, paths[k]) : null;
              row.seen.push({path: paths[k], type: r ? r.type : '?',
                             value: clip(r)});
            }
          }
          if (render) render(view(render.header || ''));
        };
        // (Tier C, R6) `errors: true`: the member's failure callback is passed too, after the success one.  Each
        // error is counted on its own line, and the first reports the declaration's `$err` paths.
        const failed = function (error) {
          errors++;
          log('event-error id=' + decl.id + ' n=' + errors);
          if (errors !== 1) return;
          for (let k = 0; k < paths.length; k++) {
            if (errorPath(paths[k])) ret(log, decl.id, paths[k], error);
          }
        };
        // NEVER member(cb) — called on BG for the same reason labapi.js calls it that way: a bridge whose
        // public wrapper reads `this` must get the facade it expects.  `args` is what the member takes
        // BEFORE its callback (watchPosition's options).  The same rule as labapi.js: no `args` key means
        // the callbacks are the only arguments.
        const given = ('args' in decl ? [decl.args] : [])
          .concat(decl.errors === true ? [delivered, failed] : [delivered]);
        // (Tier C, R1) Kept, not discarded: `remove_at_s` removes it.  A thenable that rejects is remove()'s
        // to report (`threw`); the handler here only keeps it from being an unhandled rejection until then.
        const subscription = member.apply(BG, given);
        if (subscription && typeof subscription.then === 'function') subscription.then(null, function () {});
        held[decl.id] = {subscription: subscription, seen: function () { return seen; }};
        log('event-arm id=' + decl.id + ' outcome=ok');
      } catch (e) {
        row.outcome = 'threw';
        // Subscribing is not supposed to throw; one bridge does (React Native's
        // onNotificationAction reads an Event enum value that does not exist).  Report it as the
        // arm outcome rather than letting it take the rest of the list with it.
        log('event-arm id=' + decl.id + ' outcome=threw');
      }
    }
    if (armed) {
      log('events-armed n=' + n);
      if (render) render(view(render.header || ''));
    }
    return n;
  }

  // (Tier C, R1) The removals due in one slot: every declaration whose `remove_at_s` is this slot has the
  // Subscription it armed removed, and says how that went with the deliveries it had seen.  What the member
  // handed back is awaited first, so a thenable of a Subscription is removed like a plain one.  `absent`: there
  // is nothing to call — the declaration never armed, or what came back has no remove().  One at a time, in
  // list order; the caller awaits it on its batch chain, after the slot's calls and arms.
  async function remove(events, log, slot) {
    for (let i = 0; i < (events || []).length; i++) {
      const decl = events[i];
      if (!decl || typeof decl.on !== 'string' || typeof slot !== 'number' || decl.remove_at_s !== slot) continue;
      const mine = held[decl.id];
      let outcome = 'absent';
      try {
        const subscription = mine ? await mine.subscription : null;
        if (subscription && typeof subscription.remove === 'function') {
          await subscription.remove();
          outcome = 'ok';
        }
      } catch (e) {
        outcome = 'threw';
      }
      log('event-removed id=' + decl.id + ' n=' + (mine ? mine.seen() : 0) + ' outcome=' + outcome);
    }
  }

  return {arm: arm, remove: remove, view: viewText, lines: lines, rows: rows};
});
