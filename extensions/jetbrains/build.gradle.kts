// Structure taken from JetBrains' intellij-platform-modular-plugin-template (Apache-2.0), see NOTICE.
import org.jetbrains.intellij.platform.gradle.IntelliJPlatformType
import org.jetbrains.intellij.platform.gradle.TestFrameworkType
import org.jetbrains.intellij.platform.gradle.tasks.aware.SplitModeAware

group = "io.github.kadeng.calu"
version = providers.gradleProperty("pluginVersion").get()

val intellijPlatformVersion = providers.gradleProperty("intellijPlatformVersion").get()

plugins {
    id("org.jetbrains.intellij.platform")
    id("org.jetbrains.kotlin.jvm")
    id("rpc") apply false
    id("org.jetbrains.kotlin.plugin.serialization") apply false
}

kotlin {
    jvmToolchain(25)
}

subprojects {
    apply(plugin = "org.jetbrains.intellij.platform.module")
    apply(plugin = "rpc")
    apply(plugin = "org.jetbrains.kotlin.jvm")
    apply(plugin = "org.jetbrains.kotlin.plugin.serialization")

    extensions.configure<org.jetbrains.kotlin.gradle.dsl.KotlinJvmProjectExtension> {
        jvmToolchain(25)
    }

    tasks.withType<Test>().configureEach {
        useJUnitPlatform()
        testLogging {
            events("failed", "skipped")
            exceptionFormat = org.gradle.api.tasks.testing.logging.TestExceptionFormat.FULL
        }
    }
}

dependencies {
    intellijPlatform {
        pycharm(intellijPlatformVersion)

        pluginModule(implementation(project(":shared")))
        pluginModule(implementation(project(":frontend")))
        pluginModule(implementation(project(":backend")))
        testFramework(TestFrameworkType.Platform)
    }
    testImplementation(libs.junit4)
}

intellijPlatform {
    // `runIdeSplitMode` starts a backend and a thin client; the plugin is installed in both.
    splitMode = true
    pluginInstallationTarget = SplitModeAware.PluginInstallationTarget.BOTH

    pluginConfiguration {
        ideaVersion {
            sinceBuild = "263"
            untilBuild = provider { null }
        }
    }

    pluginVerification {
        ides {
            create(IntelliJPlatformType.PyCharm, intellijPlatformVersion)
            providers.gradleProperty("verifyIdeVersions").orNull
                ?.split(',')?.map { it.trim() }?.filter { it.isNotEmpty() && it != intellijPlatformVersion }
                ?.forEach { create(IntelliJPlatformType.PyCharm, it) }
        }
    }
}
