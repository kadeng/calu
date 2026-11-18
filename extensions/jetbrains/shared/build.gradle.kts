dependencies {
    intellijPlatform {
        // RemoteApiProviderService: a module jar of its own since 2026.3.
        bundledModule("intellij.platform.rpc")
    }
    compileOnly(libs.kotlin.serialization.core.jvm)
    compileOnly(libs.kotlin.serialization.json.jvm)

    testImplementation(libs.junit.jupiter)
    testImplementation(libs.junit4)
    testRuntimeOnly(libs.junit.platform.launcher)
}
