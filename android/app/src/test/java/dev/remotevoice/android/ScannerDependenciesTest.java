package dev.remotevoice.android;

import static org.junit.Assert.assertNotNull;
import org.junit.Test;

/** Scanner's published POM omits these dependencies: guard the physical-device crash. */
public final class ScannerDependenciesTest {
    @Test public void scannerAndroidXRuntimeClassesAreAvailable() throws Exception {
        ClassLoader loader = getClass().getClassLoader();
        for (String name : new String[] {
                "androidx.core.content.ContextCompat",
                "androidx.core.app.ActivityCompat",
                "androidx.fragment.app.Fragment",
                "com.journeyapps.barcodescanner.CaptureManager",
                "com.journeyapps.barcodescanner.CaptureActivity"
        }) {
            assertNotNull(name, Class.forName(name, false, loader));
        }
    }
}
