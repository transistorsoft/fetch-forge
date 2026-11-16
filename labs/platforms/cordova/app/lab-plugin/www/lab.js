// (WO-002) window.Lab: the driver's channel to the host, the Capacitor fetch host's `Lab` plugin on Cordova's exec.
// args/knobs/probe/verdict resolve the Capacitor shapes (probe: {probe}, verdict: {value}).  log sends a driver line to
// the host, which logs it VERBATIM under the tag LAB (Cordova 15 does not intercept console messages): it passes no
// callbacks, so no reply comes back into the page for a line.
var exec = require('cordova/exec');

function call(action) {
    return new Promise(function (resolve, reject) { exec(resolve, reject, 'Lab', action, []); });
}

module.exports = {
    args: function () { return call('args'); },
    knobs: function () { return call('knobs'); },
    probe: function () { return call('probe'); },
    verdict: function () { return call('verdict'); },
    log: function (line) { exec(null, null, 'Lab', 'log', [String(line)]); }
};
