# Cordova — Setup

[![](https://img.shields.io/npm/v/cordova-plugin-background-fetch?style=flat-square)](https://www.npmjs.com/package/cordova-plugin-background-fetch)

## Installation

=== "Ionic"

    ```bash
    ionic cordova plugin add cordova-plugin-background-fetch
    ```

=== "Cordova"

    ```bash
    cordova plugin add cordova-plugin-background-fetch
    ```


## iOS Setup

### Background Modes

{{> ios-background-modes.md}}

### Info.plist

If you intend to execute custom tasks via `BackgroundFetch.scheduleTask`, you must register their identifiers in your **`config.xml`** within the `<platform name="ios">` block:

```xml
<platform name="ios">
    <config-file parent="BGTaskSchedulerPermittedIdentifiers"
                 target="*-Info.plist">
        <array>
            <string>com.transistorsoft.customtask</string>
        </array>
    </config-file>
</platform>
```

!!! warning
    A task identifier can be any string you wish, but it **must** be prefixed with `com.transistorsoft.`.

!!! note
    The base identifier `com.transistorsoft.fetch` is registered automatically by the plugin — you do **not** need to add it to `config.xml`.


