// Structure taken from JetBrains' intellij-platform-modular-plugin-template (Apache-2.0), see NOTICE.
@file:Suppress("UnstableApiUsage")

import org.jetbrains.intellij.platform.gradle.extensions.intellijPlatform

pluginManagement {
    repositories {
        mavenCentral()
        gradlePluginPortal()
        // The RPC compiler plugin (id "rpc") is published here only.
        maven("https://packages.jetbrains.team/maven/p/ij/intellij-dependencies/")
    }
    plugins {
        // These three move together: the RPC compiler plugin is built for one Kotlin minor line
        // (its version starts with that Kotlin version) and for one platform line.
        id("rpc") version "2.3.20-RC2-0.1"
        id("org.jetbrains.kotlin.jvm") version "2.3.20"
        id("org.jetbrains.kotlin.plugin.serialization") version "2.3.20"
    }
}

plugins {
    id("org.jetbrains.intellij.platform.settings") version "2.20.1"
}

rootProject.name = "calu"

dependencyResolutionManagement {
    repositories {
        mavenCentral()
        intellijPlatform {
            defaultRepositories()
        }
    }
}

include("shared")
include("frontend")
include("backend")
include("python")
