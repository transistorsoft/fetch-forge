const path = require('path');
const {getDefaultConfig, mergeConfig} = require('@react-native/metro-config');

// The plugin under test is a file: dependency outside this project (build.sh's staged copy of the
// checkout, labs/platforms/react-native/.stage), so Metro needs it in watchFolders, and every module
// IT imports must resolve back to this app's node_modules — otherwise `react-native` is
// unresolvable from the plugin's sources.  (bg-forge labs/platforms/react-native, WO-026.)
const plugin = path.resolve(
  require.resolve('react-native-background-fetch/package.json'),
  '..',
);
// labapi.js / labevents.js / logo.js are shared by every JavaScript driver (labs/platforms/_shared).
const shared = path.resolve(__dirname, '..', '..', '_shared');
const esc = p => p.replace(/[/\\]/g, '[\\/\\\\]');

module.exports = mergeConfig(getDefaultConfig(__dirname), {
  watchFolders: [plugin, shared],
  resolver: {
    extraNodeModules: new Proxy(
      {},
      {get: (_, name) => path.resolve(__dirname, 'node_modules', String(name))},
    ),
    // Never crawl a nested react-native copy the plugin might carry.
    blockList: [new RegExp(`${esc(plugin)}${esc('/node_modules/react-native/')}.+`)],
    unstable_enableSymlinks: true,
  },
});
