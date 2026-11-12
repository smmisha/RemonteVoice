package dev.remontevoice.android;

import static org.junit.Assert.assertArrayEquals;
import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertNull;
import static org.junit.Assert.assertThrows;
import static org.junit.Assert.assertTrue;

import java.lang.reflect.Constructor;
import java.net.InetAddress;
import java.nio.ByteBuffer;
import java.security.MessageDigest;
import java.util.Arrays;
import java.util.Map;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.concurrent.atomic.AtomicReference;
import okhttp3.Response;
import okhttp3.WebSocket;
import okhttp3.WebSocketListener;
import okhttp3.mockwebserver.MockResponse;
import okhttp3.mockwebserver.MockWebServer;
import okhttp3.mockwebserver.RecordedRequest;
import okhttp3.tls.HandshakeCertificates;
import okhttp3.tls.HeldCertificate;
import okio.ByteString;
import org.junit.Test;

/** Synthetic JPEG headers and loopback TLS only: no Bitmap, screen capture, or Android runtime. */
public final class ScreenConnectionTest {
    private static final String TICKET = "test_Screen_0123456789_abcdefghijkLMNOPQRSTUVWXYZ";

    private static byte[] videoFrame() {
        String hex = "0000000109f0000000016742c020da0140196c0440000003004000000f23c60ca8"
                + "00000168ce0fc8000001658888";
        byte[] data = new byte[hex.length()/2];
        for (int n=0; n<data.length; n++) data[n]=(byte)Integer.parseInt(hex.substring(n*2,n*2+2),16);
        return data;
    }

    @Test public void videoMetadataUsesBoundedIntegersWithoutRelaxingAudioParser() {
        assertEquals(Integer.valueOf(1280), BridgeConnection.parseScreenControl(
                "{\"type\":\"screen_ok\",\"width\":1280}").get("width"));
        assertThrows(IllegalArgumentException.class, () -> BridgeConnection.parseControl(
                "{\"type\":\"mic_state\",\"enabled\":1}"));
        for (String number : new String[]{"-1", "1.5", "01", "1e3", "2147483648", "999999999999999"}) {
            assertThrows(IllegalArgumentException.class, () -> BridgeConnection.parseScreenControl(
                    "{\"type\":\"screen_ok\",\"width\":"+number+"}"));
        }
    }

    @Test public void videoIsExplicitAndEachAccessUnitWaitsForConsumerAcceptance() throws Exception {
        HeldCertificate certificate = certificate();
        Observer observer = new Observer();
        CountDownLatch ready = new CountDownLatch(1), frame = new CountDownLatch(1);
        AtomicInteger pulls = new AtomicInteger();
        ScreenConnection.Listener video = new ScreenConnection.Listener() {
            public void onReady() { throw new AssertionError("Unexpected JPEG"); }
            public void onFrame(byte[] jpeg) { throw new AssertionError("Unexpected JPEG"); }
            public void onVideoReady(int width, int height, int fps) {
                assertEquals(1280, width); assertEquals(800, height); assertEquals(30, fps); ready.countDown();
            }
            public void onVideoFrame(byte[] data) { assertArrayEquals(videoFrame(), data); frame.countDown(); }
            public void onClosed(String message) { observer.onClosed(message); }
        };
        try (MockWebServer server = server(certificate, new WebSocketListener() {
            @Override public void onMessage(WebSocket socket, String text) {
                if ("screen_auth".equals(BridgeConnection.parseControl(text).get("type"))) {
                    socket.send("{\"type\":\"screen_ok\",\"codec\":\"h264\",\"width\":1280,\"height\":800,\"fps\":30}");
                } else { pulls.incrementAndGet(); socket.send(ByteString.of(videoFrame())); }
            }
        })) {
            ScreenConnection connection = new ScreenConnection(loopbackPairing(server, fingerprint(certificate)), video, true);
            try {
                connection.connect(TICKET);
                assertTrue(ready.await(3, TimeUnit.SECONDS));
                assertEquals(0, pulls.get());
                assertTrue(connection.requestNext());
                assertFalse(connection.requestNext());
                assertTrue(frame.await(3, TimeUnit.SECONDS));
                assertEquals(1, pulls.get());
                assertEquals(0, observer.closeCount.get());
                assertTrue(connection.requestNext());
            } finally { connection.close(); }
        }
    }

    @Test public void unexpectedCodecOrInvalidVideoMetadataCannotAuthorizeFrames() throws Exception {
        for (boolean video : new boolean[]{false, true}) {
            String[] replies = video ? new String[]{
                "{\"type\":\"screen_ok\"}",
                "{\"type\":\"screen_ok\",\"codec\":\"h264\",\"width\":1920,\"height\":800,\"fps\":30}",
                "{\"type\":\"screen_ok\",\"codec\":\"h264\",\"width\":1280,\"height\":800,\"fps\":60}",
                "{\"type\":\"screen_ok\",\"codec\":\"h264\",\"width\":\"1280\",\"height\":800,\"fps\":30}"
            } : new String[]{"{\"type\":\"screen_ok\",\"codec\":\"h264\",\"width\":1280,\"height\":800,\"fps\":30}"};
            for (String reply : replies) {
                HeldCertificate certificate = certificate();
                Observer observer = new Observer();
                try (MockWebServer server = server(certificate, new WebSocketListener() {
                    @Override public void onMessage(WebSocket socket, String text) { socket.send(reply); }
                })) {
                    ScreenConnection connection = new ScreenConnection(loopbackPairing(server, fingerprint(certificate)), observer, video);
                    try {
                        connection.connect(TICKET);
                        assertTrue(observer.closed.await(3, TimeUnit.SECONDS));
                        assertEquals(0, observer.readyCount.get());
                        assertEquals(0, observer.frameCount.get());
                        assertFalse(connection.requestNext());
                    } finally { connection.close(); }
                }
            }
        }
    }

    @Test public void videoRejectsDependentFirstFrameWithoutAffectingJpegCompatibility() throws Exception {
        HeldCertificate certificate = certificate();
        Observer observer = new Observer();
        CountDownLatch ready = new CountDownLatch(1);
        ScreenConnection.Listener listener = new ScreenConnection.Listener() {
            public void onReady() { throw new AssertionError(); }
            public void onFrame(byte[] data) { throw new AssertionError(); }
            public void onVideoReady(int width, int height, int fps) { ready.countDown(); }
            public void onVideoFrame(byte[] data) { throw new AssertionError("Invalid frame delivered"); }
            public void onClosed(String message) { observer.onClosed(message); }
        };
        try (MockWebServer server = server(certificate, new WebSocketListener() {
            @Override public void onMessage(WebSocket socket, String text) {
                if ("screen_auth".equals(BridgeConnection.parseControl(text).get("type"))) {
                    socket.send("{\"type\":\"screen_ok\",\"codec\":\"h264\",\"width\":1280,\"height\":800,\"fps\":30}");
                } else socket.send(ByteString.of(new byte[]{0,0,1,9,(byte)0xf0,0,0,1,0x41,1}));
            }
        })) {
            ScreenConnection connection = new ScreenConnection(loopbackPairing(server, fingerprint(certificate)), listener, true);
            try {
                connection.connect(TICKET);
                assertTrue(ready.await(3, TimeUnit.SECONDS));
                assertTrue(connection.requestNext());
                assertTrue(observer.closed.await(3, TimeUnit.SECONDS));
                assertFalse(connection.requestNext());
            } finally { connection.close(); }
        }
    }

    @Test public void pinnedTlsAuthenticatesBeforePullAndAllowsExactlyOneFrameInFlight() throws Exception {
        HeldCertificate certificate = certificate();
        Observer observer = new Observer();
        AtomicReference<WebSocket> peer = new AtomicReference<>();
        AtomicReference<String> authentication = new AtomicReference<>();
        AtomicInteger pulls = new AtomicInteger();
        CountDownLatch firstPull = new CountDownLatch(1);
        CountDownLatch secondPull = new CountDownLatch(1);
        try (MockWebServer server = server(certificate, new WebSocketListener() {
            @Override public void onOpen(WebSocket socket, Response response) { peer.set(socket); }
            @Override public void onMessage(WebSocket socket, String text) {
                if (authentication.compareAndSet(null, text)) {
                    socket.send("{\"type\":\"screen_ok\"}");
                } else {
                    assertEquals("screen_next", BridgeConnection.parseControl(text).get("type"));
                    if (pulls.incrementAndGet() == 1) firstPull.countDown();
                    else secondPull.countDown();
                }
            }
        })) {
            ScreenConnection connection = new ScreenConnection(loopbackPairing(server, fingerprint(certificate)), observer);
            try {
                assertFalse(connection.requestNext());
                connection.connect(TICKET);
                assertTrue(observer.ready.await(3, TimeUnit.SECONDS));
                Map<String, Object> auth = BridgeConnection.parseControl(authentication.get());
                assertEquals(2, auth.size());
                assertEquals("screen_auth", auth.get("type"));
                assertEquals(TICKET, auth.get("ticket"));
                RecordedRequest request = server.takeRequest(3, TimeUnit.SECONDS);
                assertEquals("/screen", request.getPath());
                assertNull(request.getRequestUrl().query());

                assertTrue(connection.requestNext());
                assertFalse(connection.requestNext());
                assertTrue(firstPull.await(3, TimeUnit.SECONDS));
                byte[] first = jpeg(960, 540);
                peer.get().send(ByteString.of(first));
                assertTrue(observer.firstFrame.await(3, TimeUnit.SECONDS));
                assertArrayEquals(first, observer.frame.get());

                assertTrue(connection.requestNext());
                assertFalse(connection.requestNext());
                assertTrue(secondPull.await(3, TimeUnit.SECONDS));
                byte[] second = jpeg(320, 180);
                peer.get().send(ByteString.of(second));
                assertTrue(observer.secondFrame.await(3, TimeUnit.SECONDS));
                assertArrayEquals(second, observer.frame.get());
                assertEquals(2, pulls.get());
                assertEquals(2, observer.frameCount.get());
                assertEquals(1, observer.readyCount.get());
                assertEquals(0, observer.closeCount.get());
                assertEquals(1, server.getRequestCount());
            } finally {
                connection.close();
            }
        }
    }

    @Test public void wrongLeafPinRejectsTlsBeforeTicketTransmission() throws Exception {
        HeldCertificate certificate = certificate();
        Observer observer = new Observer();
        AtomicInteger messages = new AtomicInteger();
        try (MockWebServer server = server(certificate, new WebSocketListener() {
            @Override public void onMessage(WebSocket socket, String text) { messages.incrementAndGet(); }
        })) {
            ScreenConnection connection = new ScreenConnection(loopbackPairing(server, "0".repeat(64)), observer);
            try {
                connection.connect(TICKET);
                assertTrue(observer.closed.await(5, TimeUnit.SECONDS));
                assertEquals(0, messages.get());
                assertEquals(0, observer.readyCount.get());
                assertEquals(0, observer.frameCount.get());
                assertEquals(1, observer.closeCount.get());
                assertFalse(observer.message.get().contains(TICKET));
            } finally {
                connection.close();
            }
        }
    }

    @Test public void frameBeforeAuthenticationOrWithoutPullIsRejected() throws Exception {
        for (boolean beforeAuth : new boolean[] {true, false}) {
            HeldCertificate certificate = certificate();
            Observer observer = new Observer();
            try (MockWebServer server = server(certificate, new WebSocketListener() {
                @Override public void onOpen(WebSocket socket, Response response) {
                    if (beforeAuth) socket.send(ByteString.of(jpeg(32, 18)));
                }
                @Override public void onMessage(WebSocket socket, String text) {
                    if (!beforeAuth) {
                        socket.send("{\"type\":\"screen_ok\"}");
                        socket.send(ByteString.of(jpeg(32, 18)));
                    }
                }
            })) {
                ScreenConnection connection = new ScreenConnection(loopbackPairing(server, fingerprint(certificate)), observer);
                try {
                    connection.connect(TICKET);
                    assertTrue(observer.closed.await(3, TimeUnit.SECONDS));
                    assertEquals(0, observer.frameCount.get());
                    assertEquals(beforeAuth ? 0 : 1, observer.readyCount.get());
                    assertEquals(1, observer.closeCount.get());
                    assertFalse(connection.requestNext());
                } finally {
                    connection.close();
                }
            }
        }
    }

    @Test public void malformedOrWrongAuthenticationReplyCannotAuthorizePreview() throws Exception {
        for (String response : new String[] {
                "[]", "{\"type\":\"auth_ok\"}", "{\"type\":\"screen_ok\",\"extra\":\"x\"}",
                "{\"type\":\"screen_ok\",\"type\":\"screen_ok\"}", "{\"type\":\"screen_ok\"}garbage"}) {
            HeldCertificate certificate = certificate();
            Observer observer = new Observer();
            try (MockWebServer server = server(certificate, new WebSocketListener() {
                @Override public void onMessage(WebSocket socket, String text) { socket.send(response); }
            })) {
                ScreenConnection connection = new ScreenConnection(loopbackPairing(server, fingerprint(certificate)), observer);
                try {
                    connection.connect(TICKET);
                    assertTrue(observer.closed.await(3, TimeUnit.SECONDS));
                    assertEquals(0, observer.readyCount.get());
                    assertEquals(0, observer.frameCount.get());
                    assertEquals(1, observer.closeCount.get());
                    assertFalse(connection.requestNext());
                } finally {
                    connection.close();
                }
            }
        }
    }

    @Test public void invalidOrOversizedPulledFrameIsNeverDelivered() throws Exception {
        byte[] oversized = new byte[ScreenFrameLimits.MAX_BYTES + 1];
        for (byte[] packet : new byte[][] {new byte[0], oversized, jpeg(1921, 1), jpeg(1, 1201)}) {
            HeldCertificate certificate = certificate();
            Observer observer = new Observer();
            try (MockWebServer server = server(certificate, new WebSocketListener() {
                @Override public void onMessage(WebSocket socket, String text) {
                    if ("screen_auth".equals(BridgeConnection.parseControl(text).get("type"))) {
                        socket.send("{\"type\":\"screen_ok\"}");
                    } else socket.send(ByteString.of(packet));
                }
            })) {
                ScreenConnection connection = new ScreenConnection(loopbackPairing(server, fingerprint(certificate)), observer);
                try {
                    connection.connect(TICKET);
                    assertTrue(observer.ready.await(3, TimeUnit.SECONDS));
                    assertTrue(connection.requestNext());
                    assertTrue(observer.closed.await(3, TimeUnit.SECONDS));
                    assertEquals(0, observer.frameCount.get());
                    assertEquals(1, observer.closeCount.get());
                } finally {
                    connection.close();
                }
            }
        }
    }

    @Test public void remoteErrorNotifiesOnceAndClosedInstanceDoesNotRetry() throws Exception {
        HeldCertificate certificate = certificate();
        Observer observer = new Observer();
        try (MockWebServer server = server(certificate, new WebSocketListener() {
            @Override public void onMessage(WebSocket socket, String text) {
                socket.send("{\"type\":\"screen_error\",\"message\":\"disabled\"}");
            }
        })) {
            ScreenConnection connection = new ScreenConnection(loopbackPairing(server, fingerprint(certificate)), observer);
            try {
                connection.connect(TICKET);
                assertTrue(observer.closed.await(3, TimeUnit.SECONDS));
                assertEquals(ScreenConnection.errorMessage("disabled"), observer.message.get());
                connection.connect(TICKET);
                assertFalse(connection.requestNext());
                connection.close();
                connection.close();
                assertEquals(1, observer.closeCount.get());
                assertEquals(0, observer.readyCount.get());
                assertEquals(1, server.getRequestCount());
            } finally {
                connection.close();
            }
        }
    }

    @Test public void explicitCloseWhileWaitingDetachesObserverAndRejectsLaterFrames() throws Exception {
        HeldCertificate certificate = certificate();
        Observer observer = new Observer();
        AtomicReference<WebSocket> peer = new AtomicReference<>();
        CountDownLatch pull = new CountDownLatch(1);
        try (MockWebServer server = server(certificate, new WebSocketListener() {
            @Override public void onOpen(WebSocket socket, Response response) { peer.set(socket); }
            @Override public void onMessage(WebSocket socket, String text) {
                if ("screen_auth".equals(BridgeConnection.parseControl(text).get("type"))) {
                    socket.send("{\"type\":\"screen_ok\"}");
                } else pull.countDown();
            }
        })) {
            ScreenConnection connection = new ScreenConnection(loopbackPairing(server, fingerprint(certificate)), observer);
            try {
                connection.connect(TICKET);
                assertTrue(observer.ready.await(3, TimeUnit.SECONDS));
                assertTrue(connection.requestNext());
                assertTrue(pull.await(3, TimeUnit.SECONDS));
                connection.close();
                connection.close();
                peer.get().send(ByteString.of(jpeg(32, 18)));
                assertFalse(connection.requestNext());
                assertEquals(0, observer.frameCount.get());
                assertEquals(0, observer.closeCount.get());
            } finally {
                connection.close();
            }
        }
    }

    @Test public void silentHostHitsScreenAuthenticationDeadline() throws Exception {
        HeldCertificate certificate = certificate();
        Observer observer = new Observer();
        CountDownLatch auth = new CountDownLatch(1);
        try (MockWebServer server = server(certificate, new WebSocketListener() {
            @Override public void onMessage(WebSocket socket, String text) { auth.countDown(); }
        })) {
            ScreenConnection connection = new ScreenConnection(loopbackPairing(server, fingerprint(certificate)), observer);
            try {
                connection.connect(TICKET);
                assertTrue(auth.await(3, TimeUnit.SECONDS));
                assertTrue(observer.closed.await(10, TimeUnit.SECONDS));
                assertEquals(ScreenConnection.errorMessage("timeout"), observer.message.get());
                assertEquals(0, observer.readyCount.get());
                assertEquals(1, observer.closeCount.get());
            } finally {
                connection.close();
            }
        }
    }

    @Test public void silentFrameRequestHitsFrameDeadline() throws Exception {
        HeldCertificate certificate = certificate();
        Observer observer = new Observer();
        try (MockWebServer server = server(certificate, new WebSocketListener() {
            @Override public void onMessage(WebSocket socket, String text) {
                if ("screen_auth".equals(BridgeConnection.parseControl(text).get("type"))) {
                    socket.send("{\"type\":\"screen_ok\"}");
                }
            }
        })) {
            ScreenConnection connection = new ScreenConnection(loopbackPairing(server, fingerprint(certificate)), observer);
            try {
                connection.connect(TICKET);
                assertTrue(observer.ready.await(3, TimeUnit.SECONDS));
                assertTrue(connection.requestNext());
                assertTrue(observer.closed.await(7, TimeUnit.SECONDS));
                assertEquals(ScreenConnection.errorMessage("timeout"), observer.message.get());
                assertEquals(0, observer.frameCount.get());
                assertEquals(1, observer.closeCount.get());
            } finally {
                connection.close();
            }
        }
    }

    @Test public void jpegDimensionsAcceptBoundedBaselineAndProgressiveHeaders() {
        for (int marker : new int[] {192, 193, 194}) {
            byte[] packet = jpeg(960, 540);
            packet[3] = (byte) marker;
            byte[] original = packet.clone();
            assertArrayEquals(new int[] {960, 540}, ScreenFrameLimits.dimensions(packet));
            assertArrayEquals(original, packet);
        }
        assertArrayEquals(new int[] {1, 1}, ScreenFrameLimits.dimensions(jpeg(1, 1)));
        assertArrayEquals(new int[] {1920, 1200}, ScreenFrameLimits.dimensions(jpeg(1920,1200)));
    }

    @Test public void jpegDimensionsRejectWrongBoundsAndExcessiveEncodedSize() {
        byte[] oversized = Arrays.copyOf(jpeg(32, 18), ScreenFrameLimits.MAX_BYTES + 1);
        oversized[oversized.length - 2] = (byte) 255;
        oversized[oversized.length - 1] = (byte) 217;
        for (byte[] packet : new byte[][] {null, new byte[0], new byte[12], oversized,
                jpeg(1921, 1), jpeg(1, 1201), jpeg(0, 1), jpeg(1, 0), jpeg(65535, 65535)}) {
            assertThrows(IllegalArgumentException.class, () -> ScreenFrameLimits.dimensions(packet));
        }
    }

    @Test public void jpegDimensionsRejectTruncatedSegmentsInvalidPrecisionAndUnsupportedChannels() {
        byte[] invalidLength = jpeg(32, 18);
        invalidLength[4] = (byte) 255;
        invalidLength[5] = (byte) 255;
        byte[] invalidPrecision = jpeg(32, 18);
        invalidPrecision[6] = 12;
        byte[] invalidChannels = jpeg(32, 18);
        invalidChannels[11] = 4;
        byte[] missingEnd = jpeg(32, 18);
        missingEnd[missingEnd.length - 1] = 0;
        for (byte[] packet : new byte[][] {invalidLength, invalidPrecision, invalidChannels, missingEnd,
                Arrays.copyOf(jpeg(32, 18), 13)}) {
            assertThrows(IllegalArgumentException.class, () -> ScreenFrameLimits.dimensions(packet));
        }
    }

    private static HeldCertificate certificate() {
        return new HeldCertificate.Builder().commonName("RemonteVoice screen test only")
                .addSubjectAlternativeName("localhost").build();
    }

    private static MockWebServer server(HeldCertificate certificate, WebSocketListener listener) throws Exception {
        MockWebServer server = new MockWebServer();
        HandshakeCertificates tls = new HandshakeCertificates.Builder().heldCertificate(certificate).build();
        server.useHttps(tls.sslSocketFactory(), false);
        server.enqueue(new MockResponse().withWebSocketUpgrade(listener));
        server.start(InetAddress.getByName("127.0.0.1"), 0);
        return server;
    }

    private static PairingInfo loopbackPairing(MockWebServer server, String fingerprint) throws Exception {
        Constructor<PairingInfo> constructor = PairingInfo.class.getDeclaredConstructor(
                String.class, int.class, String.class, String.class);
        constructor.setAccessible(true);
        return constructor.newInstance("127.0.0.1", server.getPort(), fingerprint, "12345678");
    }

    private static String fingerprint(HeldCertificate certificate) throws Exception {
        byte[] digest = MessageDigest.getInstance("SHA-256").digest(certificate.certificate().getEncoded());
        StringBuilder result = new StringBuilder();
        for (byte value : digest) result.append(String.format(java.util.Locale.ROOT, "%02X", value & 255));
        return result.toString();
    }

    /** Header-only fixture for transport/parser; Android separately verifies full image decoding. */
    private static byte[] jpeg(int width, int height) {
        ByteBuffer buffer = ByteBuffer.allocate(23);
        buffer.put((byte) 255).put((byte) 216).put((byte) 255).put((byte) 192);
        buffer.putShort((short) 17).put((byte) 8).putShort((short) height).putShort((short) width).put((byte) 3);
        for (int channel = 1; channel <= 3; channel++) buffer.put((byte) channel).put((byte) 17).put((byte) 0);
        buffer.put((byte) 255).put((byte) 217);
        return buffer.array();
    }

    private static final class Observer implements ScreenConnection.Listener {
        final CountDownLatch ready = new CountDownLatch(1);
        final CountDownLatch firstFrame = new CountDownLatch(1);
        final CountDownLatch secondFrame = new CountDownLatch(1);
        final CountDownLatch closed = new CountDownLatch(1);
        final AtomicInteger readyCount = new AtomicInteger();
        final AtomicInteger frameCount = new AtomicInteger();
        final AtomicInteger closeCount = new AtomicInteger();
        final AtomicReference<byte[]> frame = new AtomicReference<>();
        final AtomicReference<String> message = new AtomicReference<>();

        @Override public void onReady() { readyCount.incrementAndGet(); ready.countDown(); }
        @Override public void onFrame(byte[] jpeg) {
            frame.set(jpeg);
            if (frameCount.incrementAndGet() == 1) firstFrame.countDown();
            else secondFrame.countDown();
        }
        @Override public void onClosed(String value) {
            message.set(value);
            closeCount.incrementAndGet();
            closed.countDown();
        }
    }
}
