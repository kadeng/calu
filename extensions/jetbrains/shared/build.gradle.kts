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

// The tests against real bridges (RealBridgeSyncTest): `-Psshbox=<ssh config file>` (or the
// environment variable CALU_SSHBOX) names the config of uitest/sshbox/run.sh and switches the
// test over real SSH on; without it that test is skipped. `-PcaluRepo=<dir>` names the calu
// checkout whose `uv run calu ide` the local test starts (default: found from this directory).
tasks.withType<Test>().configureEach {
    (providers.gradleProperty("sshbox").orNull ?: System.getenv("CALU_SSHBOX"))?.let { systemProperty("calu.sshbox", it) }
    providers.gradleProperty("caluRepo").orNull?.let { systemProperty("calu.repo", it) }
    inputs.property("sshbox", providers.gradleProperty("sshbox").orNull ?: System.getenv("CALU_SSHBOX") ?: "")
}
