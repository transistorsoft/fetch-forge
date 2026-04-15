# Setup

=== "React Native"

    [![](https://img.shields.io/npm/v/react-native-background-fetch?style=flat-square)](https://www.npmjs.com/package/react-native-background-fetch)

    ### Installation

    === "npm"

        ```bash
        npm install react-native-background-fetch
        ```

    === "yarn"

        ```bash
        yarn add react-native-background-fetch
        ```

    ### iOS Setup

    #### CocoaPods

    ```bash
    cd ios && pod install
    ```

    #### Background Modes

    {{> ios-background-modes.md}}

    #### Info.plist

    {{> ios-info-plist.md}}

    ### Android Setup

    !!! success "No Gradle configuration required"
        The native `tsbackgroundfetch` library is hosted on **Maven Central** and is resolved automatically.  No custom `maven` URL or local `libs` repository is needed.

=== "Expo"

    [![](https://img.shields.io/npm/v/react-native-background-fetch?style=flat-square)](https://www.npmjs.com/package/react-native-background-fetch)

    ### Installation

    ```bash
    npx expo install react-native-background-fetch
    ```

    The plugin ships with an [Expo Config Plugin](https://docs.expo.dev/guides/config-plugins/) — it handles all native configuration automatically during `expo prebuild`.  No manual edits to `Info.plist`, `AppDelegate`, or Background Modes are required.

    ### `app.json`

    Add the plugin to **`plugins`** and configure **`ios.infoPlist`** with the required background modes and task identifiers:

    ```json hl_lines="4 7-14"
    {
      "expo": {
        "plugins": [
          "react-native-background-fetch"
        ],
        "ios": {
          "infoPlist": {
            "UIBackgroundModes": [
              "fetch",
              "processing"
            ],
            "BGTaskSchedulerPermittedIdentifiers": [
              "com.transistorsoft.fetch"
            ]
          }
        }
      }
    }
    ```

    If you intend to execute your own custom tasks via **`BackgroundFetch.scheduleTask`**, you must add those custom identifiers as well to **`BGTaskSchedulerPermittedIdentifiers`**.  For example, if you intend to execute a custom **`taskId: 'com.transistorsoft.customtask'`**, add the identifier **`com.transistorsoft.customtask`**:

    ```json hl_lines="3"
    "BGTaskSchedulerPermittedIdentifiers": [
      "com.transistorsoft.fetch",
      "com.transistorsoft.customtask"
    ]
    ```

    !!! warning
        A task identifier can be any string you wish, but it **must** be prefixed with `com.transistorsoft.`.

    ### Prebuild

    You must rebuild for the added plugins to be evaluated.

    If you're developing locally:

    ```bash
    npx expo prebuild
    ```

    If you're using **Expo EAS**:

    ```bash
    eas build --profile development --platform android
    ```

