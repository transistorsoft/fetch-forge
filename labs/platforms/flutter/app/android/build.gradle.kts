// (WO-002) The Flutter fetch lab's root project: Flutter 3.47.5's template, plus the native pin.
//
// The SDK under test is the label build.sh passes (-P labNativeVersion=<label>, -P labMavenRepo=<labs/m2>).  The
// plugin reads it as rootProject.ext.backgroundFetchVersion (flutter/background_fetch android/build.gradle
// safeExtGet('backgroundFetchVersion', '4.1.+')), and com.transistorsoft:tsbackgroundfetch may resolve from labs/m2
// ONLY: the plugin's own `4.1.+` would otherwise also match a Maven Central release.  The label is required: there is
// no fallback, so a build without it fails instead of quietly linking whatever `4.1.+` finds.
val labNativeVersion = (findProperty("labNativeVersion") as String?)
    ?: throw GradleException("labNativeVersion is required (-P labNativeVersion=<label>): build through labs/platforms/flutter/build.sh")
extra["backgroundFetchVersion"] = labNativeVersion

allprojects {
    repositories {
        exclusiveContent {
            forRepository {
                maven {
                    url = uri((findProperty("labMavenRepo") as String?) ?: rootProject.file("../../../../m2").path)
                }
            }
            filter { includeModule("com.transistorsoft", "tsbackgroundfetch") }
        }
        google()
        mavenCentral()
    }
}

// The template's: every project's build output under app/build/, so the staged plugin is never written into.
val newBuildDir: Directory =
    rootProject.layout.buildDirectory
        .dir("../../build")
        .get()
rootProject.layout.buildDirectory.value(newBuildDir)

subprojects {
    val newSubprojectBuildDir: Directory = newBuildDir.dir(project.name)
    project.layout.buildDirectory.value(newSubprojectBuildDir)
}
subprojects {
    project.evaluationDependsOn(":app")
}

tasks.register<Delete>("clean") {
    delete(rootProject.layout.buildDirectory)
}
