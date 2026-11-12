package dev.remontevoice.android;

import static org.junit.Assert.assertArrayEquals;
import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertNull;
import static org.junit.Assert.assertThrows;
import static org.junit.Assert.assertTrue;

import java.lang.reflect.Constructor;
import java.lang.reflect.Field;
import java.lang.reflect.Method;
import java.net.InetAddress;
import java.nio.ByteBuffer;
import java.nio.ByteOrder;
import java.security.MessageDigest;
import java.security.cert.CertificateException;
import java.util.ArrayList;
import java.util.Collections;
import java.util.List;
import java.util.Map;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.Executors;
import java.util.concurrent.ScheduledExecutorService;
import java.util.concurrent.ScheduledFuture;
import java.util.concurrent.ScheduledThreadPoolExecutor;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.concurrent.atomic.AtomicLong;
import java.util.function.LongSupplier;
import java.util.concurrent.atomic.AtomicReference;
import okhttp3.Request;
import okhttp3.Response;
import okhttp3.WebSocket;
import okhttp3.WebSocketListener;
import okhttp3.mockwebserver.MockResponse;
import okhttp3.mockwebserver.MockWebServer;
import okhttp3.tls.HandshakeCertificates;
import okhttp3.tls.HeldCertificate;
import okio.ByteString;
import org.junit.Test;

public final class BridgeConnectionTest {
    private static final String TOKEN = "aB_0123456789-abcdefghijkLMNOPQRSTUVWXYZ";

    @Test public void losslessAudioRequiresExactPeerAcknowledgement() throws Exception {
        for(boolean supported:new boolean[]{false,true}) {
            Observer observer=new Observer();FakeWebSocket socket=new FakeWebSocket();
            BridgeConnection connection=connectingConnection(socket,observer);
            try {
                WebSocketListener listener=socketListener(connection);listener.onOpen(socket,null);
                assertEquals(Boolean.TRUE,BridgeConnection.parseControl(socket.controls.get(0)).get("system_deflate"));
                listener.onMessage(socket,authOk(false).replace("}",supported?",\"system_deflate\":true}":"}"));
                assertEquals(TOKEN,observer.token.get());assertTrue(connection.markPlaybackReady());
                listener.onMessage(socket,ByteString.of(LosslessAudioProtocolTest.golden()));
                if(supported){assertFalse(socket.cancelled);assertEquals(960,observer.audio.get().length);}
                else {assertTrue(socket.cancelled);assertNull(observer.audio.get());}
            } finally {connection.disconnect();}
        }
    }

    @Test public void losslessNegotiationRejectsFalseOrNonBooleanFlags() throws Exception {
        for(String value:new String[]{"false","\"true\"","1","null"}) {
            Observer observer=new Observer();FakeWebSocket socket=new FakeWebSocket();
            BridgeConnection connection=connectingConnection(socket,observer);
            try {
                socketListener(connection).onMessage(socket,authOk(false).replace("}",",\"system_deflate\":"+value+"}"));
                assertTrue(socket.cancelled);assertNull(observer.token.get());
            } finally {connection.disconnect();}
        }
    }

    @Test public void receiveTimingIsOptInAndObserverFailureCannotStopDelivery() throws Exception {
        AtomicLong now = new AtomicLong();
        Observer observer = new Observer() {
            @Override public boolean collectAudioTiming() { return true; }
            @Override public void onAudio(float[] samples) {
                super.onAudio(samples);
                now.addAndGet(40_000_000);
            }
            @Override public void onTransportDiagnostic(String event) {
                diagnostics.add(event);
                throw new IllegalStateException("diagnostic observer failed");
            }
        };
        FakeWebSocket socket = new FakeWebSocket();
        BridgeConnection connection = authenticatedConnection(socket, observer, now::get);
        try {
            socketListener(connection).onMessage(socket, ByteString.of(systemFrame()));
            assertTrue(observer.audio.get() != null);
            assertTrue(observer.diagnostics.get(0).contains("callback_ms=40"));
            assertFalse(socket.cancelled);
        } finally { connection.disconnect(); }
        BridgeConnection ordinary = authenticatedConnection(new FakeWebSocket(), new Observer());
        try {
            assertNull(getField(ordinary, "receiveTiming"));
            assertNull(ordinary.readerMaximum());
            assertNull(ordinary.readerWaitSite());
        }
        finally { ordinary.disconnect(); }
    }

    @Test public void lateBinaryCallbackCannotChangeClosedReaderMaximum() throws Exception {
        AtomicLong now = new AtomicLong();
        AtomicInteger delivered = new AtomicInteger();
        Observer observer = new Observer() {
            @Override public boolean collectAudioTiming() { return true; }
            @Override public void onAudio(float[] samples) {
                super.onAudio(samples);
                delivered.incrementAndGet();
            }
        };
        FakeWebSocket socket = new FakeWebSocket();
        BridgeConnection connection = authenticatedConnection(socket, observer, now::get);
        WebSocketListener retained = socketListener(connection);
        try {
            retained.onMessage(socket, ByteString.of(systemFrame()));
            now.set(150_000_000);
            retained.onMessage(socket, ByteString.of(systemFrame()));
            assertEquals(2, delivered.get());
            connection.disconnect();
            String atClose = connection.readerMaximum();
            assertTrue(atClose != null);
            assertTrue(atClose.contains("packet=2"));
            assertTrue(atClose.contains("gap_ns=150000000"));
            now.set(1_000_000_000);
            retained.onMessage(socket, ByteString.of(systemFrame()));
            assertTrue(socket.cancelled);
            assertEquals(2, delivered.get());
            assertEquals(1, observer.closeCount.get());
            assertEquals(atClose, connection.readerMaximum());
        } finally {
            connection.disconnect();
        }
    }

    @Test public void deliversActualReceiveOrdinalsOnlyWhenTimingIsEnabled() throws Exception {
        for (boolean timing : new boolean[] {false, true}) {
            List<Long> packets = new ArrayList<>();
            Observer observer = new Observer() {
                @Override public boolean collectAudioTiming() { return timing; }
                @Override public void onAudio(float[] samples, long packet) {
                    super.onAudio(samples);
                    packets.add(packet);
                }
            };
            FakeWebSocket socket = new FakeWebSocket();
            BridgeConnection connection = authenticatedConnection(socket, observer);
            try {
                for (int i = 0; i < 2; i++)
                    socketListener(connection).onMessage(socket, ByteString.of(systemFrame()));
                assertEquals(Long.valueOf(timing ? 1 : 0), packets.get(0));
                assertEquals(Long.valueOf(timing ? 2 : 0), packets.get(1));
                assertTrue(observer.audio.get() != null);
                assertFalse(socket.cancelled);
            } finally { connection.disconnect(); }
        }
    }

    @Test public void audioReadyNegotiatesWithoutChangingLegacyHostControls() throws Exception {
        for (boolean readySupported : new boolean[] {false, true}) {
            Observer observer = new Observer();
            FakeWebSocket socket = new FakeWebSocket();
            BridgeConnection connection = connectingConnection(socket, observer);
            CapturingScheduler scheduler = (CapturingScheduler) getField(connection, "scheduler");
            try {
                WebSocketListener listener = socketListener(connection);
                listener.onOpen(socket, null);
                assertEquals(Boolean.TRUE, BridgeConnection.parseControl(socket.controls.get(0)).get("audio_ready"));
                socket.controls.clear();
                listener.onMessage(socket, authOk(false).replace("}", readySupported ? ",\"audio_ready\":true}" : "}"));
                assertEquals(TOKEN, observer.token.get());
                assertFalse(connection.setMicrophoneEnabled(true));
                assertFalse(connection.sendMicrophone(new float[480]));
                assertFalse(connection.requestScreen());
                assertEquals(0, socket.controls.size());
                assertTrue(connection.markPlaybackReady());
                assertTrue(connection.markPlaybackReady());
                assertEquals(readySupported ? List.of("{\"type\":\"audio_ready\"}") : List.of(), socket.controls);
                ScheduledFuture<?> deadline = (ScheduledFuture<?>) getField(connection, "playbackReadyDeadline");
                assertTrue(deadline == null || deadline.isCancelled());
                scheduler.screenTimeouts.get(0).run(); // A cancelled callback already dispatched must be inert.
                assertFalse(socket.cancelled);
            } finally { connection.disconnect(); }
        }
    }

    @Test public void playbackReadyDeadlineClosesLegacyAndNegotiatedSessionsBeforeLocalReady() throws Exception {
        for (boolean readySupported : new boolean[] {false, true}) {
            Observer observer = new Observer();
            FakeWebSocket socket = new FakeWebSocket();
            BridgeConnection connection = connectingConnection(socket, observer);
            CapturingScheduler scheduler = (CapturingScheduler) getField(connection, "scheduler");
            try {
                socketListener(connection).onMessage(socket, authOk(false).replace("}",
                        readySupported ? ",\"audio_ready\":true}" : "}"));
                assertEquals(1, scheduler.screenTimeouts.size());
                assertFalse(socket.cancelled);
                scheduler.screenTimeouts.get(0).run();
                assertTrue(socket.cancelled);
                assertEquals(1, observer.closeCount.get());
                assertFalse(connection.markPlaybackReady());
                assertEquals(0, socket.controls.size());
            } finally { connection.disconnect(); }
        }
    }

    @Test public void authReadyAndScreenFlagsAcceptOnlyExactKnownTrueFields() throws Exception {
        for (String fields : new String[] {",\"audio_ready\":true", ",\"screen_detail\":true,\"audio_ready\":true"}) {
            Observer observer = new Observer();
            FakeWebSocket socket = new FakeWebSocket();
            BridgeConnection connection = connectingConnection(socket, observer);
            try {
                socketListener(connection).onMessage(socket, authOk(false).replace("}", fields + "}"));
                assertEquals(TOKEN, observer.token.get());
                assertFalse(socket.cancelled);
                assertTrue(connection.markPlaybackReady());
            } finally { connection.disconnect(); }
        }
        for (String fields : new String[] {",\"audio_ready\":false", ",\"audio_ready\":\"true\"",
                ",\"screen_detail\":false,\"audio_ready\":true", ",\"audio_ready\":true,\"unknown\":true"}) {
            Observer observer = new Observer();
            FakeWebSocket socket = new FakeWebSocket();
            BridgeConnection connection = connectingConnection(socket, observer);
            try {
                socketListener(connection).onMessage(socket, authOk(false).replace("}", fields + "}"));
                assertTrue(socket.cancelled);
                assertNull(observer.token.get());
                assertEquals(1, observer.closeCount.get());
            } finally { connection.disconnect(); }
        }
    }

    @Test public void blockedAudioCallbackCannotHoldPlaybackReadyDeadlineOrReviveClosedSession() throws Exception {
        CountDownLatch entered = new CountDownLatch(1);
        CountDownLatch release = new CountDownLatch(1);
        AtomicInteger callbacks = new AtomicInteger();
        Observer observer = new Observer() {
            @Override public void onAudio(float[] samples) {
                callbacks.incrementAndGet();
                entered.countDown();
                try { assertTrue(release.await(3, TimeUnit.SECONDS)); }
                catch (InterruptedException exception) { Thread.currentThread().interrupt(); }
                throw new IllegalStateException("Late callback failure");
            }
        };
        FakeWebSocket socket = new FakeWebSocket();
        BridgeConnection connection = connectingConnection(socket, observer);
        CapturingScheduler scheduler = (CapturingScheduler) getField(connection, "scheduler");
        WebSocketListener listener = socketListener(connection);
        listener.onMessage(socket, authOk(false));
        Thread callback = new Thread(() -> listener.onMessage(socket, ByteString.of(systemFrame())), "blocked-audio-observer");
        try {
            callback.start();
            assertTrue(entered.await(1, TimeUnit.SECONDS));
            scheduler.screenTimeouts.get(0).run();
            assertTrue(socket.cancelled);
            assertEquals(1, observer.closeCount.get());
            assertFalse(connection.markPlaybackReady());
        } finally {
            release.countDown();
            callback.join(1000);
            connection.disconnect();
        }
        assertFalse(callback.isAlive());
        assertEquals(1, observer.closeCount.get());
        assertEquals(0, socket.controls.size());
        listener.onMessage(socket, ByteString.of(systemFrame()));
        assertEquals(1, observer.closeCount.get());
        assertEquals(1, callbacks.get());
    }

    @Test public void audioCallbackFailureClosesCurrentSessionAndCannotMarkItReady() throws Exception {
        Observer observer = new Observer() {
            @Override public void onAudio(float[] samples) { throw new IllegalStateException("Synthetic observer failure"); }
        };
        FakeWebSocket socket = new FakeWebSocket();
        BridgeConnection connection = connectingConnection(socket, observer);
        try {
            WebSocketListener listener = socketListener(connection);
            listener.onMessage(socket, authOk(false));
            listener.onMessage(socket, ByteString.of(systemFrame()));
            assertTrue(socket.cancelled);
            assertEquals(1, observer.closeCount.get());
            assertFalse(connection.markPlaybackReady());
        } finally { connection.disconnect(); }
    }

    private static BridgeConnection connectingConnection(FakeWebSocket socket, Observer observer) throws Exception {
        BridgeConnection connection = authenticatedConnection(socket, observer);
        Field state = BridgeConnection.class.getDeclaredField("state");
        state.setAccessible(true);
        @SuppressWarnings({"rawtypes", "unchecked"})
        Object connecting = Enum.valueOf((Class<? extends Enum>) state.getType(), "CONNECTING");
        state.set(connection, connecting);
        setField(connection, "playbackReady", false);
        CapturingScheduler scheduler = replaceScheduler(connection);
        setField(connection, "authenticationDeadline", scheduler.schedule(() -> {}, 1, TimeUnit.DAYS));
        return connection;
    }

    private static WebSocketListener socketListener(BridgeConnection connection) throws Exception {
        Class<?> type = Class.forName(BridgeConnection.class.getName() + "$SocketListener");
        Constructor<?> constructor = type.getDeclaredConstructor(BridgeConnection.class, String.class);
        constructor.setAccessible(true);
        return (WebSocketListener) constructor.newInstance(connection, "12345678");
    }

    @Test public void parsesHostControlWithoutAndroidJson() {
        Map<String, Object> control = BridgeConnection.parseControl(
                " {\"type\": \"auth_ok\", \"credential\":\"" + TOKEN
                        + "\", \"microphone_enabled\":false} \n");
        assertEquals("auth_ok", control.get("type"));
        assertEquals(TOKEN, control.get("credential"));
        assertEquals(Boolean.FALSE, control.get("microphone_enabled"));
        assertEquals("pong", BridgeConnection.parseControl("{\"t\\u0079pe\":\"pong\"}").get("type"));
    }

    @Test public void rejectsMalformedAmbiguousAndUnboundedControl() {
        for (String text : new String[] {null, "", "[]", "{}", "{\"type\":true}",
                "{\"type\":\"pong\",\"type\":\"auth_ok\"}",
                "{\"type\":\"pong\",}", "{\"type\":\"pong\"}garbage",
                "{\"type\":\"mic_state\",\"enabled\":1}",
                "{\"type\":\"pong\",\"value\":{}}", "{\"type\":\"po\ng\"}",
                "{\"type\":\"\\x\"}", "{\"type\":\"\\uXXXZ\"}", " ".repeat(4097),
                "{\"type\":\"" + "a".repeat(513) + "\"}"}) {
            assertThrows(IllegalArgumentException.class, () -> BridgeConnection.parseControl(text));
        }
        assertTrue(BridgeConnection.validToken(TOKEN));
        assertFalse(BridgeConnection.validToken("a".repeat(31)));
        assertFalse(BridgeConnection.validToken("a".repeat(257)));
        assertFalse(BridgeConnection.validToken("a".repeat(32) + "\""));
    }

    @Test public void acceptsOnlyPinnedLeafCertificateAndRejectsOtherCertificates() throws Exception {
        HeldCertificate expected = certificate();
        HeldCertificate other = certificate();
        String pin = fingerprint(expected);
        BridgeConnection.PinnedTrustManager trust = new BridgeConnection.PinnedTrustManager(pin);
        trust.checkServerTrusted(new java.security.cert.X509Certificate[] {expected.certificate()}, "EC");
        assertTrue(BridgeConnection.matchesFingerprint(expected.certificate(), pin));
        assertFalse(BridgeConnection.matchesFingerprint(other.certificate(), pin));
        assertThrows(CertificateException.class, () -> trust.checkServerTrusted(
                new java.security.cert.X509Certificate[] {other.certificate(), expected.certificate()}, "EC"));
        assertThrows(CertificateException.class,
                () -> trust.checkServerTrusted(new java.security.cert.X509Certificate[0], "EC"));
        assertThrows(CertificateException.class, () -> trust.checkServerTrusted(null, "EC"));
        assertThrows(CertificateException.class, () -> trust.checkClientTrusted(
                new java.security.cert.X509Certificate[] {expected.certificate()}, "EC"));
        assertEquals(0, trust.getAcceptedIssuers().length);
    }

    @Test public void hostnameVerificationRequiresExactPairingIpAndLeafPin() throws Exception {
        HeldCertificate expected = certificate();
        HeldCertificate other = certificate();
        PairingInfo pairing = PairingInfo.parse("remontevoice://pair?host=10.1.2.3&port=8765&fingerprint="
                + fingerprint(expected) + "&pin=12345678");
        javax.net.ssl.SSLSession expectedSession = session(expected);
        assertTrue(BridgeConnection.verifyHostname(pairing, "10.1.2.3", expectedSession));
        assertFalse(BridgeConnection.verifyHostname(pairing, "10.1.2.4", expectedSession));
        assertFalse(BridgeConnection.verifyHostname(pairing, "remontevoice.local", expectedSession));
        assertFalse(BridgeConnection.verifyHostname(pairing, "10.1.2.3", session(other)));
        assertFalse(BridgeConnection.verifyHostname(pairing, "10.1.2.3", null));
    }

    @Test public void expiredCertificateIsRejectedEvenWhenItsPinMatches() throws Exception {
        long yesterday = System.currentTimeMillis() - TimeUnit.DAYS.toMillis(1);
        HeldCertificate expired = new HeldCertificate.Builder().commonName("Expired test only")
                .validityInterval(yesterday - TimeUnit.DAYS.toMillis(1), yesterday).build();
        BridgeConnection.PinnedTrustManager trust = new BridgeConnection.PinnedTrustManager(fingerprint(expired));
        assertThrows(CertificateException.class, () -> trust.checkServerTrusted(
                new java.security.cert.X509Certificate[] {expired.certificate()}, "EC"));
    }

    @Test public void pinnedTlsAuthenticatesExchangesAudioAndAcknowledgesMute() throws Exception {
        HeldCertificate certificate = certificate();
        Observer observer = new Observer();
        AtomicReference<String> firstMessage = new AtomicReference<>();
        AtomicReference<ByteString> microphoneFrame = new AtomicReference<>();
        CountDownLatch microphoneReceived = new CountDownLatch(1);
        try (MockWebServer server = server(certificate)) {
            server.enqueue(new MockResponse().withWebSocketUpgrade(new WebSocketListener() {
                @Override public void onMessage(WebSocket socket, String text) {
                    Map<String, Object> control = BridgeConnection.parseControl(text);
                    if (firstMessage.compareAndSet(null, text)) {
                        socket.send(authOk(false));
                        socket.send(ByteString.of(systemFrame()));
                    } else if ("mic_enabled".equals(control.get("type"))) {
                        socket.send("{\"type\":\"mic_state\",\"enabled\":" + control.get("enabled") + "}");
                    }
                }

                @Override public void onMessage(WebSocket socket, ByteString bytes) {
                    microphoneFrame.set(bytes);
                    microphoneReceived.countDown();
                }
            }));
            BridgeConnection connection = new BridgeConnection(loopbackPairing(server, fingerprint(certificate)), observer);
            try {
                assertFalse(connection.sendMicrophone(new float[480]));
                connection.connect("12345678");
                assertTrue("authentication", observer.authenticated.await(5, TimeUnit.SECONDS));
                assertTrue(connection.markPlaybackReady());
                assertTrue("system audio", observer.audioReceived.await(5, TimeUnit.SECONDS));
                assertEquals(TOKEN, observer.token.get());
                assertEquals("auth", BridgeConnection.parseControl(firstMessage.get()).get("type"));
                assertEquals("12345678", BridgeConnection.parseControl(firstMessage.get()).get("credential"));
                assertArrayEquals(AudioProtocol.decodeSystem(systemFrame()), observer.audio.get(), 0.0f);
                assertFalse(connection.sendMicrophone(new float[480]));
                assertTrue(connection.setMicrophoneEnabled(true));
                assertTrue("mic acknowledgement", observer.microphoneOn.await(5, TimeUnit.SECONDS));
                float[] samples = new float[480];
                samples[0] = 0.25f;
                assertTrue(connection.sendMicrophone(samples));
                assertTrue(microphoneReceived.await(5, TimeUnit.SECONDS));
                assertArrayEquals(AudioProtocol.encodeMicrophone(samples), microphoneFrame.get().toByteArray());
                assertTrue(connection.setMicrophoneEnabled(false));
                assertFalse(connection.sendMicrophone(samples));
                connection.disconnect();
                connection.disconnect();
                assertTrue(observer.closed.await(5, TimeUnit.SECONDS));
                assertEquals(1, observer.closeCount.get());
            } finally {
                connection.disconnect();
            }
        }
    }

    @Test public void wrongPinRejectsTlsBeforeCredentialTransmission() throws Exception {
        HeldCertificate certificate = certificate();
        Observer observer = new Observer();
        AtomicInteger serverMessages = new AtomicInteger();
        try (MockWebServer server = server(certificate)) {
            server.enqueue(new MockResponse().withWebSocketUpgrade(new WebSocketListener() {
                @Override public void onMessage(WebSocket socket, String text) {
                    serverMessages.incrementAndGet();
                }
            }));
            BridgeConnection connection = new BridgeConnection(loopbackPairing(server, "0".repeat(64)), observer);
            try {
                connection.connect("12345678");
                assertTrue(observer.closed.await(5, TimeUnit.SECONDS));
                assertEquals(0, serverMessages.get());
                assertNull(observer.token.get());
                assertFalse(observer.message.get().contains("12345678"));
                assertEquals(1, observer.closeCount.get());
            } finally {
                connection.disconnect();
            }
        }
    }

    @Test public void rejectsAudioBeforeAuthentication() throws Exception {
        assertRejectedHost(new WebSocketListener() {
            @Override public void onOpen(WebSocket socket, Response response) {
                socket.send(ByteString.of(systemFrame()));
            }
        });
    }

    @Test public void requiresInitiallyMutedAuthenticationAndBoundedToken() throws Exception {
        for (String response : new String[] {authOk(true), authOk(false).replace(TOKEN, "short"),
                "{\"type\":\"auth_error\"}", "{\"type\":\"auth_ok\",\"credential\":\"" + TOKEN + "\"}"}) {
            assertRejectedHost(new WebSocketListener() {
                @Override public void onMessage(WebSocket socket, String text) {
                    socket.send(response);
                }
            });
        }
    }

    @Test public void silentHostHitsAuthenticationDeadline() throws Exception {
        HeldCertificate certificate = certificate();
        Observer observer = new Observer();
        CountDownLatch authenticationRequest = new CountDownLatch(1);
        try (MockWebServer server = server(certificate)) {
            server.enqueue(new MockResponse().withWebSocketUpgrade(new WebSocketListener() {
                @Override public void onMessage(WebSocket socket, String text) {
                    authenticationRequest.countDown();
                }
            }));
            BridgeConnection connection = new BridgeConnection(loopbackPairing(server, fingerprint(certificate)), observer);
            try {
                connection.connect("12345678");
                assertTrue(authenticationRequest.await(5, TimeUnit.SECONDS));
                assertTrue(observer.closed.await(10, TimeUnit.SECONDS));
                assertNull(observer.token.get());
                assertEquals(1, observer.closeCount.get());
                assertEquals("Ноутбук не подтвердил подключение. Проверьте сеть и сопряжение.", observer.message.get());
            } finally {
                connection.disconnect();
            }
        }
    }

    @Test public void boundedQueueDropsCaptureAndRefillsAsSoonAsThereIsSpace() throws Exception {
        Observer observer = new Observer();
        FakeWebSocket socket = new FakeWebSocket();
        BridgeConnection connection = authenticatedConnection(socket, observer);
        try {
            fillMicrophoneQueue(connection);
            assertFalse(connection.sendMicrophone(new float[480]));
            assertEquals(10, socket.audio.size());
            socket.queued -= 1921; // One whole frame written, nine are still pending.
            assertTrue(connection.sendMicrophone(new float[480]));
            assertEquals(11, socket.audio.size());
            assertEquals(19210, socket.queued);
            socket.queued = 1921;
            assertTrue(connection.setMicrophoneEnabled(false));
            assertFalse(socket.cancelled);
            assertEquals(Collections.singletonList("{\"type\":\"mic_enabled\",\"enabled\":false}"), socket.controls);
            assertFalse(connection.sendMicrophone(new float[480]));
        } finally {
            connection.disconnect();
        }
    }

    @Test public void muteCancelsOverflowOrProvenStaleAudio() throws Exception {
        for (boolean overflow : new boolean[] {true, false}) {
            Observer observer = new Observer();
            FakeWebSocket socket = new FakeWebSocket();
            AtomicLong now = new AtomicLong(1);
            BridgeConnection connection = authenticatedConnection(socket, observer, now::get);
            try {
                assertTrue(connection.sendMicrophone(new float[480]));
                if (overflow) socket.queued = 19211;
                else now.addAndGet(TimeUnit.SECONDS.toNanos(1));
                assertFalse(connection.setMicrophoneEnabled(false));
                assertTrue(socket.cancelled);
                assertEquals(0, socket.controls.size());
                assertEquals(1, observer.closeCount.get());
            } finally {
                connection.disconnect();
            }
        }
    }

    @Test public void drainedAudioDoesNotCancelMuteAfterOldDeadline() throws Exception {
        Observer observer = new Observer();
        FakeWebSocket socket = new FakeWebSocket();
        AtomicLong now = new AtomicLong(1);
        BridgeConnection connection = authenticatedConnection(socket, observer, now::get);
        try {
            assertTrue(connection.sendMicrophone(new float[480]));
            ScheduledFuture<?> deadline = (ScheduledFuture<?>) getField(connection, "backlogDeadline");
            now.addAndGet(TimeUnit.SECONDS.toNanos(1));
            socket.queued = 0;
            assertTrue(connection.setMicrophoneEnabled(false));
            assertTrue(deadline.isCancelled());
            assertFalse(socket.cancelled);
            assertEquals(0, observer.closeCount.get());
        } finally {
            connection.disconnect();
        }
    }

    @Test public void sustainedProgressDoesNotRequireEmptyQueueOrResetOldFrameAge() throws Exception {
        Observer observer = new Observer();
        FakeWebSocket socket = new FakeWebSocket();
        AtomicLong now = new AtomicLong(1);
        BridgeConnection connection = authenticatedConnection(socket, observer, now::get);
        try {
            fillMicrophoneQueue(connection);
            for (int i = 0; i < 60; i++) {
                now.addAndGet(TimeUnit.MILLISECONDS.toNanos(20));
                socket.queued -= 1921;
                assertTrue(connection.sendMicrophone(new float[480]));
                assertFalse(connection.sendMicrophone(new float[480]));
                assertEquals(19210, socket.queued);
                assertFalse(socket.cancelled);
            }
            assertEquals(70, socket.audio.size());
            assertEquals(0, observer.closeCount.get());
        } finally {
            connection.disconnect();
        }
    }

    @Test public void deliveredOldFrameDoesNotExpireFreshRemainingFrame() throws Exception {
        Observer observer = new Observer();
        FakeWebSocket socket = new FakeWebSocket();
        AtomicLong now = new AtomicLong(1);
        BridgeConnection connection = authenticatedConnection(socket, observer, now::get);
        try {
            assertTrue(connection.sendMicrophone(new float[480]));
            ScheduledFuture<?> first = (ScheduledFuture<?>) getField(connection, "backlogDeadline");
            now.addAndGet(TimeUnit.MILLISECONDS.toNanos(200));
            assertTrue(connection.sendMicrophone(new float[480]));
            socket.queued = 1921;
            now.addAndGet(TimeUnit.MILLISECONDS.toNanos(350));
            assertTrue(connection.sendMicrophone(new float[480]));
            assertTrue(first.isCancelled());
            assertFalse(socket.cancelled);
            assertEquals(3, socket.audio.size());
        } finally {
            connection.disconnect();
        }
    }

    @Test public void stalePartialQueueClosesBeforeRefillingOrSendingMute() throws Exception {
        Observer observer = new Observer();
        FakeWebSocket socket = new FakeWebSocket();
        AtomicLong now = new AtomicLong(1);
        BridgeConnection connection = authenticatedConnection(socket, observer, now::get);
        try {
            fillMicrophoneQueue(connection);
            socket.queued = 1921;
            now.addAndGet(TimeUnit.SECONDS.toNanos(1));
            assertFalse(connection.sendMicrophone(new float[480]));
            assertFalse(connection.setMicrophoneEnabled(false));
            assertTrue(socket.cancelled);
            assertEquals(10, socket.audio.size());
            assertEquals(0, socket.controls.size());
            assertEquals(1, observer.closeCount.get());
        } finally {
            connection.disconnect();
        }
    }

    @Test public void watchdogClosesEvenOneStaleFrameWithoutAnotherMicrophoneRead() throws Exception {
        Observer observer = new Observer();
        FakeWebSocket socket = new FakeWebSocket();
        BridgeConnection connection = authenticatedConnection(socket, observer);
        ScheduledExecutorService scheduler = (ScheduledExecutorService) getField(connection, "scheduler");
        try {
            long startedAt = System.nanoTime();
            assertTrue(connection.sendMicrophone(new float[480]));
            assertTrue(observer.closed.await(3, TimeUnit.SECONDS));
            assertTrue(System.nanoTime() - startedAt >= TimeUnit.MILLISECONDS.toNanos(500));
            assertTrue(socket.cancelled);
            assertEquals(1, socket.audio.size());
            assertEquals(1, observer.closeCount.get());
            assertTrue(scheduler.isShutdown());
        } finally {
            connection.disconnect();
        }
    }

    @Test public void disconnectCancelsPendingAudioWatchdog() throws Exception {
        Observer observer = new Observer();
        FakeWebSocket socket = new FakeWebSocket();
        BridgeConnection connection = authenticatedConnection(socket, observer);
        ScheduledExecutorService scheduler = (ScheduledExecutorService) getField(connection, "scheduler");
        try {
            assertTrue(connection.sendMicrophone(new float[480]));
            ScheduledFuture<?> deadline = (ScheduledFuture<?>) getField(connection, "backlogDeadline");
            connection.disconnect();
            assertTrue(deadline.isCancelled());
            assertTrue(scheduler.isShutdown());
            assertEquals(1, observer.closeCount.get());
            assertEquals(0L, getField(connection, "outgoingBytes"));
        } finally {
            connection.disconnect();
        }
    }

    @Test public void watchdogKeepsSessionWhenWriterDrainsWithoutAnotherCapture() throws Exception {
        Observer observer = new Observer();
        FakeWebSocket socket = new FakeWebSocket();
        BridgeConnection connection = authenticatedConnection(socket, observer);
        try {
            assertTrue(connection.sendMicrophone(new float[480]));
            socket.queued = 0;
            assertFalse(observer.closed.await(750, TimeUnit.MILLISECONDS));
            assertFalse(socket.cancelled);
            assertEquals(0L, getField(connection, "outgoingBytes"));
            assertNull(getField(connection, "backlogDeadline"));
        } finally {
            connection.disconnect();
        }
    }

    @Test public void writtenControlCannotBeMistakenForWrittenAudio() throws Exception {
        Observer observer = new Observer();
        FakeWebSocket socket = new FakeWebSocket();
        AtomicLong now = new AtomicLong(1);
        BridgeConnection connection = authenticatedConnection(socket, observer, now::get);
        try {
            assertTrue(connection.requestScreen());
            assertTrue(connection.sendMicrophone(new float[480]));
            socket.queued = 1921; // Only the screen request was written.
            now.addAndGet(TimeUnit.MILLISECONDS.toNanos(500));
            assertFalse(connection.sendMicrophone(new float[480]));
            assertTrue(socket.cancelled);
            assertEquals(1, socket.audio.size());
        } finally {
            connection.disconnect();
        }
    }

    @Test public void oldControlAfterWrittenAudioDoesNotExpireFreshAudio() throws Exception {
        Observer observer = new Observer();
        FakeWebSocket socket = new FakeWebSocket();
        AtomicLong now = new AtomicLong(1);
        BridgeConnection connection = authenticatedConnection(socket, observer, now::get);
        try {
            assertTrue(connection.sendMicrophone(new float[480]));
            assertTrue(connection.requestScreen());
            socket.queued = socket.controls.get(0).length(); // Audio was written, control remains.
            now.addAndGet(TimeUnit.SECONDS.toNanos(1));
            assertTrue(connection.sendMicrophone(new float[480]));
            assertFalse(socket.cancelled);
            assertEquals(2, socket.audio.size());
        } finally {
            connection.disconnect();
        }
    }

    private static void fillMicrophoneQueue(BridgeConnection connection) {
        for (int i = 0; i < 10; i++) assertTrue(connection.sendMicrophone(new float[480]));
    }

    @Test public void screenTimeoutRetryIgnoresLateOfferUntilCurrentResponse() throws Exception {
        Observer observer = new Observer();
        FakeWebSocket socket = new FakeWebSocket();
        BridgeConnection connection = authenticatedConnection(socket, observer);
        CapturingScheduler scheduler = replaceScheduler(connection);
        try {
            setField(connection, "screenRequestEpoch", 100L);
            assertTrue(connection.requestScreen(101));
            assertEquals("101", BridgeConnection.parseControl(socket.controls.get(0)).get("request_id"));
            ScheduledFuture<?> firstDeadline = (ScheduledFuture<?>) getField(connection, "screenDeadline");
            firstDeadline.cancel(false);
            scheduler.screenTimeouts.get(0).run(); // Actual timeout callback for request 101.
            assertEquals(1, observer.screenErrorCount.get());
            assertEquals(101, observer.screenRequest.get());
            assertTrue(connection.requestScreen(102));
            ScheduledFuture<?> retryDeadline = (ScheduledFuture<?>) getField(connection, "screenDeadline");
            scheduler.screenTimeouts.get(0).run(); // Already-dispatched timeout from request 101.
            receiveScreen(connection, Map.of("type", "screen_offer", "request_id", "101", "ticket", TOKEN));
            assertTrue((Boolean) getField(connection, "screenPending"));
            assertEquals(retryDeadline, getField(connection, "screenDeadline"));
            assertFalse(retryDeadline.isCancelled());
            assertEquals(0, observer.screenOfferCount.get());
            assertEquals(1, observer.screenErrorCount.get());
            assertFalse(connection.requestScreen(103));
            String currentTicket = TOKEN + "fresh";
            receiveScreen(connection, Map.of("type", "screen_offer", "request_id", "102", "ticket", currentTicket));
            assertEquals(102, observer.screenRequest.get());
            assertEquals(currentTicket, observer.screenTicket.get());
            assertEquals(1, observer.screenOfferCount.get());
            assertFalse((Boolean) getField(connection, "screenPending"));
            assertTrue(retryDeadline.isCancelled());
            receiveScreen(connection, Map.of("type", "screen_offer", "request_id", "102", "ticket", currentTicket));
            assertEquals(1, observer.screenOfferCount.get());
            assertFalse(socket.cancelled);
            assertTrue(connection.sendMicrophone(new float[480]));
        } finally {
            connection.disconnect();
        }
    }

    @Test public void unmatchedScreenErrorOrMissingIdCannotConsumePendingRequest() throws Exception {
        Observer observer = new Observer();
        FakeWebSocket socket = new FakeWebSocket();
        BridgeConnection connection = authenticatedConnection(socket, observer);
        try {
            assertTrue(connection.requestScreen(77));
            ScheduledFuture<?> deadline = (ScheduledFuture<?>) getField(connection, "screenDeadline");
            for (Map<String, Object> response : List.<Map<String, Object>>of(
                    Map.of("type", "screen_error", "request_id", "0", "message", "capture_failed"),
                    Map.of("type", "screen_error", "request_id", "01", "message", "capture_failed"),
                    Map.of("type", "screen_error", "request_id", true, "message", "capture_failed"),
                    Map.of("type", "screen_error", "request_id", 1, "message", "capture_failed"),
                    Map.of("type", "screen_error", "message", "capture_failed"),
                    Map.of("type", "screen_offer", "ticket", TOKEN))) {
                receiveScreen(connection, response);
                assertTrue((Boolean) getField(connection, "screenPending"));
                assertEquals(deadline, getField(connection, "screenDeadline"));
                assertFalse(deadline.isCancelled());
                assertEquals(0, observer.screenErrorCount.get());
                assertEquals(0, observer.screenOfferCount.get());
            }
            receiveScreen(connection, Map.of("type", "screen_error", "request_id", "1", "message", "capture_failed"));
            assertEquals(1, observer.screenErrorCount.get());
            assertEquals(77, observer.screenRequest.get());
            assertEquals(ScreenConnection.errorMessage("capture_failed"), observer.screenMessage.get());
            assertFalse((Boolean) getField(connection, "screenPending"));
            assertTrue(deadline.isCancelled());
            assertFalse(socket.cancelled);
        } finally {
            connection.disconnect();
        }
    }

    @Test public void legacyScreenResponseTimesOutOnlyPreviewAndLeavesAudioWorking() throws Exception {
        Observer observer = new Observer();
        FakeWebSocket socket = new FakeWebSocket();
        BridgeConnection connection = authenticatedConnection(socket, observer);
        CapturingScheduler scheduler = replaceScheduler(connection);
        try {
            assertTrue(connection.requestScreen(7));
            receiveScreen(connection, Map.of("type", "screen_offer", "ticket", TOKEN));
            ScheduledFuture<?> deadline = (ScheduledFuture<?>) getField(connection, "screenDeadline");
            assertTrue((Boolean) getField(connection, "screenPending"));
            assertFalse(deadline.isCancelled());
            deadline.cancel(false);
            scheduler.screenTimeouts.get(0).run();
            assertEquals(1, observer.screenErrorCount.get());
            assertEquals(7, observer.screenRequest.get());
            assertEquals(0, observer.closeCount.get());
            assertFalse(socket.cancelled);
            assertTrue(connection.sendMicrophone(new float[480]));
            assertTrue(connection.requestScreen(8));
        } finally {
            connection.disconnect();
        }
    }

    @Test public void malformedMatchingScreenResponseFailsOnlyItsOwnPreview() throws Exception {
        for (Map<String, Object> response : List.<Map<String, Object>>of(
                Map.of("type", "screen_offer", "request_id", "1", "ticket", "short"),
                Map.of("type", "screen_offer", "request_id", "1", "ticket", TOKEN, "extra", true),
                Map.of("type", "screen_offer", "request_id", "1", "message", "capture_failed"),
                Map.of("type", "screen_error", "request_id", "1", "message", true))) {
            Observer observer = new Observer();
            FakeWebSocket socket = new FakeWebSocket();
            BridgeConnection connection = authenticatedConnection(socket, observer);
            try {
                assertTrue(connection.requestScreen(66));
                receiveScreen(connection, response);
                assertEquals(1, observer.screenErrorCount.get());
                assertEquals(66, observer.screenRequest.get());
                assertEquals(ScreenConnection.errorMessage("invalid_request"), observer.screenMessage.get());
                assertFalse((Boolean) getField(connection, "screenPending"));
                assertEquals(0, observer.screenOfferCount.get());
                assertEquals(0, observer.closeCount.get());
                assertFalse(socket.cancelled);
            } finally {
                connection.disconnect();
            }
        }
    }

    @Test public void pinnedTlsScreenRequestAndResponseUseExactStringId() throws Exception {
        HeldCertificate certificate = certificate();
        Observer observer = new Observer();
        AtomicReference<String> requestId = new AtomicReference<>();
        try (MockWebServer server = server(certificate)) {
            server.enqueue(new MockResponse().withWebSocketUpgrade(new WebSocketListener() {
                @Override public void onMessage(WebSocket socket, String text) {
                    Map<String, Object> control = BridgeConnection.parseControl(text);
                    if ("auth".equals(control.get("type"))) socket.send(authOk(false));
                    else if ("screen_request".equals(control.get("type"))) {
                        assertEquals(2, control.size());
                        requestId.set((String) control.get("request_id"));
                        socket.send("{\"type\":\"screen_offer\",\"request_id\":\"" + requestId.get()
                                + "\",\"ticket\":\"" + TOKEN + "\"}");
                    }
                }
            }));
            BridgeConnection connection = new BridgeConnection(loopbackPairing(server, fingerprint(certificate)), observer);
            try {
                connection.connect("12345678");
                assertTrue(observer.authenticated.await(5, TimeUnit.SECONDS));
                assertTrue(connection.markPlaybackReady());
                assertTrue(connection.requestScreen(999));
                assertTrue(observer.screenOffered.await(5, TimeUnit.SECONDS));
                assertEquals("1", requestId.get());
                assertEquals(999, observer.screenRequest.get());
                assertEquals(TOKEN, observer.screenTicket.get());
                assertEquals(0, observer.closeCount.get());
            } finally {
                connection.disconnect();
            }
        }
    }

    @Test public void unsolicitedOrIncompleteVideoAcknowledgementIsRejected() throws Exception {
        for (boolean requested : new boolean[]{false, true}) {
            HeldCertificate certificate = certificate();
            Observer observer = new Observer();
            try (MockWebServer server = server(certificate)) {
                server.enqueue(new MockResponse().withWebSocketUpgrade(new WebSocketListener() {
                    @Override public void onMessage(WebSocket socket, String text) {
                        if ("auth".equals(BridgeConnection.parseControl(text).get("type"))) {
                            socket.send(authOk(false).replace("}", (requested ? "" : ",\"screen_detail\":true")
                                    + ",\"screen_video\":true}"));
                        }
                    }
                }));
                BridgeConnection connection = new BridgeConnection(loopbackPairing(server, fingerprint(certificate)), observer, requested);
                try {
                    connection.connect("12345678");
                    assertTrue(observer.closed.await(5, TimeUnit.SECONDS));
                    assertEquals(1, observer.authenticated.getCount());
                    assertFalse(connection.requestScreen(1));
                } finally { connection.disconnect(); }
            }
        }
    }

    @Test public void videoSelectionRequiresHostAcknowledgementAndPreservesOldHostFallback() throws Exception {
        for (boolean acknowledged : new boolean[]{false, true}) {
            HeldCertificate certificate = certificate();
            Observer observer = new Observer();
            AtomicReference<Boolean> offeredVideo = new AtomicReference<>();
            CountDownLatch offered = new CountDownLatch(1);
            BridgeConnection.Listener listener = new BridgeConnection.Listener() {
                public void onAuthenticated(String token) { observer.onAuthenticated(token); }
                public void onAudio(float[] samples) { observer.onAudio(samples); }
                public void onMicState(boolean enabled) { observer.onMicState(enabled); }
                public void onClosed(String message) { observer.onClosed(message); }
                public void onScreenOffer(long request, String ticket, boolean video) {
                    assertEquals(101, request); assertEquals(TOKEN, ticket);
                    offeredVideo.set(video); offered.countDown();
                }
            };
            try (MockWebServer server = server(certificate)) {
                server.enqueue(new MockResponse().withWebSocketUpgrade(new WebSocketListener() {
                    @Override public void onMessage(WebSocket socket, String text) {
                        Map<String,Object> control = BridgeConnection.parseControl(text);
                        if ("auth".equals(control.get("type"))) {
                            assertEquals(Boolean.TRUE, control.get("screen_video"));
                            socket.send(authOk(false).replace("}", ",\"screen_detail\":true"
                                    + (acknowledged ? ",\"screen_video\":true" : "") + "}"));
                        } else if ("screen_request".equals(control.get("type"))) {
                            assertEquals(acknowledged ? "video" : "detail", control.get("profile"));
                            socket.send("{\"type\":\"screen_offer\",\"request_id\":\"" + control.get("request_id")
                                    + "\",\"ticket\":\"" + TOKEN + "\"}");
                        }
                    }
                }));
                BridgeConnection connection = new BridgeConnection(loopbackPairing(server, fingerprint(certificate)), listener, true);
                try {
                    connection.connect("12345678");
                    assertTrue(observer.authenticated.await(5, TimeUnit.SECONDS));
                    assertTrue(connection.markPlaybackReady());
                    assertTrue(connection.requestScreen(101));
                    assertTrue(offered.await(5, TimeUnit.SECONDS));
                    assertEquals(acknowledged, offeredVideo.get());
                    assertEquals(0, observer.closeCount.get());
                } finally { connection.disconnect(); }
            }
        }
    }

    @Test public void detailNegotiationSelectsProfileOnlyAfterHostAcknowledges() throws Exception {
        HeldCertificate certificate = certificate();
        Observer observer = new Observer();
        AtomicReference<String> requestId = new AtomicReference<>();
        try (MockWebServer server = server(certificate)) {
            server.enqueue(new MockResponse().withWebSocketUpgrade(new WebSocketListener() {
                @Override public void onMessage(WebSocket socket, String text) {
                    Map<String, Object> control = BridgeConnection.parseControl(text);
                    if ("auth".equals(control.get("type"))) {
                        assertEquals(Boolean.TRUE,control.get("screen_detail"));
                        socket.send(authOk(false).replace("}",",\"screen_detail\":true}"));
                    }
                    else if ("screen_request".equals(control.get("type"))) {
                        assertEquals(3, control.size());
                        assertEquals("detail",control.get("profile"));
                        requestId.set((String) control.get("request_id"));
                        socket.send("{\"type\":\"screen_offer\",\"request_id\":\"" + requestId.get()
                                + "\",\"ticket\":\"" + TOKEN + "\"}");
                    }
                }
            }));
            BridgeConnection connection = new BridgeConnection(loopbackPairing(server, fingerprint(certificate)), observer);
            try {
                connection.connect("12345678");
                assertTrue(observer.authenticated.await(5, TimeUnit.SECONDS));
                assertTrue(connection.markPlaybackReady());
                assertTrue(connection.requestScreen(999));
                assertTrue(observer.screenOffered.await(5, TimeUnit.SECONDS));
                assertEquals("1", requestId.get());
                assertEquals(999, observer.screenRequest.get());
                assertEquals(TOKEN, observer.screenTicket.get());
                assertEquals(0, observer.closeCount.get());
            } finally {
                connection.disconnect();
            }
        }
    }

    private static void receiveScreen(BridgeConnection connection, Map<String, Object> control) throws Exception {
        Method method = BridgeConnection.class.getDeclaredMethod("receiveScreenControl", Map.class);
        method.setAccessible(true);
        method.invoke(connection, control);
    }

    private static CapturingScheduler replaceScheduler(BridgeConnection connection) throws Exception {
        ((ScheduledExecutorService) getField(connection, "scheduler")).shutdownNow();
        CapturingScheduler replacement = new CapturingScheduler();
        setField(connection, "scheduler", replacement);
        return replacement;
    }

    private static final class CapturingScheduler extends ScheduledThreadPoolExecutor {
        final List<Runnable> screenTimeouts = new ArrayList<>();
        CapturingScheduler() {
            super(1, task -> {
                Thread thread = new Thread(task, "remontevoice-test-screen-timeout");
                thread.setDaemon(true);
                return thread;
            });
        }
        @Override public ScheduledFuture<?> schedule(Runnable command, long delay, TimeUnit unit) {
            if (delay == 8 && unit == TimeUnit.SECONDS) screenTimeouts.add(command);
            return super.schedule(command, delay, unit);
        }
    }

    @Test public void doesNotSendMicrophoneUntilHostAcknowledgesRequest() throws Exception {
        Observer observer = new Observer();
        FakeWebSocket socket = new FakeWebSocket();
        BridgeConnection connection = authenticatedConnection(socket, observer);
        try {
            assertTrue(connection.setMicrophoneEnabled(true));
            assertFalse(connection.sendMicrophone(new float[480]));
            assertEquals(Collections.singletonList("{\"type\":\"mic_enabled\",\"enabled\":true}"), socket.controls);
            assertEquals(0, socket.audio.size());
        } finally {
            connection.disconnect();
        }
    }

    @Test public void muteRequestDoesNotMasqueradeAsHostAcknowledgement() throws Exception {
        Observer observer = new Observer();
        FakeWebSocket socket = new FakeWebSocket();
        BridgeConnection connection = authenticatedConnection(socket, observer);
        try {
            assertTrue(connection.setMicrophoneEnabled(false));
            assertEquals(0, observer.microphoneStateCount.get());
            assertFalse(connection.sendMicrophone(new float[480]));
            assertEquals(Collections.singletonList("{\"type\":\"mic_enabled\",\"enabled\":false}"), socket.controls);
        } finally {
            connection.disconnect();
        }
    }

    private static void assertRejectedHost(WebSocketListener listener) throws Exception {
        HeldCertificate certificate = certificate();
        Observer observer = new Observer();
        try (MockWebServer server = server(certificate)) {
            server.enqueue(new MockResponse().withWebSocketUpgrade(listener));
            BridgeConnection connection = new BridgeConnection(loopbackPairing(server, fingerprint(certificate)), observer);
            try {
                connection.connect("12345678");
                assertTrue(observer.closed.await(5, TimeUnit.SECONDS));
                assertNull(observer.token.get());
                assertNull(observer.audio.get());
                assertEquals(1, observer.closeCount.get());
            } finally {
                connection.disconnect();
            }
        }
    }

    @Test public void diagnosticNeverExposesRemoteReasonOrPreventsClosure() throws Exception {
        for (boolean throwing : new boolean[] {false, true}) {
            HeldCertificate certificate = certificate();
            Observer observer = new Observer();
            observer.throwDiagnostic = throwing;
            try (MockWebServer server = server(certificate)) {
                server.enqueue(new MockResponse().withWebSocketUpgrade(new WebSocketListener() {
                    @Override public void onMessage(WebSocket socket, String text) {
                        socket.close(1011, "private remote reason " + TOKEN);
                    }
                }));
                BridgeConnection connection = new BridgeConnection(loopbackPairing(server, fingerprint(certificate)), observer);
                try {
                    connection.connect("12345678");
                    assertTrue(observer.closed.await(5, TimeUnit.SECONDS));
                    assertEquals(1, observer.closeCount.get());
                    assertEquals(List.of("close_code=1011"), observer.diagnostics);
                } finally { connection.disconnect(); }
            }
        }
    }

    private static HeldCertificate certificate() {
        return new HeldCertificate.Builder().commonName("RemonteVoice test only")
                .addSubjectAlternativeName("localhost").build();
    }

    private static javax.net.ssl.SSLSession session(HeldCertificate certificate) {
        return (javax.net.ssl.SSLSession) java.lang.reflect.Proxy.newProxyInstance(
                BridgeConnectionTest.class.getClassLoader(), new Class<?>[] {javax.net.ssl.SSLSession.class},
                (proxy, method, arguments) -> {
                    if (method.getName().equals("getPeerCertificates")) {
                        return new java.security.cert.Certificate[] {certificate.certificate()};
                    }
                    return null;
                });
    }

    private static MockWebServer server(HeldCertificate certificate) throws Exception {
        MockWebServer server = new MockWebServer();
        HandshakeCertificates tls = new HandshakeCertificates.Builder().heldCertificate(certificate).build();
        server.useHttps(tls.sslSocketFactory(), false);
        server.start(InetAddress.getByName("127.0.0.1"), 0);
        return server;
    }

    private static PairingInfo loopbackPairing(MockWebServer server, String fingerprint) throws Exception {
        // Production parsing keeps rejecting loopback; only the local JVM TLS fixture bypasses construction.
        Constructor<PairingInfo> constructor = PairingInfo.class.getDeclaredConstructor(
                String.class, int.class, String.class, String.class);
        constructor.setAccessible(true);
        return constructor.newInstance("127.0.0.1", server.getPort(), fingerprint, "12345678");
    }

    private static String fingerprint(HeldCertificate certificate) throws Exception {
        byte[] digest = MessageDigest.getInstance("SHA-256").digest(certificate.certificate().getEncoded());
        StringBuilder result = new StringBuilder();
        for (byte value : digest) {
            result.append(String.format(java.util.Locale.ROOT, "%02X", value & 0xff));
        }
        return result.toString();
    }

    private static String authOk(boolean microphoneEnabled) {
        return "{\"type\":\"auth_ok\",\"credential\":\"" + TOKEN
                + "\",\"microphone_enabled\":" + microphoneEnabled + "}";
    }

    private static byte[] systemFrame() {
        ByteBuffer packet = ByteBuffer.allocate(3841).order(ByteOrder.LITTLE_ENDIAN);
        packet.put((byte) 2);
        for (int i = 0; i < 960; i++) {
            packet.putFloat(i % 2 == 0 ? 0.25f : -0.25f);
        }
        return packet.array();
    }

    private static BridgeConnection authenticatedConnection(FakeWebSocket socket, Observer observer) throws Exception {
        return authenticatedConnection(socket, observer, System::nanoTime);
    }

    @SuppressWarnings({"unchecked", "rawtypes"})
    private static BridgeConnection authenticatedConnection(FakeWebSocket socket, Observer observer, LongSupplier clock) throws Exception {
        PairingInfo pairing = PairingInfo.parse("remontevoice://pair?host=10.1.2.3&port=8765&fingerprint="
                + "0".repeat(64) + "&pin=12345678");
        BridgeConnection connection = new BridgeConnection(pairing, observer, clock);
        Field state = BridgeConnection.class.getDeclaredField("state");
        state.setAccessible(true);
        state.set(connection, Enum.valueOf((Class<? extends Enum>) state.getType(), "AUTHENTICATED"));
        setField(connection, "socket", socket);
        setField(connection, "microphoneRequested", true);
        setField(connection, "microphoneEnabled", true);
        setField(connection, "playbackReady", true);
        setField(connection, "scheduler", Executors.newSingleThreadScheduledExecutor(task -> {
            Thread thread = new Thread(task, "remontevoice-test-backlog-timeout");
            thread.setDaemon(true);
            return thread;
        }));
        return connection;
    }

    private static Object getField(Object target, String name) throws Exception {
        Field field = target.getClass().getDeclaredField(name);
        field.setAccessible(true);
        return field.get(target);
    }

    private static void setField(Object target, String name, Object value) throws Exception {
        Field field = target.getClass().getDeclaredField(name);
        field.setAccessible(true);
        field.set(target, value);
    }

    private static class Observer implements BridgeConnection.Listener {
        final List<String> diagnostics = Collections.synchronizedList(new ArrayList<>());
        volatile boolean throwDiagnostic;
        @Override public void onTransportDiagnostic(String event) {
            diagnostics.add(event);
            if (throwDiagnostic) throw new IllegalStateException("Diagnostic observer failed");
        }
        final CountDownLatch authenticated = new CountDownLatch(1);
        final CountDownLatch audioReceived = new CountDownLatch(1);
        final CountDownLatch microphoneOn = new CountDownLatch(1);
        final CountDownLatch closed = new CountDownLatch(1);
        final AtomicReference<String> token = new AtomicReference<>();
        final AtomicReference<float[]> audio = new AtomicReference<>();
        final AtomicReference<String> message = new AtomicReference<>();
        final AtomicInteger closeCount = new AtomicInteger();
        final AtomicInteger microphoneStateCount = new AtomicInteger();
        final CountDownLatch screenOffered = new CountDownLatch(1);
        final AtomicInteger screenOfferCount = new AtomicInteger();
        final AtomicInteger screenErrorCount = new AtomicInteger();
        final AtomicLong screenRequest = new AtomicLong(-1);
        final AtomicReference<String> screenTicket = new AtomicReference<>();
        final AtomicReference<String> screenMessage = new AtomicReference<>();

        @Override public void onScreenOffer(long request, String ticket) {
            screenRequest.set(request);
            screenTicket.set(ticket);
            screenOfferCount.incrementAndGet();
            screenOffered.countDown();
        }

        @Override public void onScreenError(long request, String message) {
            screenRequest.set(request);
            screenMessage.set(message);
            screenErrorCount.incrementAndGet();
        }

        @Override public void onAuthenticated(String value) {
            token.set(value);
            authenticated.countDown();
        }

        @Override public void onAudio(float[] samples) {
            audio.set(samples);
            audioReceived.countDown();
        }

        @Override public void onMicState(boolean enabled) {
            microphoneStateCount.incrementAndGet();
            if (enabled) {
                microphoneOn.countDown();
            }
        }

        @Override public void onClosed(String value) {
            message.set(value);
            closeCount.incrementAndGet();
            closed.countDown();
        }
    }

    private static final class FakeWebSocket implements WebSocket {
        volatile long queued;
        volatile boolean cancelled;
        final List<String> controls = new ArrayList<>();
        final List<ByteString> audio = new ArrayList<>();

        @Override public Request request() { return new Request.Builder().url("https://10.1.2.3/").build(); }
        @Override public long queueSize() { return queued; }
        @Override public boolean send(String text) { controls.add(text); queued += text.length(); return true; }
        @Override public boolean send(ByteString bytes) { audio.add(bytes); queued += bytes.size(); return true; }
        @Override public boolean close(int code, String reason) { return true; }
        @Override public void cancel() { cancelled = true; }
    }
}
