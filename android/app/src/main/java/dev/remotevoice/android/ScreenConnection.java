package dev.remotevoice.android;

import java.net.Proxy;
import java.security.GeneralSecurityException;
import java.util.Map;
import java.util.Arrays;
import java.util.Objects;
import java.util.concurrent.Executors;
import java.util.concurrent.ScheduledExecutorService;
import java.util.concurrent.ScheduledFuture;
import java.util.concurrent.TimeUnit;
import javax.net.ssl.SSLContext;
import javax.net.ssl.TrustManager;
import okhttp3.OkHttpClient;
import okhttp3.Request;
import okhttp3.Response;
import okhttp3.WebSocket;
import okhttp3.WebSocketListener;
import okio.ByteString;

/** One explicit preview session, with one frame in flight and no automatic retry. */
public final class ScreenConnection {
    public interface Listener {
        void onReady();
        void onFrame(byte[] jpeg);
        default void onVideoReady(int width, int height, int fps) { throw new IllegalStateException(); }
        default void onVideoFrame(byte[] accessUnit) { throw new IllegalStateException(); }
        void onClosed(String message);
    }

    private enum State { NEW, CONNECTING, READY, WAITING, DELIVERING, CLOSED }
    private final Object lock = new Object();
    private final PairingInfo pairing;
    private final boolean videoExpected;
    private int videoWidth, videoHeight;
    private H264AccessUnit.Parameters videoParameters;
    private Listener listener;
    private State state = State.NEW;
    private OkHttpClient client;
    private WebSocket socket;
    private ScheduledExecutorService scheduler;
    private ScheduledFuture<?> deadline;
    private long deadlineEpoch;

    public ScreenConnection(PairingInfo pairing, Listener listener) {
        this(pairing, listener, false);
    }

    ScreenConnection(PairingInfo pairing, Listener listener, boolean videoExpected) {
        this.pairing = Objects.requireNonNull(pairing);
        this.listener = Objects.requireNonNull(listener);
        this.videoExpected = videoExpected;
    }

    public void connect(String ticket) {
        synchronized (lock) {
            if (state != State.NEW) return;
            if (!BridgeConnection.validToken(ticket)) { finish("Запрос экрана недействителен."); return; }
            state = State.CONNECTING;
            try {
                BridgeConnection.PinnedTrustManager trust = new BridgeConnection.PinnedTrustManager(pairing.fingerprint);
                SSLContext context = SSLContext.getInstance("TLS");
                context.init(null, new TrustManager[]{trust}, null);
                client = new OkHttpClient.Builder()
                        .sslSocketFactory(context.getSocketFactory(), trust)
                        .hostnameVerifier((host, session) -> BridgeConnection.verifyHostname(pairing, host, session))
                        .proxy(Proxy.NO_PROXY).followRedirects(false).followSslRedirects(false)
                        .retryOnConnectionFailure(false).connectTimeout(8, TimeUnit.SECONDS)
                        .readTimeout(0, TimeUnit.SECONDS).writeTimeout(5, TimeUnit.SECONDS)
                        .build();
                scheduler = Executors.newSingleThreadScheduledExecutor(task -> {
                    Thread thread = new Thread(task, "remotevoice-screen-timeout");
                    thread.setDaemon(true);
                    return thread;
                });
                armDeadline(8);
                socket = client.newWebSocket(new Request.Builder()
                        .url("wss://" + pairing.host + ":" + pairing.port + "/screen").build(),
                        new SocketListener(ticket));
            } catch (GeneralSecurityException | RuntimeException exception) {
                finish("Не удалось открыть защищённый просмотр экрана.");
            }
        }
    }

    /** JPEG: pull after display. H.264: pull after the codec accepts the previous access unit. */
    public boolean requestNext() {
        synchronized (lock) {
            if ((state != State.READY && state != State.DELIVERING) || socket == null) return false;
            // A peer response can arrive just before OkHttp decrements the written control bytes.
            if (socket.queueSize() > 256) { finish("Просмотр экрана остановлен из-за задержки сети."); return false; }
            state = State.WAITING;
            armDeadline(5);
            if (socket.send("{\"type\":\"screen_next\"}")) return true;
            finish("Просмотр экрана отключён. Попробуйте снова.");
            return false;
        }
    }

    /** Cancels the preview and releases its observer without notifying a hidden Activity. */
    public void close() {
        synchronized (lock) {
            listener = null;
            finish("");
        }
    }

    private void armDeadline(int seconds) {
        if (deadline != null) deadline.cancel(false);
        final long epoch = ++deadlineEpoch;
        deadline = scheduler.schedule(() -> {
            synchronized (lock) {
                if (state != State.CLOSED && deadlineEpoch == epoch) finish(errorMessage("timeout"));
            }
        }, seconds, TimeUnit.SECONDS);
    }

    private void finish(String message) {
        synchronized (lock) {
            if (state == State.CLOSED) return;
            state = State.CLOSED;
            ++deadlineEpoch;
            if (deadline != null) deadline.cancel(false);
            deadline = null;
            if (socket != null) socket.cancel();
            socket = null;
            if (scheduler != null) scheduler.shutdownNow();
            scheduler = null;
            if (client != null) {
                client.dispatcher().cancelAll();
                client.connectionPool().evictAll();
                client.dispatcher().executorService().shutdown();
            }
            client = null;
            Listener previous = listener;
            listener = null;
            if (previous != null) {
                try { previous.onClosed(message); }
                catch (RuntimeException ignored) { /* Preview observers cannot affect audio. */ }
            }
        }
    }

    private final class SocketListener extends WebSocketListener {
        private String ticket;
        SocketListener(String ticket) { this.ticket = ticket; }

        @Override public void onOpen(WebSocket webSocket, Response response) {
            synchronized (lock) {
                String value = ticket;
                ticket = null;
                if (state != State.CONNECTING) { webSocket.cancel(); return; }
                if (value == null || !webSocket.send("{\"type\":\"screen_auth\",\"ticket\":\"" + value + "\"}")) {
                    finish("Ноутбук не подтвердил просмотр экрана.");
                }
            }
        }

        @Override public void onMessage(WebSocket webSocket, String text) {
            final Map<String, Object> control;
            try { control = BridgeConnection.parseScreenControl(text); }
            catch (IllegalArgumentException exception) { finish("Некорректный ответ просмотра экрана."); return; }
            synchronized (lock) {
                if (state == State.CLOSED) return;
                if ("screen_error".equals(control.get("type")) && control.size() == 2
                        && control.get("message") instanceof String) {
                    finish(errorMessage((String) control.get("message")));
                } else if (state == State.CONNECTING && validReady(control)) {
                    deadline.cancel(false);
                    deadline = null;
                    ++deadlineEpoch;
                    state = State.READY;
                    try {
                        if (listener != null) {
                            if (videoExpected) listener.onVideoReady(videoWidth, videoHeight, 30);
                            else listener.onReady();
                        }
                    }
                    catch (RuntimeException exception) { finish("Не удалось показать экран."); }
                } else finish("Некорректный ответ просмотра экрана.");
            }
        }

        @Override public void onMessage(WebSocket webSocket, ByteString bytes) {
            synchronized (lock) {
                if (state == State.CLOSED) return;
                if (state != State.WAITING || bytes.size() > (videoExpected
                        ? H264AccessUnit.MAX_BYTES : ScreenFrameLimits.MAX_BYTES)) {
                    finish("Ноутбук передал неожиданный кадр экрана."); return;
                }
                byte[] payload = bytes.toByteArray();
                try {
                    if (videoExpected) {
                        H264AccessUnit.Parameters parameters = H264AccessUnit.inspect(payload,
                                videoWidth, videoHeight, videoParameters == null);
                        if (parameters != null) {
                            if (videoParameters != null && (!Arrays.equals(videoParameters.sps, parameters.sps)
                                    || !Arrays.equals(videoParameters.pps, parameters.pps))) throw new IllegalArgumentException();
                            videoParameters = parameters;
                        }
                    } else ScreenFrameLimits.dimensions(payload);
                }
                catch (IllegalArgumentException exception) { finish("Ноутбук передал некорректный кадр экрана."); return; }
                deadline.cancel(false);
                deadline = null;
                ++deadlineEpoch;
                state = State.DELIVERING;
                try {
                    if (listener != null) {
                        if (videoExpected) listener.onVideoFrame(payload);
                        else listener.onFrame(payload);
                    }
                }
                catch (RuntimeException exception) { finish("Не удалось показать экран."); }
            }
        }

        @Override public void onClosing(WebSocket socket, int code, String reason) {
            ticket = null; finish("Просмотр экрана завершён.");
        }
        @Override public void onClosed(WebSocket socket, int code, String reason) {
            ticket = null; finish("Просмотр экрана завершён.");
        }
        @Override public void onFailure(WebSocket socket, Throwable failure, Response response) {
            ticket = null; finish("Не удалось получить экран ноутбука. Проверьте подключение.");
        }
    }

    private boolean validReady(Map<String, Object> control) {
        if (!"screen_ok".equals(control.get("type"))) return false;
        if (!videoExpected) return control.size() == 1;
        // This version implements one bounded baseline profile, never dimensions supplied without negotiation.
        if (control.size() != 5 || !"h264".equals(control.get("codec"))
                || !Integer.valueOf(1280).equals(control.get("width"))
                || !Integer.valueOf(800).equals(control.get("height"))
                || !Integer.valueOf(30).equals(control.get("fps"))) return false;
        videoWidth = 1280; videoHeight = 800;
        return true;
    }

    static String errorMessage(String code) {
        if (code == null) return "Просмотр экрана недоступен.";
        switch (code) {
            case "disabled": return "Разрешите просмотр экрана в Windows Host.";
            case "unavailable": return "Запрос экрана устарел. Нажмите «Показать экран» снова.";
            case "busy": return "Экран уже открыт в другом сеансе.";
            case "capture_failed": return "Ноутбук не смог получить изображение экрана.";
            case "timeout": return "Экран не получен вовремя. Просмотр остановлен.";
            case "invalid_request": return "Не удалось подтвердить запрос экрана.";
            default: return "Просмотр экрана недоступен.";
        }
    }
}
