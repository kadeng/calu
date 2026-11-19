// The optional module calu.python: PyCharm's SSH interpreter for the SSH mode. It compiles
// against the Python plugins and the SSH, remote-run and deployment plugins of PyCharm; the IDE
// loads it only where those are present (module descriptor calu.python.xml).
dependencies {
    intellijPlatform {
        bundledModule("intellij.platform.backend")
        bundledModule("intellij.platform.execution")
        bundledPlugin("PythonCore")
        bundledPlugin("Pythonid")
        bundledPlugin("org.jetbrains.plugins.remote-run")
        bundledPlugin("com.jetbrains.plugins.webDeployment")
        bundledPlugin("com.intellij.platform.ssh")
    }
    compileOnly(libs.kotlin.serialization.core.jvm)
    compileOnly(libs.kotlin.serialization.json.jvm)
    implementation(project(":shared"))
    implementation(project(":backend"))
}
