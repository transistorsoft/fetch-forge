// iOS half of the fetch lab's `Lab` native module (WO-002 phase 2).  The Android half is LabModule.kt, which reads
// the launch Intent's extras; here the scenario args arrive as process argv, which is what
// `xcrun simctl launch <dev> <bundle> --key=value …` gives.  Same module name, same keys, so the JavaScript driver
// (app/index.js) is one file for both OSes.  Modelled on bg-forge's labs/platforms/react-native/app/native-ios/
// LabModule.m (0fe0eb14c).
//
// A legacy RCT_EXPORT_MODULE, like LabModule.kt's ReactContextBaseJavaModule: the driver reaches it as
// NativeModules.Lab through the new architecture's legacy-module interop.  It is also the ExceptionsManager's
// delegate, so a fatal JavaScript error ends the process here as it does on Android (args()).
//
// One file for both iOS hosts, compiled without the project file listing it: by a local development pod in the
// CocoaPods host (LabModule.podspec, the Podfile's `pod 'LabModule'`), and as an app module in the SwiftPM host
// (app/package.json `swiftpmConfig.modules`, WO-002 phase 2b).  React Native's SwiftPM wrapper for an app module
// gives it React's headers but no other dependency, so this file imports none of the SDK's: it reads the SDK's
// state through the Objective-C runtime (LabSdk), and the one symbol it names, the version string, is resolved
// when the app links, where both hosts link TSBackgroundFetch.
#import <React/RCTBridgeModule.h>
#import <React/RCTExceptionsManager.h>
#import <UIKit/UIKit.h>

@interface LabModule : NSObject <RCTBridgeModule, RCTExceptionsManagerDelegate>
@end

@implementation LabModule

RCT_EXPORT_MODULE(Lab)

@synthesize moduleRegistry = _moduleRegistry;

+ (BOOL)requiresMainQueueSetup { return NO; }

/// The SDK's apple-generic version string (`@(#)PROGRAM:TSBackgroundFetch  PROJECT:TSBackgroundFetch-<build>`), which
/// no SDK code references.  The slice is a static archive, and the linker takes an archive member only when something
/// references it, so without this reference the app binary carries no PROJECT:TSBackgroundFetch-<build> string, and
/// build.sh's check and the harness's (fetch_pack Simctl.native_resolved, run.assert_native) would refuse every app
/// (MEASURED 2026-11-10: FetchLab.debug.dylib linked 72 TSBackgroundFetch symbols and no version string).  The probe
/// reports the build from it.
extern const unsigned char TSBackgroundFetchVersionString[];

static NSString *LabSdkBuild(void) {
  NSString *v = [NSString stringWithUTF8String:(const char *)TSBackgroundFetchVersionString] ?: @"";
  NSRange r = [v rangeOfString:@"PROJECT:TSBackgroundFetch-"];
  if (r.location == NSNotFound) return @"?";
  NSString *rest = [v substringFromIndex:NSMaxRange(r)];
  NSCharacterSet *stop = [[NSCharacterSet decimalDigitCharacterSet] invertedSet];
  NSRange end = [rest rangeOfCharacterFromSet:stop];
  return end.location == NSNotFound ? rest : [rest substringToIndex:end.location];
}

/// MainActivity.DEFAULT_FINISH_DELAY_MS on Android: how long after a fetch the driver calls finish(taskId).
static const NSInteger kDefaultFinishDelayMs = 1000;

/// `--key=value` pairs from argv; a bare `--key` reads "true".
static NSDictionary<NSString *, NSString *> *LabArgv(void) {
  NSMutableDictionary *out = [NSMutableDictionary dictionary];
  for (NSString *a in [[NSProcessInfo processInfo] arguments]) {
    if (![a hasPrefix:@"--"]) continue;
    NSString *body = [a substringFromIndex:2];
    NSRange eq = [body rangeOfString:@"="];
    if (eq.location == NSNotFound) { out[body] = @"true"; continue; }
    out[[body substringToIndex:eq.location]] = [body substringFromIndex:eq.location + 1];
  }
  return out;
}

static BOOL LabFlag(NSDictionary *argv, NSString *key, BOOL fallback) {
  NSString *v = argv[key];
  return v ? ([v isEqualToString:@"true"] || [v isEqualToString:@"1"]) : fallback;
}

static NSInteger LabInt(NSDictionary *argv, NSString *key, NSInteger fallback) {
  NSString *v = argv[key];
  NSScanner *s = v ? [NSScanner scannerWithString:v] : nil;
  NSInteger n = 0;
  return (s && [s scanInteger:&n] && s.isAtEnd) ? n : fallback;
}

static NSString *LabDecode(NSDictionary *argv, NSString *key, NSString *fallback) {
  NSString *v = argv[key];
  NSData *d = v ? [[NSData alloc] initWithBase64EncodedString:v options:0] : nil;
  NSString *s = d ? [[NSString alloc] initWithData:d encoding:NSUTF8StringEncoding] : nil;
  return s ?: fallback;
}

/// The finish knobs.  Android persists them for its headless task, which has no intent; iOS has no headless mode,
/// so knobs() is the launch's own.
static NSDictionary *LabKnobs(NSDictionary *argv) {
  return @{
    @"finishDelayMs":   @(LabInt(argv, @"finishDelayMs", kDefaultFinishDelayMs)),
    @"withholdFinish":  @(LabFlag(argv, @"withholdFinish", NO)),
    @"finishOnTimeout": @(LabFlag(argv, @"finishOnTimeout", YES)),
  };
}

/// The Android counter lives on MainActivity; the JavaScript context is what boots here.
static NSInteger sJsBoots = 0;

RCT_EXPORT_METHOD(args:(RCTPromiseResolveBlock)resolve reject:(RCTPromiseRejectBlock)reject)
{
  // A fatal JavaScript error must end the process, as it does on Android (useDevSupport false: the release
  // DevSupportManager rethrows it -> FATAL EXCEPTION -> `crashes`).  A Debug iOS build only shows a RedBox, so a
  // crash would score 0 here.  args() is the first thing the driver awaits: install before it.
  id exceptions = [_moduleRegistry moduleForName:"ExceptionsManager"];
  if ([exceptions isKindOfClass:[RCTExceptionsManager class]]) {
    ((RCTExceptionsManager *)exceptions).delegate = self;
  }
  // Only what the driver honours, as on Android: the boot line echoes this map.
  NSDictionary *argv = LabArgv();
  NSMutableDictionary *out = [LabKnobs(argv) mutableCopy];
  [out addEntriesFromDictionary:@{
    @"stopOnTerminate": @(LabFlag(argv, @"stopOnTerminate", YES)),
    @"noConfigure":     @(LabFlag(argv, @"noConfigure", NO)),
    @"scenario":        argv[@"scenario"] ?: @"",
    @"nativeLabel":     argv[@"nativeLabel"] ?: @"",
    @"statusBarHeight": @0,                        // the driver pads iOS itself (index.js)
    @"config":          LabDecode(argv, @"configB64", @"{}"),
    @"hooks":           LabDecode(argv, @"hooksB64", @"[]"),
    @"events":          LabDecode(argv, @"eventsB64", @"[]"),
    @"view":            argv[@"view"] ?: @"",
    @"boot":            @(++sJsBoots),
  }];
  resolve(out);
}

RCT_EXPORT_METHOD(knobs:(RCTPromiseResolveBlock)resolve reject:(RCTPromiseRejectBlock)reject)
{
  resolve(LabKnobs(LabArgv()));
}

/// The SDK's public class, method or property by name (its headers are not imported here; see the file comment).
static id LabSdk(NSString *className, NSString *classSelector) {
  Class c = NSClassFromString(className);
  SEL sel = NSSelectorFromString(classSelector);
  if (!c || ![c respondsToSelector:sel]) return nil;
  return ((id (*)(id, SEL))[c methodForSelector:sel])(c, sel);
}

static NSString *LabBool(id obj, NSString *key) {
  @try { return [[obj valueForKey:key] boolValue] ? @"true" : @"false"; }
  @catch (__unused NSException *e) { return @"?"; }
}

/// The SDK's build (LabSdkBuild) and state through its public API, never by building anything: the Android probe reads LifecycleManager
/// by reflection, which iOS has no counterpart of (`headless=-`).  `subscribers` are the fetch listeners
/// (TSBGAppRefreshSubscriber), `tasks` the scheduled TSBGTasks, `pendingFetch` whether a legacy fetch's completion
/// handler is held, `bgTask` whether its UIBackgroundTask (the legacy timeout, TSBackgroundFetch.m:186-195) is open.
RCT_EXPORT_METHOD(probe:(RCTPromiseResolveBlock)resolve reject:(RCTPromiseRejectBlock)reject)
{
  id f = LabSdk(@"TSBackgroundFetch", @"sharedInstance");
  NSDictionary *subscribers = LabSdk(@"TSBGAppRefreshSubscriber", @"subscribers");
  NSArray *subs = [[subscribers allKeys] sortedArrayUsingSelector:@selector(compare:)];
  NSMutableArray *tasks = [NSMutableArray array];
  for (id t in (NSArray *)LabSdk(@"TSBGTask", @"tasks")) [tasks addObject:[t valueForKey:@"identifier"] ?: @"?"];
  [tasks sortUsingSelector:@selector(compare:)];
  NSUInteger bg = f ? [[f valueForKey:@"backgroundTask"] unsignedIntegerValue] : UIBackgroundTaskInvalid;
  resolve([NSString stringWithFormat:
           @"headless=- sdkBuild=%@ configured=%@ active=%@ stopOnTerminate=%@ subscribers=%@ tasks=%@ pendingFetch=%@ bgTask=%@",
           LabSdkBuild(), LabBool(f, @"configured"), LabBool(f, @"active"), LabBool(f, @"stopOnTerminate"),
           subs.count ? [subs componentsJoinedByString:@","] : @"-",
           tasks.count ? [tasks componentsJoinedByString:@","] : @"-",
           [f valueForKey:@"completionHandler"] ? @"true" : @"false",
           bg != UIBackgroundTaskInvalid ? @"true" : @"false"]);
}

/// The host's verdict for the device view: run.py writes it into this app's data container
/// (backends.Simctl.push_verdict), or nothing yet.  Read here, never judged.
RCT_EXPORT_METHOD(verdict:(RCTPromiseResolveBlock)resolve reject:(RCTPromiseRejectBlock)reject)
{
  NSString *path = [NSHomeDirectory() stringByAppendingPathComponent:@"Library/lab-verdict.json"];
  resolve([NSString stringWithContentsOfFile:path encoding:NSUTF8StringEncoding error:nil] ?: @"");
}

#pragma mark - RCTExceptionsManagerDelegate

- (void)handleSoftJSExceptionWithMessage:(NSString *)message
                                   stack:(NSArray *)stack
                             exceptionId:(NSNumber *)exceptionId
                         extraDataAsJSON:(NSString *)extraDataAsJSON
{
}

/// The line names the error; abort() files the .ips the backend folds into `crashes`: one crash, counted once, like
/// Android's single FATAL EXCEPTION.
- (void)handleFatalJSExceptionWithMessage:(NSString *)message
                                    stack:(NSArray *)stack
                             exceptionId:(NSNumber *)exceptionId
                         extraDataAsJSON:(NSString *)extraDataAsJSON
{
  NSString *first = [[message ?: @"" componentsSeparatedByString:@"\n"] firstObject];
  NSLog(@"%@", [@"LAB point=jsFatal message=" stringByAppendingString:
                [first stringByAddingPercentEncodingWithAllowedCharacters:
                 [NSCharacterSet URLQueryAllowedCharacterSet]] ?: @"-"]);
  abort();
}

@end
