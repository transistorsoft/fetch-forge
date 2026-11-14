/// (WO-002) The Flutter fetch lab's native channel, named.  The driver (app/lib/main.dart) and its headless task use
/// the literal `MethodChannel('lab/probe')` and do not import this library; what matters is the plugin's Android half
/// (LabProbePlugin), which pub registers on every engine, the headless one included.
library lab_probe;

class LabProbe {
  static const String channel = 'lab/probe';
}
