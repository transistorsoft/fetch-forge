import java.security.MessageDigest

plugins {
    id("com.android.application")
    // The Flutter Gradle Plugin must be applied after the Android and Kotlin Gradle plugins.
    id("dev.flutter.flutter-gradle-plugin")
}

android {
    // (WO-002) Distinct from bg-forge's com.transistorsoft.labs.*: the two labs share the Lab devices, and each lab's
    // first step evicts every other lab app by prefix (bg-forge WO-191).
    namespace = "com.transistorsoft.fetchlab.flutter"
    // (WO-002) The SDK levels are literals, Flutter 3.47.5's defaults (FlutterExtension.kt:23,26,34), not flutter.*:
    // run.py's source stamp hashes this file but nothing of the Flutter SDK, so a `flutter upgrade` that moved them
    // would leave a stale APK stamped current.  The Capacitor and React Native hosts pin theirs the same way.
    compileSdk = 36
    ndkVersion = flutter.ndkVersion

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    defaultConfig {
        applicationId = "com.transistorsoft.fetchlab.flutter"
        minSdk = 24
        targetSdk = 36
        versionCode = flutter.versionCode
        versionName = flutter.versionName
    }
}

kotlin {
    compilerOptions {
        jvmTarget = org.jetbrains.kotlin.gradle.dsl.JvmTarget.JVM_17
    }
}

flutter {
    source = "../.."
}

// (WO-002) Which tsbackgroundfetch the app's runtime classpath resolved, and the AAR's sha256: build.sh refuses an APK
// whose version is not the label or whose AAR is not labs/m2's (the Capacitor host's labNativeResolved, in Kotlin DSL).
// The plugin declares the SDK `implementation`, so it reaches the app only at runtime.
tasks.register("labNativeResolved") {
    val classpath = configurations.named("debugRuntimeClasspath")
    notCompatibleWithConfigurationCache("reads the resolved dependency graph")
    doLast {
        val cfg = classpath.get()
        val result = cfg.incoming.resolutionResult
        val comps = result.allComponents.filter {
            it.moduleVersion?.group == "com.transistorsoft" && it.moduleVersion?.name == "tsbackgroundfetch"
        }
        if (comps.isEmpty()) {
            println("[lab] tsbackgroundfetch resolved=NONE")
            return@doLast
        }
        comps.forEach { c ->
            val reasons = c.selectionReason.descriptions.joinToString("|") { it.description }.replace(' ', '_')
            println("[lab] tsbackgroundfetch resolved=${c.moduleVersion?.version} reason=$reasons")
        }
        val aars = cfg.incoming.artifactView {
            attributes { attribute(Attribute.of("artifactType", String::class.java), "aar") }
            componentFilter { id ->
                id is ModuleComponentIdentifier && id.group == "com.transistorsoft" && id.module == "tsbackgroundfetch"
            }
        }.files.files
        aars.forEach { f ->
            val md = MessageDigest.getInstance("SHA-256")
            f.inputStream().use { s ->
                val buf = ByteArray(1 shl 16)
                while (true) { val n = s.read(buf); if (n <= 0) break; md.update(buf, 0, n) }
            }
            println("[lab] tsbackgroundfetch aar=$f sha256=${md.digest().joinToString("") { "%02x".format(it) }}")
        }
    }
}
