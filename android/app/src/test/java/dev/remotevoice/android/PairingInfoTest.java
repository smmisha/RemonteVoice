package dev.remotevoice.android;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertThrows;

import org.junit.Test;

public final class PairingInfoTest {
    private static final String FINGERPRINT = "abcdef0123456789".repeat(4);

    private static String link(String host, String port, String fingerprint, String pin) {
        return "remotevoice://pair?host=" + host + "&port=" + port
                + "&fingerprint=" + fingerprint + "&pin=" + pin;
    }

    @Test public void readsHostIdentityAndNormalizesFingerprint() {
        PairingInfo pairing = PairingInfo.parse(link("192.168.1.12", "8765", FINGERPRINT, "00123456"));
        assertEquals("192.168.1.12", pairing.host);
        assertEquals(8765, pairing.port);
        assertEquals(FINGERPRINT.toUpperCase(java.util.Locale.ROOT), pairing.fingerprint);
        assertEquals("00123456", pairing.pin);
        assertEquals("wss://192.168.1.12:8765/", pairing.webSocketUrl());
    }

    @Test public void permitsOnlyRfc1918Ipv4Ranges() {
        for (String host : new String[] {"10.0.0.1", "10.255.255.255", "172.16.0.1",
                "172.31.255.254", "192.168.0.1", "192.168.255.254"}) {
            assertEquals(host, PairingInfo.parse(link(host, "1", FINGERPRINT, "12345678")).host);
        }
        for (String host : new String[] {"127.0.0.1", "169.254.1.2", "172.15.255.254",
                "172.32.0.1", "192.169.0.1", "8.8.8.8", "0.0.0.0", "224.0.0.1",
                "localhost", "remotevoice.local", "192.168.1", "192.168.1.2.3", "192.168..1",
                "192.168.1.256", "192.168.01.1", "192.168.1.-1", "0xc0.168.1.1", "[::1]"}) {
            assertThrows(host, IllegalArgumentException.class,
                    () -> PairingInfo.parse(link(host, "8765", FINGERPRINT, "12345678")));
        }
    }

    @Test public void rejectsMissingDuplicateAndUnknownFields() {
        String valid = link("10.1.2.3", "8765", FINGERPRINT, "12345678");
        for (String suffix : new String[] {"&pin=87654321", "&host=10.4.5.6",
                "&%70in=87654321", "&unexpected=value", "&", "&port"}) {
            assertThrows(IllegalArgumentException.class, () -> PairingInfo.parse(valid + suffix));
        }
        assertThrows(IllegalArgumentException.class,
                () -> PairingInfo.parse(valid.substring(0, valid.indexOf("&pin="))));
    }

    @Test public void validatesAsciiPinFingerprintAndPortBounds() {
        for (String port : new String[] {"0", "65536", "-1", "+1", "1.5", "", "999999"}) {
            assertThrows(IllegalArgumentException.class,
                    () -> PairingInfo.parse(link("10.1.2.3", port, FINGERPRINT, "12345678")));
        }
        assertEquals(65535, PairingInfo.parse(link("10.1.2.3", "65535", FINGERPRINT, "12345678")).port);
        for (String pin : new String[] {"1234567", "123456789", "１２３４５６７８", "1234567a", "1234567+"}) {
            assertThrows(IllegalArgumentException.class,
                    () -> PairingInfo.parse(link("10.1.2.3", "8765", FINGERPRINT, pin)));
        }
        for (String fingerprint : new String[] {"a".repeat(63), "a".repeat(65), "g".repeat(64)}) {
            assertThrows(IllegalArgumentException.class,
                    () -> PairingInfo.parse(link("10.1.2.3", "8765", fingerprint, "12345678")));
        }
    }

    @Test public void rejectsAlternateUriTargetsAndMalformedEncoding() {
        String valid = link("10.1.2.3", "8765", FINGERPRINT, "12345678");
        for (String value : new String[] {null, "", " " + valid, valid + " ", valid + "#fragment",
                valid.replace("remotevoice:", "https:"), valid.replace("//pair?", "//other?"),
                valid.replace("//pair?", "//pair/path?"), valid.replace("//pair?", "//user@pair?"),
                valid.replace("//pair?", "//pair:123?"), valid + "%xy", valid + "%", valid + "="}) {
            assertThrows(IllegalArgumentException.class, () -> PairingInfo.parse(value));
        }
    }

    @Test public void decodesNormalQueryPercentEncoding() {
        PairingInfo pairing = PairingInfo.parse(link("192%2E168%2E1%2E2", "8765", FINGERPRINT, "12345678"));
        assertEquals("192.168.1.2", pairing.host);
    }
}
