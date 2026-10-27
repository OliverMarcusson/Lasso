plugins {
    id("com.android.application")
}

android {
    namespace = "dev.marcusson.lasso"
    compileSdk = 36

    defaultConfig {
        applicationId = "dev.marcusson.lasso"
        minSdk = 33
        targetSdk = 36
        versionCode = 1
        versionName = "0.1.0"
    }

    buildTypes {
        release {
            isMinifyEnabled = false
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
}

dependencies {
    implementation("org.jetbrains.kotlinx:kotlinx-coroutines-android:1.10.2")
    implementation("dev.rikka.shizuku:api:13.1.5")
    implementation("dev.rikka.shizuku:provider:13.1.5")

    testImplementation("junit:junit:4.13.2")
    testImplementation("org.jetbrains.kotlinx:kotlinx-coroutines-test:1.10.2")
    // android.jar's org.json throws "not mocked" on the JVM.
    testImplementation("org.json:json:20250517")
}
