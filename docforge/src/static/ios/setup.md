# Setup

## Installation

### Swift Package Manager (recommended)

In Xcode, choose **File ▸ Add Package Dependencies…** and add:

```
https://github.com/transistorsoft/transistor-background-fetch
```

Add the **BackgroundFetch** library product to your target. A single `import BackgroundFetch`
then exposes the `BackgroundFetch.shared` Swift API used throughout these docs.

### CocoaPods

```ruby
pod 'TSBackgroundFetch'
```

!!! note
    The CocoaPods spec currently vendors the Objective-C `TSBackgroundFetch` framework only.
    To use the `BackgroundFetch.shared` Swift API shown in these docs, install via **Swift
    Package Manager**.

## Background Modes

{{> ios-background-modes.md}}

## Info.plist

{{> ios-info-plist.md}}
