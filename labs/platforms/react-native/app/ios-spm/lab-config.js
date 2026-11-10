#!/usr/bin/env node
// (WO-002 phase 2b) React Native's SwiftPM setup asks the CLI's `config` where the iOS project is
// (project.ios.sourceDir) and writes autolinking.json under it. The CLI always answers `ios/`, the CocoaPods host,
// so this prints the same config with the SwiftPM host, `ios-spm/`, in its place. `setup-apple-spm.js add
// --config-command '["node","ios-spm/lab-config.js"]'` remembers the command in .spm-injected.json, so later runs
// and the sync build phase use it too. Run with the app as cwd (the tool runs it from the project root).
const {execFileSync} = require('child_process');
const path = require('path');

const appRoot = path.resolve(__dirname, '..');
const cli = require.resolve('@react-native-community/cli/build/bin.js', {paths: [appRoot]});
const config = JSON.parse(execFileSync(process.execPath, [cli, 'config'], {cwd: appRoot, maxBuffer: 64 << 20}));
if (!config.project || !config.project.ios) {
  console.error('lab-config.js: the CLI config has no project.ios');
  process.exit(1);
}
config.project.ios.sourceDir = __dirname;
process.stdout.write(JSON.stringify(config));
