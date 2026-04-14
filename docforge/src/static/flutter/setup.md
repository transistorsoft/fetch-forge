# Flutter — Setup

[![](https://img.shields.io/pub/v/background_fetch?style=flat-square)](https://pub.dev/packages/background_fetch)

## Installation

```bash
flutter pub add background_fetch
```


## iOS Setup

### CocoaPods

If you're **not** using Swift Package Manager, adjust `use_frameworks!` in your **`ios/Podfile`**:

```ruby
target 'Runner' do
  use_frameworks! :linkage => :static   # <-- append :linkage => :static
end
```

!!! warning
    Without `:linkage => :static`, you'll get a build error about statically linked binaries in the `TSBackgroundFetch.xcframework`.

### Background Modes

{{> ios-background-modes.md}}

### Info.plist

{{> ios-info-plist.md}}


## Android Setup

No additional Android setup is required for `background_fetch >= 1.5.0`.

!!! note "Upgrading from < 1.5.0?"
    If you previously added a custom `maven` URL for `background_fetch` in your **`android/build.gradle`**, you can now remove it.

