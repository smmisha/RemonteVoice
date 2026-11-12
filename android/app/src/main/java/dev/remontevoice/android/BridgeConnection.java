package dev.remontevoice.android;

import java.net.Proxy;
import java.security.GeneralSecurityException;
import java.security.MessageDigest;
import java.security.cert.CertificateException;
import java.security.cert.X509Certificate;
import java.util.ArrayDeque;
import java.util.HashMap;
import java.util.Map;
import java.util.Objects;
import java.util.concurrent.Executors;
import java.util.concurrent.ScheduledExecutorService;
import java.util.concurrent.ScheduledFuture;
import java.util.concurrent.TimeUnit;
import java.util.function.LongSupplier;
import javax.net.ssl.SSLContext;
import javax.net.ssl.SSLSession;
import javax.net.ssl.TrustManager;
import javax.net.ssl.X509TrustManager;
import okhttp3.OkHttpClient;
import okhttp3.Request;
import okhttp3.Response;
import okhttp3.WebSocket;
import okhttp3.WebSocketListener;
import okio.ByteString;

/** A single, pinned TLS session. Credentials and raw remote errors are never logged. */
public final class BridgeConnection {
    public interface Listener {
        void onAuthenticated(String token);
        void onAudio(float[] samples);
        default void onAudio(float[] samples, long packet) { onAudio(samples); }
        void onMicState(boolean enabled);
        void onClosed(String message);
        default void onTransportDiagnostic(String event) {}
        default boolean collectAudioTiming() { return false; }
        default boolean collectReaderWaitSites() { return false; }
        default int currentNativeReaderTid() { return 0; }
        default void onScreenOffer(String ticket) {}
        default void onScreenError(String message) {}
        default void onScreenOffer(long request, String ticket) { onScreenOffer(ticket); }
        default void onScreenOffer(long request, String ticket, boolean video) {
            if (video) onScreenError(request, "Этот экран ещё не поддерживает видеорежим.");
            else onScreenOffer(request, ticket);
        }
        default void onScreenError(long request, String message) { onScreenError(message); }
    }

    private enum State { NEW, CONNECTING, AUTHENTICATED, CLOSED }
    private static final long MAX_AUDIO_QUEUE = 19210;
    private static final long MAX_BACKLOG_NANOS = TimeUnit.MILLISECONDS.toNanos(500);
    private static final String STALLED_AUDIO = "Передача звука остановилась. Сеанс завершён, микрофон выключен.";
    private final Object lock = new Object();
    private final PairingInfo pairing;
    private final Listener listener;
    private final LongSupplier clock;
    private final boolean videoEnabled;
    private final AudioReceiveTiming receiveTiming;
    private final ReaderWaitSite readerWaitSite;
    private final ArrayDeque<Boolean> pendingMicStates = new ArrayDeque<>();
    private final ArrayDeque<QueuedPacket> outgoing = new ArrayDeque<>();
    private State state = State.NEW;
    private OkHttpClient client;
    private WebSocket socket;
    private ScheduledExecutorService scheduler;
    private ScheduledFuture<?> authenticationDeadline;
    private ScheduledFuture<?> playbackReadyDeadline;
    private ScheduledFuture<?> backlogDeadline;
    private ScheduledFuture<?> screenDeadline;
    private boolean screenPending;
    private long screenRequestEpoch;
    private long screenListenerRequest;
    private boolean microphoneRequested;
    private boolean microphoneEnabled;
    private boolean detailScreenSupported;
    private boolean videoScreenSupported;
    private boolean screenRequestVideo;
    private boolean audioReadySupported;
    private boolean systemDeflateSupported;
    private boolean playbackReady;
    private long playbackReadyEpoch;
    private long callbackEpoch;
    private long outgoingBytes;
    private long watchedAudioTime;
    private long audioDeadlineEpoch;

    public BridgeConnection(PairingInfo pairing, Listener listener) {
        this(pairing, listener, System::nanoTime, false);
    }

    BridgeConnection(PairingInfo pairing, Listener listener, boolean videoEnabled) {
        this(pairing, listener, System::nanoTime, videoEnabled);
    }

    BridgeConnection(PairingInfo pairing, Listener listener, LongSupplier clock) {
        this(pairing, listener, clock, false);
    }

    private BridgeConnection(PairingInfo pairing, Listener listener, LongSupplier clock, boolean videoEnabled) {
        this.pairing = Objects.requireNonNull(pairing, "pairing");
        this.listener = Objects.requireNonNull(listener, "listener");
        this.clock = Objects.requireNonNull(clock, "clock");
        this.videoEnabled = videoEnabled;
        this.receiveTiming = listener.collectAudioTiming()
                ? new AudioReceiveTiming(clock, ReaderThreadTiming.linux(clock)) : null;
        ReaderWaitSite observer = null;
        if (receiveTiming != null && listener.collectReaderWaitSites()) {
            try { observer = ReaderWaitSite.createProcOnly(clock, android.os.Debug::threadCpuTimeNanos); }
            catch (RuntimeException ignored) { /* Optional diagnostics cannot prevent audio startup. */ }
        }
        this.readerWaitSite = observer;
    }

    public void connect(String credential) {
        if (!validCredential(credential)) {
            finish("Начните новое сопряжение на ноутбуке.");
            return;
        }
        synchronized (lock) {
            if (state != State.NEW) {
                return;
            }
            state = State.CONNECTING;
            try {
                PinnedTrustManager trust = new PinnedTrustManager(pairing.fingerprint);
                SSLContext context = SSLContext.getInstance("TLS");
                context.init(null, new TrustManager[] {trust}, null);
                client = new OkHttpClient.Builder()
                        .sslSocketFactory(context.getSocketFactory(), trust)
                        .hostnameVerifier((hostname, session) -> verifyHostname(pairing, hostname, session))
                        .proxy(Proxy.NO_PROXY)
                        .followRedirects(false)
                        .followSslRedirects(false)
                        .retryOnConnectionFailure(false)
                        .connectTimeout(8, TimeUnit.SECONDS)
                        .readTimeout(0, TimeUnit.SECONDS)
                        .writeTimeout(8, TimeUnit.SECONDS)
                        .pingInterval(10, TimeUnit.SECONDS)
                        .build();
                scheduler = Executors.newSingleThreadScheduledExecutor(task -> {
                    Thread thread = new Thread(task, "remontevoice-auth-timeout");
                    thread.setDaemon(true);
                    return thread;
                });
                authenticationDeadline = scheduler.schedule(
                        () -> finish("Ноутбук не подтвердил подключение. Проверьте сеть и сопряжение."),
                        8, TimeUnit.SECONDS);
                socket = client.newWebSocket(new Request.Builder().url(pairing.webSocketUrl()).build(),
                        new SocketListener(credential));
            } catch (GeneralSecurityException | RuntimeException ex) {
                finish("Не удалось создать защищённое подключение.");
            }
        }
    }

    public boolean setMicrophoneEnabled(boolean enabled) {
        boolean failed = false;
        synchronized (lock) {
            if (state != State.AUTHENTICATED || !playbackReady || socket == null) {
                return false;
            }
            microphoneRequested = enabled;
            microphoneEnabled = false;
            long now = clock.getAsLong();
            long queued = reconcileOutgoing();
            if (!ensureFreshAudio(now)) return false;
            if (pendingMicStates.size() >= 4 || queued > MAX_AUDIO_QUEUE) {
                failed = true;
            } else {
                pendingMicStates.addLast(enabled);
                if (sendControl(socket, "{\"type\":\"mic_enabled\",\"enabled\":" + enabled + "}")) {
                    return true;
                }
                failed = true;
            }
        }
        if (failed) {
            finish("Связь с ноутбуком прервана. Микрофон выключен.");
        }
        return false;
    }

    public boolean sendMicrophone(float[] samples) {
        byte[] packet = AudioProtocol.encodeMicrophone(samples);
        synchronized (lock) {
            if (state != State.AUTHENTICATED || !playbackReady || !microphoneRequested || !microphoneEnabled
                    || !pendingMicStates.isEmpty() || socket == null) {
                return false;
            }
            long queued = reconcileOutgoing();
            long now = clock.getAsLong();
            if (!ensureFreshAudio(now)) return false;
            if (queued + packet.length > MAX_AUDIO_QUEUE) {
                // Drop this capture, rather than adding latency or requiring a fully empty socket.
                return false;
            }
            if (socket.send(ByteString.of(packet))) {
                trackOutgoing(packet.length, now, true);
                scheduleAudioDeadline();
                return true;
            }
        }
        finish("Связь с ноутбуком прервана. Микрофон выключен.");
        return false;
    }

    /** Preview negotiation never queues pictures on the audio socket. */
    public boolean requestScreen() {
        return requestScreen(0);
    }

    public boolean requestScreen(long listenerRequest) {
        synchronized (lock) {
            if (state != State.AUTHENTICATED || !playbackReady || socket == null || screenPending
                    || socket.queueSize() > MAX_AUDIO_QUEUE || scheduler == null) return false;
            screenPending = true;
            screenListenerRequest = listenerRequest;
            final long request = ++screenRequestEpoch;
            screenDeadline = scheduler.schedule(() -> {
                synchronized (lock) {
                    if (!screenPending || screenRequestEpoch != request || state != State.AUTHENTICATED) return;
                    screenPending = false;
                    screenDeadline = null;
                    notifyScreenError(listenerRequest, "Ноутбук не ответил на запрос экрана. Попробуйте снова.");
                }
            }, 8, TimeUnit.SECONDS);
            screenRequestVideo = videoScreenSupported;
            String profile = screenRequestVideo ? ",\"profile\":\"video\""
                    : detailScreenSupported ? ",\"profile\":\"detail\"" : "";
            if (sendControl(socket, "{\"type\":\"screen_request\",\"request_id\":\"" + request + "\"" + profile + "}")) return true;
            screenPending = false;
            screenDeadline.cancel(false);
            screenDeadline = null;
            return false;
        }
    }

    private void receiveScreenControl(Map<String, Object> control) {
        // A timed-out request may arrive after a retry. Only its own exact wire ID owns this timer.
        // Older Hosts without request IDs time out the preview without interrupting the audio session.
        if (!screenPending || !Long.toString(screenRequestEpoch).equals(control.get("request_id"))) return;
        long request = screenListenerRequest;
        screenPending = false;
        if (screenDeadline != null) screenDeadline.cancel(false);
        screenDeadline = null;
        if ("screen_offer".equals(control.get("type")) && control.size() == 3
                && control.get("ticket") instanceof String
                && validToken((String) control.get("ticket"))) {
            try { listener.onScreenOffer(request, (String) control.get("ticket"), screenRequestVideo); }
            catch (RuntimeException ignored) { /* Preview must not interrupt audio. */ }
        } else {
            notifyScreenError(request, ScreenConnection.errorMessage(
                    "screen_error".equals(control.get("type")) && control.size() == 3
                            && control.get("message") instanceof String
                            ? (String) control.get("message") : "invalid_request"));
        }
    }

    private void notifyScreenError(long request, String message) {
        try { listener.onScreenError(request, message); }
        catch (RuntimeException ignored) { /* Preview observers are independent of audio. */ }
    }

    private static final class QueuedPacket {
        final long bytes;
        final long queuedAt;
        final boolean audio;

        QueuedPacket(long bytes, long queuedAt, boolean audio) {
            this.bytes = bytes; this.queuedAt = queuedAt; this.audio = audio;
        }
    }

    // OkHttp subtracts whole message payloads only after writing them to the socket.
    // Track text as well as PCM so a control message cannot be mistaken for delivered audio.
    // This observes the local writer, not receipt or playback on the Host.
    private long reconcileOutgoing() {
        return reconcileOutgoing(socket);
    }

    private long reconcileOutgoing(WebSocket target) {
        long queued = target.queueSize();
        while (!outgoing.isEmpty() && outgoingBytes - outgoing.peekFirst().bytes >= queued) {
            outgoingBytes -= outgoing.removeFirst().bytes;
        }
        return queued;
    }

    private void trackOutgoing(long bytes, long now, boolean audio) {
        outgoing.addLast(new QueuedPacket(bytes, now, audio));
        outgoingBytes += bytes;
    }

    private boolean sendControl(WebSocket target, String text) {
        reconcileOutgoing(target);
        if (!target.send(text)) return false;
        // Every generated control is ASCII (including the validated credential).
        trackOutgoing(text.length(), clock.getAsLong(), false);
        return true;
    }

    private QueuedPacket oldestAudio() {
        for (QueuedPacket packet : outgoing) if (packet.audio) return packet;
        return null;
    }

    private boolean ensureFreshAudio(long now) {
        QueuedPacket oldest = oldestAudio();
        if (oldest != null && now - oldest.queuedAt >= MAX_BACKLOG_NANOS) {
            // OkHttp cannot purge a stale frame. Cancel so mute cannot sit behind stale voice.
            finish(STALLED_AUDIO);
            return false;
        }
        scheduleAudioDeadline();
        return true;
    }

    private void scheduleAudioDeadline() {
        QueuedPacket oldest = oldestAudio();
        if (oldest != null && backlogDeadline != null && watchedAudioTime == oldest.queuedAt) return;
        final long epoch = ++audioDeadlineEpoch;
        if (backlogDeadline != null) {
            backlogDeadline.cancel(false);
            backlogDeadline = null;
        }
        watchedAudioTime = 0;
        if (oldest == null || scheduler == null) return;
        watchedAudioTime = oldest.queuedAt;
        long delay = Math.max(0, MAX_BACKLOG_NANOS - (clock.getAsLong() - oldest.queuedAt));
        backlogDeadline = scheduler.schedule(() -> {
            synchronized (lock) {
                if (audioDeadlineEpoch != epoch) return;
                backlogDeadline = null;
                watchedAudioTime = 0;
                if (state != State.AUTHENTICATED || socket == null) return;
                reconcileOutgoing();
                ensureFreshAudio(clock.getAsLong());
            }
        }, delay, TimeUnit.NANOSECONDS);
    }

    public void disconnect() {
        finish("Отключено.");
    }

    String readerMaximum() { return receiveTiming == null ? null : receiveTiming.maximum(); }
    ReaderWaitSite readerWaitSite() { return readerWaitSite; }

    /** Confirm local startup; older Hosts receive no additional control. */
    public boolean markPlaybackReady() {
        synchronized (lock) {
            if (state != State.AUTHENTICATED || socket == null) return false;
            if (playbackReady) return true;
            if (audioReadySupported && !sendControl(socket, "{\"type\":\"audio_ready\"}")) {
                finish("Не удалось подтвердить запуск звука. Подключитесь заново.");
                return false;
            }
            playbackReady = true;
            ++playbackReadyEpoch;
            if (playbackReadyDeadline != null) playbackReadyDeadline.cancel(false);
            playbackReadyDeadline = null;
            return true;
        }
    }

    private void watchPlaybackStartup() {
        final long epoch = ++playbackReadyEpoch;
        playbackReadyDeadline = scheduler.schedule(() -> {
            synchronized (lock) {
                if (state != State.AUTHENTICATED || playbackReady || playbackReadyEpoch != epoch) return;
                finish("Звук телефона не запустился вовремя. Подключение остановлено.");
            }
        }, 8, TimeUnit.SECONDS);
    }

    private final class SocketListener extends WebSocketListener {
        private String credential;

        SocketListener(String credential) {
            this.credential = credential;
        }

        @Override public void onOpen(WebSocket webSocket, Response response) {
            synchronized (lock) {
                if (state != State.CONNECTING) {
                    credential = null;
                    webSocket.cancel();
                    return;
                }
                String value = credential;
                credential = null;
                String video = videoEnabled ? ",\"screen_video\":true" : "";
                if (value == null || !sendControl(webSocket, "{\"type\":\"auth\",\"credential\":\"" + value + "\",\"screen_detail\":true,\"audio_ready\":true,\"system_deflate\":true" + video + "}")) {
                    finish("Ноутбук не подтвердил подключение.");
                }
            }
        }

        @Override public void onMessage(WebSocket webSocket, String text) {
            final Map<String, Object> control;
            try {
                control = parseControl(text);
            } catch (IllegalArgumentException ex) {
                finish("Ноутбук передал некорректное управляющее сообщение.");
                return;
            }
            synchronized (lock) {
                if (state == State.CLOSED) {
                    return;
                }
                String type = (String) control.get("type");
                if (state == State.CONNECTING) {
                    boolean detail = Boolean.TRUE.equals(control.get("screen_detail"));
                    boolean video = Boolean.TRUE.equals(control.get("screen_video"));
                    boolean ready = Boolean.TRUE.equals(control.get("audio_ready"));
                    boolean deflate = Boolean.TRUE.equals(control.get("system_deflate"));
                    boolean validShape = control.size() == 3 + (detail ? 1 : 0) + (ready ? 1 : 0) + (video ? 1 : 0) + (deflate ? 1 : 0)
                            && (!control.containsKey("screen_detail") || detail)
                            && (!control.containsKey("screen_video") || (video && videoEnabled && detail))
                            && (!control.containsKey("audio_ready") || ready)
                            && (!control.containsKey("system_deflate") || deflate);
                    if (!"auth_ok".equals(type) || !validShape
                            || !(control.get("credential") instanceof String)
                            || !validToken((String) control.get("credential"))
                            || !Boolean.FALSE.equals(control.get("microphone_enabled"))) {
                        finish("Сопряжение отклонено. Начните новое сопряжение на ноутбуке.");
                        return;
                    }
                    state = State.AUTHENTICATED;
                    detailScreenSupported = detail;
                    videoScreenSupported = video;
                    audioReadySupported = ready;
                    systemDeflateSupported = deflate;
                    authenticationDeadline.cancel(false);
                    microphoneRequested = false;
                    microphoneEnabled = false;
                    watchPlaybackStartup();
                    try {
                        listener.onAuthenticated((String) control.get("credential"));
                        if (state == State.AUTHENTICATED) {
                            notifyMic(false);
                        }
                    } catch (RuntimeException ex) {
                        finish("Не удалось запустить аудиосеанс.");
                    }
                    return;
                }
                if (state != State.AUTHENTICATED) {
                    finish("Ноутбук не подтвердил подключение.");
                    return;
                }
                if ("screen_offer".equals(type) || "screen_error".equals(type)) {
                    receiveScreenControl(control);
                } else if ("mic_state".equals(type) && control.size() == 2
                        && control.get("enabled") instanceof Boolean) {
                    boolean enabled = (Boolean) control.get("enabled");
                    Boolean expected = pendingMicStates.pollFirst();
                    if (expected == null || expected != enabled) {
                        finish("Ноутбук передал неожиданное состояние микрофона.");
                        return;
                    }
                    microphoneEnabled = enabled && microphoneRequested && pendingMicStates.isEmpty();
                    notifyMic(microphoneEnabled);
                } else if (!("pong".equals(type) && control.size() == 1)) {
                    finish("Ноутбук передал неподдерживаемое управляющее сообщение.");
                }
            }
        }

        @Override public void onMessage(WebSocket webSocket, ByteString bytes) {
            if (receiveTiming != null) receiveTiming.begin();
            if (readerWaitSite != null) readerWaitSite.onEntry(receiveTiming.packet(),
                    receiveTiming.entryNanos(), listener.currentNativeReaderTid());
            try {
                final long epoch;
                final boolean compressedAllowed;
                synchronized (lock) {
                    if (state == State.CLOSED) {
                        return;
                    }
                    if (state != State.AUTHENTICATED) {
                        finish("Аудио получено до подтверждения сопряжения.");
                        return;
                    }
                    epoch = callbackEpoch;
                    compressedAllowed = systemDeflateSupported;
                }
                // Native playback startup can hold the audio engine monitor. Never
                // let its callback block the independent transport startup deadline.
                try {
                    if (bytes.size() < 2 || bytes.size() > 3841
                            || (bytes.getByte(0) == 3 && !compressedAllowed)) throw new IllegalArgumentException();
                    float[] samples = AudioProtocol.decodeSystem(bytes.toByteArray());
                    synchronized (lock) {
                        if (state != State.AUTHENTICATED || callbackEpoch != epoch) return;
                    }
                    listener.onAudio(samples, receiveTiming == null ? 0 : receiveTiming.packet());
                } catch (RuntimeException ex) {
                    synchronized (lock) {
                        if (state == State.AUTHENTICATED && callbackEpoch == epoch) {
                            finish("Не удалось обработать аудиокадр ноутбука.");
                        }
                    }
                }
            } finally {
                if (readerWaitSite != null) readerWaitSite.onExit(receiveTiming.packet());
                if (receiveTiming != null) {
                    String event = receiveTiming.finish();
                    if (event != null) diagnostic(event);
                }
            }
        }

        @Override public void onClosing(WebSocket webSocket, int code, String reason) {
            credential = null;
            diagnostic("close_code=" + code);
            finish(closeMessage(code));
        }

        @Override public void onClosed(WebSocket webSocket, int code, String reason) {
            credential = null;
            finish(closeMessage(code));
        }

        @Override public void onFailure(WebSocket webSocket, Throwable failure, Response response) {
            credential = null;
            diagnostic("failure_class=" + failure.getClass().getSimpleName());
            finish("Не удалось подключиться к ноутбуку. Проверьте Wi-Fi, программу на ПК и код сопряжения.");
        }
    }

    private void notifyMic(boolean enabled) {
        try {
            listener.onMicState(enabled);
        } catch (RuntimeException ex) {
            finish("Не удалось изменить состояние микрофона.");
        }
    }

    private void diagnostic(String event) {
        try { listener.onTransportDiagnostic(event); }
        catch (RuntimeException ignored) { /* Observation must never interfere with teardown. */ }
    }

    private void finish(String message) {
        synchronized (lock) {
            if (state == State.CLOSED) {
                return;
            }
            state = State.CLOSED;
            ++callbackEpoch;
            if (receiveTiming != null) receiveTiming.freezeMaximum();
            if (readerWaitSite != null) readerWaitSite.requestStop();
            ++playbackReadyEpoch;
            playbackReady = false;
            systemDeflateSupported = false;
            microphoneRequested = false;
            microphoneEnabled = false;
            pendingMicStates.clear();
            outgoing.clear();
            outgoingBytes = 0;
            watchedAudioTime = 0;
            ++audioDeadlineEpoch;
            screenPending = false;
            if (screenDeadline != null) {
                screenDeadline.cancel(false);
                screenDeadline = null;
            }
            if (authenticationDeadline != null) {
                authenticationDeadline.cancel(false);
            }
            if (playbackReadyDeadline != null) {
                playbackReadyDeadline.cancel(false);
                playbackReadyDeadline = null;
            }
            if (backlogDeadline != null) {
                backlogDeadline.cancel(false);
                backlogDeadline = null;
            }
            if (socket != null) {
                socket.cancel();
                socket = null;
            }
            if (scheduler != null) {
                scheduler.shutdownNow();
                scheduler = null;
            }
            if (client != null) {
                client.dispatcher().cancelAll();
                client.connectionPool().evictAll();
                client.dispatcher().executorService().shutdown();
                client = null;
            }
            try {
                listener.onMicState(false);
            } catch (RuntimeException ignored) {
                // Closing must continue even when an observer has already stopped.
            }
            try {
                listener.onClosed(message);
            } catch (RuntimeException ignored) {
                // Do not expose observer exceptions through OkHttp logging.
            }
        }
    }

    private static String closeMessage(int code) {
        switch (code) {
            case 4001: return "Сопряжение отклонено. Создайте новый код на ноутбуке.";
            case 4002: return "К ноутбуку уже подключён другой телефон.";
            case 4003: return "Телефон и ноутбук должны находиться в одной локальной сети.";
            case 4005: return "Слишком много попыток. Подождите минуту и повторите сопряжение.";
            default: return "Соединение с ноутбуком завершено. Микрофон выключен.";
        }
    }

    private static boolean validCredential(String value) {
        return value != null && (value.matches("[0-9]{8}") || validToken(value));
    }

    static boolean validToken(String value) {
        return value != null && value.matches("[A-Za-z0-9_-]{32,256}");
    }

    static boolean verifyHostname(PairingInfo pairing, String hostname, SSLSession session) {
        if (!pairing.host.equals(hostname) || session == null) {
            return false;
        }
        try {
            java.security.cert.Certificate[] chain = session.getPeerCertificates();
            return chain.length > 0 && chain[0] instanceof X509Certificate
                    && matchesFingerprint((X509Certificate) chain[0], pairing.fingerprint);
        } catch (GeneralSecurityException | javax.net.ssl.SSLPeerUnverifiedException ex) {
            return false;
        }
    }

    static boolean matchesFingerprint(X509Certificate certificate, String fingerprint)
            throws GeneralSecurityException {
        certificate.checkValidity();
        byte[] expected = new byte[32];
        if (fingerprint == null || !fingerprint.matches("[0-9a-fA-F]{64}")) {
            return false;
        }
        for (int i = 0; i < expected.length; i++) {
            expected[i] = (byte) Integer.parseInt(fingerprint.substring(i * 2, i * 2 + 2), 16);
        }
        return MessageDigest.isEqual(expected,
                MessageDigest.getInstance("SHA-256").digest(certificate.getEncoded()));
    }

    static final class PinnedTrustManager implements X509TrustManager {
        private final String fingerprint;

        PinnedTrustManager(String fingerprint) {
            this.fingerprint = fingerprint;
        }

        @Override public void checkServerTrusted(X509Certificate[] chain, String authType)
                throws CertificateException {
            try {
                if (chain == null || chain.length == 0 || chain[0] == null
                        || authType == null || authType.isEmpty()
                        || !matchesFingerprint(chain[0], fingerprint)) {
                    throw new CertificateException("Сертификат ноутбука не совпадает с сопряжением.");
                }
            } catch (GeneralSecurityException ex) {
                throw new CertificateException("Не удалось проверить сертификат ноутбука.");
            }
        }

        @Override public void checkClientTrusted(X509Certificate[] chain, String authType)
                throws CertificateException {
            throw new CertificateException("Клиентские сертификаты не поддерживаются.");
        }

        @Override public X509Certificate[] getAcceptedIssuers() {
            return new X509Certificate[0];
        }
    }

    /** Strict, bounded flat JSON avoids an Android-only JSON dependency in protocol tests. */
    static Map<String, Object> parseControl(String text) {
        return parseControl(text, false);
    }

    /** Integer metadata is permitted only on the separate screen transport. */
    static Map<String, Object> parseScreenControl(String text) {
        return parseControl(text, true);
    }

    private static Map<String, Object> parseControl(String text, boolean integers) {
        if (text == null || text.length() > 4096) {
            throw new IllegalArgumentException("Некорректное управляющее сообщение.");
        }
        return new ControlParser(text, integers).parse();
    }

    private static final class ControlParser {
        private final String text;
        private final boolean integers;
        private int index;

        ControlParser(String text, boolean integers) { this.text = text; this.integers = integers; }

        Map<String, Object> parse() {
            Map<String, Object> values = new HashMap<>();
            whitespace();
            expect('{');
            whitespace();
            if (!take('}')) {
                do {
                    whitespace();
                    String key = string();
                    whitespace();
                    expect(':');
                    whitespace();
                    Object value;
                    if (index < text.length() && text.charAt(index) == '"') {
                        value = string();
                    } else if (text.startsWith("true", index)) {
                        index += 4;
                        value = Boolean.TRUE;
                    } else if (text.startsWith("false", index)) {
                        index += 5;
                        value = Boolean.FALSE;
                    } else if (integers && index < text.length() && text.charAt(index) >= '0'
                            && text.charAt(index) <= '9') {
                        int start = index;
                        while (index < text.length() && text.charAt(index) >= '0' && text.charAt(index) <= '9') index++;
                        String digits = text.substring(start, index);
                        if (digits.length() > 10 || (digits.length() > 1 && digits.charAt(0) == '0')) throw invalid();
                        try { value = Integer.valueOf(digits); }
                        catch (NumberFormatException error) { throw invalid(); }
                    } else {
                        throw invalid();
                    }
                    if (values.size() >= 8 || values.put(key, value) != null) {
                        throw invalid();
                    }
                    whitespace();
                    if (take('}')) {
                        break;
                    }
                    expect(',');
                } while (true);
            }
            whitespace();
            if (index != text.length() || !(values.get("type") instanceof String)) {
                throw invalid();
            }
            return values;
        }

        private String string() {
            expect('"');
            StringBuilder result = new StringBuilder();
            while (index < text.length()) {
                char current = text.charAt(index++);
                if (current == '"') {
                    return result.toString();
                }
                if (current < 0x20) {
                    throw invalid();
                }
                if (current == '\\') {
                    if (index >= text.length()) {
                        throw invalid();
                    }
                    char escaped = text.charAt(index++);
                    switch (escaped) {
                        case '"': case '\\': case '/': current = escaped; break;
                        case 'b': current = '\b'; break;
                        case 'f': current = '\f'; break;
                        case 'n': current = '\n'; break;
                        case 'r': current = '\r'; break;
                        case 't': current = '\t'; break;
                        case 'u':
                            if (index + 4 > text.length()) {
                                throw invalid();
                            }
                            String hex = text.substring(index, index + 4);
                            if (!hex.matches("[0-9a-fA-F]{4}")) {
                                throw invalid();
                            }
                            current = (char) Integer.parseInt(hex, 16);
                            index += 4;
                            break;
                        default: throw invalid();
                    }
                }
                result.append(current);
                if (result.length() > 512) {
                    throw invalid();
                }
            }
            throw invalid();
        }

        private void whitespace() {
            while (index < text.length() && (text.charAt(index) == ' '
                    || text.charAt(index) == '\n' || text.charAt(index) == '\r'
                    || text.charAt(index) == '\t')) {
                index++;
            }
        }

        private boolean take(char wanted) {
            if (index < text.length() && text.charAt(index) == wanted) {
                index++;
                return true;
            }
            return false;
        }

        private void expect(char wanted) {
            if (!take(wanted)) {
                throw invalid();
            }
        }

        private IllegalArgumentException invalid() {
            return new IllegalArgumentException("Некорректное управляющее сообщение.");
        }
    }
}
