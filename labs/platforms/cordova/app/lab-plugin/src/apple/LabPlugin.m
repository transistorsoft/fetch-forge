// (WO-002, iOS) iOS half of the fetch lab's `Lab` Cordova plugin (window.Lab): the driver's channel to the host.  The
// Android half is src/android/LabPlugin.java, which reads the launch Intent's extras; here the scenario args arrive as
// process argv (`xcrun simctl launch <dev> <bundle> --key=value …`).  Same keys, same TYPES (the driver tests `== true`
// and reads numbers), same reply shapes (probe: {probe}, verdict: {value}), so app/www/index.html is one file for both
// OSes.  The Capacitor fetch host's LabPlugin (labs/platforms/capacitor/app/native-ios/AppDelegate.swift) in Objective-C.
//
// Under src/apple, never a directory named `ios`: run.py's source stamp skips every one, so a tracked file there would
// be in no stamp.  The SDK is reached by name (the Objective-C runtime), never imported: this file compiles into the App
// target, while the SDK comes into the app through the plugin's Swift package.
#import <Cordova/CDVPlugin.h>
#import <UIKit/UIKit.h>
#import <objc/runtime.h>

// The SDK's apple-generic version string (`PROJECT:TSBackgroundFetch-<build>`), which no SDK code references.  The slice
// is a static archive and the linker takes an archive member only when something references it: this reference is what
// puts the string into the app, for the harness's native_resolved and build.sh's proof (the React Native host's
// LabModule.m does the same).
extern const unsigned char TSBackgroundFetchVersionString[];

static NSString *LabStateName(UIApplicationState s) {
    switch (s) {
        case UIApplicationStateActive: return @"active";
        case UIApplicationStateInactive: return @"inactive";
        case UIApplicationStateBackground: return @"background";
    }
    return @"unknown";
}

@interface LabPlugin : CDVPlugin
@end

@implementation LabPlugin

/// Plugin instances in this process (`pluginLoad n=`): onload, so one per CDVViewController.  It must stay 1.
static int sLoads = 0;
/// Pages that asked for their args: a boot is a page that asked.
static int sBoots = 0;
static BOOL sFireArmed = NO;
/// MainActivity.DEFAULT_FINISH_DELAY_MS on Android: how long after a fetch the driver calls finish(taskId).
static const NSInteger kDefaultFinishDelayMs = 1000;

- (void)pluginInitialize {
    sLoads++;
    NSLog(@"%@", [NSString stringWithFormat:@"LAB point=pluginLoad n=%d scenes=%lu state=%@", sLoads,
                  (unsigned long)UIApplication.sharedApplication.connectedScenes.count,
                  LabStateName(UIApplication.sharedApplication.applicationState)]);
    [LabPlugin observeFires];
}

// --- argv ---------------------------------------------------------------------------------------------------------

/// `--key=value` pairs from argv; a bare `--key` reads "true".
+ (NSDictionary<NSString *, NSString *> *)argv {
    static NSDictionary *argv;
    static dispatch_once_t once;
    dispatch_once(&once, ^{
        NSMutableDictionary *out = [NSMutableDictionary dictionary];
        for (NSString *a in NSProcessInfo.processInfo.arguments) {
            if (![a hasPrefix:@"--"]) continue;
            NSString *body = [a substringFromIndex:2];
            NSRange eq = [body rangeOfString:@"="];
            if (eq.location == NSNotFound) { out[body] = @"true"; continue; }
            out[[body substringToIndex:eq.location]] = [body substringFromIndex:eq.location + 1];
        }
        argv = [out copy];
    });
    return argv;
}

+ (BOOL)flag:(NSString *)key fallback:(BOOL)fallback {
    NSString *v = self.argv[key];
    if (v == nil) return fallback;
    return [v isEqualToString:@"true"] || [v isEqualToString:@"1"];
}

+ (NSInteger)integer:(NSString *)key fallback:(NSInteger)fallback {
    NSString *v = self.argv[key];
    if (v == nil) return fallback;
    NSScanner *scanner = [NSScanner scannerWithString:v];
    NSInteger n;
    return ([scanner scanInteger:&n] && scanner.isAtEnd) ? n : fallback;
}

+ (NSString *)string:(NSString *)key {
    return self.argv[key] ?: @"";
}

+ (NSString *)decode:(NSString *)key fallback:(NSString *)fallback {
    NSString *v = self.argv[key];
    if (v == nil) return fallback;
    NSData *d = [[NSData alloc] initWithBase64EncodedString:v options:0];
    NSString *s = d ? [[NSString alloc] initWithData:d encoding:NSUTF8StringEncoding] : nil;
    return s ?: fallback;
}

/// The finish knobs.  Android persists them for its headless task; iOS has no headless mode, so they are the launch's
/// own (an OS launch has no argv: the defaults).
+ (NSDictionary *)knobValues {
    return @{@"finishDelayMs": @([self integer:@"finishDelayMs" fallback:kDefaultFinishDelayMs]),
             @"withholdFinish": @([self flag:@"withholdFinish" fallback:NO]),
             @"finishOnTimeout": @([self flag:@"finishOnTimeout" fallback:YES])};
}

// --- the actions ----------------------------------------------------------------------------------------------------

/// The launch's args: only what the driver honours, as on Android (the boot line echoes this map).  restartOnFinish is
/// Android's (the Simulator host has no finish trigger), read so the key set is the same.
- (void)args:(CDVInvokedUrlCommand *)command {
    sBoots++;
    NSMutableDictionary *out = [[LabPlugin knobValues] mutableCopy];
    [out addEntriesFromDictionary:@{
        @"stopOnTerminate": @([LabPlugin flag:@"stopOnTerminate" fallback:YES]),
        @"noConfigure": @([LabPlugin flag:@"noConfigure" fallback:NO]),
        @"restartOnFinish": @([LabPlugin flag:@"restartOnFinish" fallback:NO]),
        @"scenario": [LabPlugin string:@"scenario"],
        @"nativeLabel": [LabPlugin string:@"nativeLabel"],
        @"config": [LabPlugin decode:@"configB64" fallback:@"{}"],
        @"hooks": [LabPlugin decode:@"hooksB64" fallback:@"[]"],
        @"events": [LabPlugin decode:@"eventsB64" fallback:@"[]"],
        @"view": [LabPlugin string:@"view"],
        @"boot": @(sBoots),
    }];
    [self.commandDelegate sendPluginResult:[CDVPluginResult resultWithStatus:CDVCommandStatus_OK messageAsDictionary:out]
                                callbackId:command.callbackId];
}

- (void)knobs:(CDVInvokedUrlCommand *)command {
    [self.commandDelegate sendPluginResult:[CDVPluginResult resultWithStatus:CDVCommandStatus_OK
                                                         messageAsDictionary:[LabPlugin knobValues]]
                                callbackId:command.callbackId];
}

- (void)probe:(CDVInvokedUrlCommand *)command {
    [self.commandDelegate sendPluginResult:[CDVPluginResult resultWithStatus:CDVCommandStatus_OK
                                                         messageAsDictionary:@{@"probe": [LabPlugin probeLine]}]
                                callbackId:command.callbackId];
}

/// The host's verdict for the device view: run.py writes it into this app's data container (backends.Simctl
/// push_verdict), or nothing yet.  Read here, never judged.
- (void)verdict:(CDVInvokedUrlCommand *)command {
    NSString *path = [NSHomeDirectory() stringByAppendingPathComponent:@"Library/lab-verdict.json"];
    NSString *text = [NSString stringWithContentsOfFile:path encoding:NSUTF8StringEncoding error:nil] ?: @"";
    [self.commandDelegate sendPluginResult:[CDVPluginResult resultWithStatus:CDVCommandStatus_OK
                                                         messageAsDictionary:@{@"value": text}]
                                callbackId:command.callbackId];
}

/// A driver line (`app=…`), as `LAB app=…`.  Android logs it under the tag LAB, which the harness reads as "LAB " + the
/// line; os_log has no tag, and the harness's tap keeps only messages containing `LAB ` (fetch_pack's predicate), so
/// the prefix is written here (MEASURED: without it no driver line reached a capture, 2026-11-15).  No reply: lab.js
/// passes no callbacks, so the callbackId is INVALID and cordova-ios sends nothing back into the page for a line
/// (CDVCommandDelegateImpl).
- (void)log:(CDVInvokedUrlCommand *)command {
    id line = command.arguments.firstObject;
    if ([line isKindOfClass:NSString.class]) NSLog(@"LAB %@", line);
}

// --- the probe ------------------------------------------------------------------------------------------------------

/// A class method of the SDK by name, e.g. +[TSBackgroundFetch sharedInstance]: nil when it is absent.
+ (id)sdk:(NSString *)className selector:(NSString *)selectorName {
    Class cls = NSClassFromString(className);
    SEL sel = NSSelectorFromString(selectorName);
    if (cls == nil || ![cls respondsToSelector:sel]) return nil;
    id (*send)(id, SEL) = (id (*)(id, SEL))[cls methodForSelector:sel];
    return send(cls, sel);
}

/// Key-value read that never raises: only a key the object answers, as a getter or as KVC's `_key` / `key` ivar.
+ (id)kvc:(id)obj key:(NSString *)key {
    if (![obj isKindOfClass:NSObject.class]) return nil;
    Class cls = [obj class];
    if (![obj respondsToSelector:NSSelectorFromString(key)]
        && class_getInstanceVariable(cls, [@"_" stringByAppendingString:key].UTF8String) == NULL
        && class_getInstanceVariable(cls, key.UTF8String) == NULL) return nil;
    @try { return [obj valueForKey:key]; } @catch (__unused NSException *e) { return nil; }
}

+ (NSString *)boolOf:(id)obj key:(NSString *)key {
    id v = [self kvc:obj key:key];
    if (![v isKindOfClass:NSNumber.class]) return @"?";
    return [v boolValue] ? @"true" : @"false";
}

+ (NSString *)sdkBuild {
    NSString *v = [NSString stringWithUTF8String:(const char *)TSBackgroundFetchVersionString];
    NSRange r = [v rangeOfString:@"PROJECT:TSBackgroundFetch-"];
    if (r.location == NSNotFound) return @"?";
    NSString *rest = [v substringFromIndex:NSMaxRange(r)];
    NSUInteger n = 0;
    while (n < rest.length && isdigit([rest characterAtIndex:n])) n++;
    return n ? [rest substringToIndex:n] : @"?";
}

/// The SDK's build and state, read through the Objective-C runtime, never by building anything (the React Native
/// host's LabModule.m probe, field for field).
+ (NSString *)probeLine {
    id f = [self sdk:@"TSBackgroundFetch" selector:@"sharedInstance"];
    id subsObj = [self sdk:@"TSBGAppRefreshSubscriber" selector:@"subscribers"];
    NSDictionary *subsDict = [subsObj isKindOfClass:NSDictionary.class] ? subsObj : nil;
    NSArray *subs = [[subsDict allKeys] sortedArrayUsingSelector:@selector(compare:)] ?: @[];
    NSMutableArray *tasks = [NSMutableArray array];
    id list = [self sdk:@"TSBGTask" selector:@"tasks"];
    if ([list isKindOfClass:NSArray.class]) {
        for (id t in list) {
            id ident = [self kvc:t key:@"identifier"];
            [tasks addObject:[ident isKindOfClass:NSString.class] ? ident : @"?"];
        }
    }
    [tasks sortUsingSelector:@selector(compare:)];
    id bg = [self kvc:f key:@"backgroundTask"];
    BOOL bgOpen = [bg isKindOfClass:NSNumber.class] && [bg unsignedIntegerValue] != UIBackgroundTaskInvalid;
    return [NSString stringWithFormat:@"headless=- sdkBuild=%@ configured=%@ active=%@ stopOnTerminate=%@ subscribers=%@ "
                                       "tasks=%@ pendingFetch=%@ bgTask=%@",
            [self sdkBuild], [self boolOf:f key:@"configured"], [self boolOf:f key:@"active"],
            [self boolOf:f key:@"stopOnTerminate"], subs.count ? [subs componentsJoinedByString:@","] : @"-",
            tasks.count ? [tasks componentsJoinedByString:@","] : @"-",
            [self kvc:f key:@"completionHandler"] != nil ? @"true" : @"false", bgOpen ? @"true" : @"false"];
}

// --- (ios-task-sim) the custom-task hook ----------------------------------------------------------------------------

/// BGTaskScheduler is unavailable on the Simulator, so nothing launches a TSBGTask there; but scheduleTask has put the
/// task, with the plugin's callback, in TSBGTask's list before its submit fails.  The host's `fire: task` /
/// `fire: task-timeout` posts a Darwin notification, `<bundle id>/<kind>/<taskId>` (`simctl spawn <udid> notifyutil -p`),
/// and this calls that task's `execute` or `onTimeout`, as BGTaskScheduler's launch handler and the task's expiration
/// handler do on a device (the React Native host's LabModule.m LabFire, the Capacitor host's LabPlugin).
static void LabFireCallback(CFNotificationCenterRef center, void *observer, CFNotificationName name, const void *object,
                            CFDictionaryRef userInfo) {
    [LabPlugin fire:(__bridge NSString *)name];
}

+ (void)observeFires {
    if (sFireArmed) return;
    sFireArmed = YES;
    NSString *bundle = NSBundle.mainBundle.bundleIdentifier ?: @"";
    NSMutableArray *ids = [NSMutableArray array];
    for (id t in [NSBundle.mainBundle objectForInfoDictionaryKey:@"BGTaskSchedulerPermittedIdentifiers"] ?: @[]) {
        if ([t isKindOfClass:NSString.class] && ![t isEqualToString:@"com.transistorsoft.fetch"]) [ids addObject:t];
    }
    for (NSString *taskId in ids) {
        for (NSString *kind in @[@"task", @"task-timeout"]) {
            NSString *name = [NSString stringWithFormat:@"%@/%@/%@", bundle, kind, taskId];
            CFNotificationCenterAddObserver(CFNotificationCenterGetDarwinNotifyCenter(), NULL, LabFireCallback,
                                            (__bridge CFStringRef)name, NULL,
                                            CFNotificationSuspensionBehaviorDeliverImmediately);
        }
    }
    NSLog(@"%@", [NSString stringWithFormat:@"LAB point=fireArmed taskIds=%@",
                  ids.count ? [ids componentsJoinedByString:@","] : @"-"]);
}

+ (void)fire:(NSString *)name {
    NSString *prefix = [(NSBundle.mainBundle.bundleIdentifier ?: @"") stringByAppendingString:@"/"];
    if (![name hasPrefix:prefix]) return;
    NSString *rest = [name substringFromIndex:prefix.length];
    NSRange slash = [rest rangeOfString:@"/"];
    if (slash.location == NSNotFound) return;
    NSString *kind = [rest substringToIndex:slash.location], *taskId = [rest substringFromIndex:slash.location + 1];
    id task = nil;
    Class cls = NSClassFromString(@"TSBGTask");
    SEL get = NSSelectorFromString(@"get:");
    if (cls && [cls respondsToSelector:get]) {
        id (*send)(id, SEL, id) = (id (*)(id, SEL, id))[cls methodForSelector:get];
        task = send(cls, get, taskId);
    }
    SEL sel = NSSelectorFromString([kind isEqualToString:@"task-timeout"] ? @"onTimeout" : @"execute");
    BOOL ran = NO;
    if (task && [task respondsToSelector:sel]) {
        // NO from either: the task has no callback (loaded from the defaults, never scheduled in this process).
        BOOL (*run)(id, SEL) = (BOOL (*)(id, SEL))[task methodForSelector:sel];
        ran = run(task, sel);
    }
    NSLog(@"%@", [NSString stringWithFormat:@"LAB point=fire kind=%@ taskId=%@ found=%@ ran=%@", kind, taskId,
                  task ? @"true" : @"false", ran ? @"true" : @"false"]);
}

@end
