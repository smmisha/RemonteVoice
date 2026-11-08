package dev.remotevoice.android;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Intent;
import android.content.Context;
import android.net.wifi.WifiManager;
import android.content.pm.ServiceInfo;
import android.os.Binder;
import android.os.Build;
import android.os.Handler;
import android.os.IBinder;
import android.os.Looper;

import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

/** A user-started audio session, with an ongoing notification and no automatic reconnect. */
public final class BridgeService extends Service {
    static final String CONNECT = "dev.remotevoice.CONNECT";
    static final String DISCONNECT = "dev.remotevoice.DISCONNECT";
    static final String MIC_ON = "dev.remotevoice.MIC_ON";
    static final String MIC_OFF = "dev.remotevoice.MIC_OFF";
    private static final String CHANNEL = "remotevoice-session";
    private static final int NOTIFICATION_ID = 1;
    private final Handler main = new Handler(Looper.getMainLooper());
    private final ExecutorService credentials = Executors.newSingleThreadExecutor();
    private final LocalBinder binder = new LocalBinder();
    private PhoneAudioEngine audio;
    private CredentialStore store;
    private BridgeConnection connection;
    ReaderWaitSite lastReaderWaitSite;
    private PairingInfo activePairing;
    private WifiManager.WifiLock wifiPerformance;
    private WifiManager.WifiLock wifiLatency;
    private long screenRequestId;
    private long screenOwnerRequest;
    private ScreenOffer screenOffer;
    private final java.util.Set<Runnable> observers = new java.util.HashSet<>();
    private volatile long generation;
    private int microphoneRequest;
    private boolean requestedMicrophone;
    boolean connected;
    boolean connecting;
    boolean microphone;
    boolean microphonePending;
    String status = "Готов к подключению";
    String host = "";

    public final class LocalBinder extends Binder { BridgeService service() { return BridgeService.this; } }
    @Override public IBinder onBind(Intent intent) { return binder; }
    @Override public boolean onUnbind(Intent intent) {
        observers.clear();
        screenOwnerRequest = 0;
        screenOffer = null;
        return false;
    }

    @Override public void onCreate() {
        super.onCreate();
        store = new CredentialStore(this);
        NotificationChannel channel = new NotificationChannel(CHANNEL, getString(R.string.channel_name), NotificationManager.IMPORTANCE_LOW);
        channel.setDescription(getString(R.string.channel_description));
        channel.setSound(null, null);
        getSystemService(NotificationManager.class).createNotificationChannel(channel);
    }

    void observe(Runnable observer) { observers.add(observer); observer.run(); }
    void stopObserving(Runnable observer) { observers.remove(observer); }
    long sessionId() { return generation; }

    /** Immutable, one-use offer; never contains an Activity, bitmap, or audio credential. */
    static final class ScreenOffer {
        final PairingInfo pairing;
        final String ticket;
        final String error;
        final boolean video;
        ScreenOffer(PairingInfo pairing, String ticket, String error) {
            this(pairing, ticket, error, false);
        }
        ScreenOffer(PairingInfo pairing, String ticket, String error, boolean video) {
            this.pairing = pairing; this.ticket = ticket; this.error = error;
            this.video = video;
        }
    }

    long requestScreen() {
        if (!connected || connection == null || activePairing == null || screenOwnerRequest != 0) return -1;
        long request = ++screenRequestId;
        screenOwnerRequest = request;
        screenOffer = null;
        if (connection.requestScreen(request)) return request;
        screenOwnerRequest = 0;
        return -1;
    }

    ScreenOffer takeScreenOffer(long session, long request) {
        if (generation != session || screenOwnerRequest != request || screenOffer == null) return null;
        ScreenOffer offer = screenOffer;
        screenOffer = null;
        screenOwnerRequest = 0;
        return offer;
    }

    void cancelScreenRequest(long request) {
        if (screenOwnerRequest == request) {
            screenOwnerRequest = 0;
            screenOffer = null;
        }
    }

    @Override public int onStartCommand(Intent intent, int flags, int startId) {
        if (intent == null) { close("Соединение остановлено"); return START_NOT_STICKY; }
        try {
            String action = intent.getAction();
            if (CONNECT.equals(action)) begin(intent.getStringExtra("link"), intent.getBooleanExtra("fresh", true),
                    intent.getBooleanExtra("reader_wait_probe", false));
            else if (DISCONNECT.equals(action)) close("Соединение остановлено");
            else if (MIC_ON.equals(action)) changeMicrophone(true);
            else if (MIC_OFF.equals(action)) changeMicrophone(false);
        } catch (RuntimeException exception) {
            close("Не удалось открыть аудиосеанс. Проверьте разрешения и попробуйте снова.");
        }
        return START_NOT_STICKY;
    }

    private void begin(String link, boolean fresh, boolean requestedReaderWaitProbe) {
        if (connecting || connected) return;
        PairingInfo pairing;
        try { pairing = PairingInfo.parse(link); }
        catch (IllegalArgumentException exception) { close("Нужна корректная ссылка из QR-кода RemoteVoice на ноутбуке."); return; }
        final long session = ++generation;
        final boolean readerWaitProbe = requestedReaderWaitProbe
                && (getApplicationInfo().flags & android.content.pm.ApplicationInfo.FLAG_DEBUGGABLE) != 0
                && (lastReaderWaitSite == null || lastReaderWaitSite.awaitStopped(0));
        activePairing = pairing;
        screenOwnerRequest = 0;
        screenOffer = null;
        final PhoneAudioEngine sessionAudio = new PhoneAudioEngine(this, message -> main.post(() -> {
            if (generation == session) close(message);
        }));
        audio = sessionAudio;
        connecting = true;
        microphone = false;
        host = pairing.host;
        status = "Подключение к " + host;
        foreground(false);
        acquireWifiPerformance();
        changed();
        credentials.execute(() -> {
            String credential;
            try {
                String saved = fresh ? null : store.load(pairing.fingerprint);
                credential = saved != null ? saved : pairing.pin;
            } catch (java.security.GeneralSecurityException exception) {
                main.post(() -> { if (generation == session) close("Не удалось прочитать ключ сопряжения. Отсканируйте новый QR-код."); });
                return;
            }
            final String auth = credential;
            main.post(() -> {
                if (generation != session) return;
                connection = new BridgeConnection(pairing, new BridgeConnection.Listener() {
                    public void onAuthenticated(String token) {
                        if (generation != session || credentials.isShutdown()) return;
                        credentials.execute(() -> {
                            try { store.save(pairing.fingerprint, token); }
                            catch (java.security.GeneralSecurityException exception) {
                                main.post(() -> { if (generation == session) close("Не удалось защитить ключ сопряжения. Подключение остановлено."); });
                                return;
                            }
                            main.post(() -> {
                                if (generation != session) return;
                                try {
                                    sessionAudio.startPlayback();
                                    BridgeConnection active = connection;
                                    if (generation != session || active == null || !active.markPlaybackReady()) {
                                        if (generation == session) close("Не удалось подтвердить запуск звука. Подключитесь заново.");
                                        return;
                                    }
                                    connecting = false;
                                    connected = true;
                                    status = "Подключено. Микрофон выключен";
                                    watchAudioProgress(session, sessionAudio);
                                    getSharedPreferences("endpoint", MODE_PRIVATE).edit()
                                            .putString("link", "remotevoice://pair?host=" + pairing.host + "&port=" + pairing.port
                                                    + "&fingerprint=" + pairing.fingerprint + "&pin=00000000").apply();
                                    foreground(false);
                                    changed();
                                } catch (RuntimeException exception) { close("Не удалось запустить звук. Проверьте наушники и аудиоустройство."); }
                            });
                        });
                    }
                    public void onAudio(float[] samples) { if (generation == session) sessionAudio.offer(samples); }
                    public void onAudio(float[] samples, long packet) {
                        if (generation == session) sessionAudio.offer(samples, packet);
                    }
                    public boolean collectAudioTiming() {
                        return (getApplicationInfo().flags & android.content.pm.ApplicationInfo.FLAG_DEBUGGABLE) != 0;
                    }
                    public boolean collectReaderWaitSites() { return readerWaitProbe; }
                    public int currentNativeReaderTid() { return android.os.Process.myTid(); }
                    public void onTransportDiagnostic(String event) {
                        if (generation == session) android.util.Log.i("RemoteVoiceSession", event);
                    }
                    public void onMicState(boolean enabled) { main.post(() -> acknowledgeMicrophone(session, enabled)); }
                    public void onClosed(String message) {
                        main.post(() -> { if (generation == session) close(message == null ? "Соединение закрыто" : message); });
                    }
                    public void onScreenOffer(long request, String ticket, boolean video) {
                        main.post(() -> {
                            if (generation != session || !connected || screenOwnerRequest != request) return;
                            screenOffer = new ScreenOffer(pairing, ticket, null, video);
                            changed();
                        });
                    }
                    public void onScreenError(long request, String message) {
                        main.post(() -> {
                            if (generation != session || !connected || screenOwnerRequest != request) return;
                            screenOffer = new ScreenOffer(null, null, message);
                            changed();
                        });
                    }
                }, true);
                connection.connect(auth);
            });
        });
    }

    private void changeMicrophone(boolean enabled) {
        if (!connected || connection == null || microphonePending) return;
        if (!enabled) {
            audio.stopMicrophone();
            microphone = false;
        }
        // MIC_ON is only sent by the visible Activity after runtime permission is granted.
        foreground(enabled);
        microphonePending = true;
        requestedMicrophone = enabled;
        int request = ++microphoneRequest;
        status = enabled ? "Включаем микрофон…" : "Выключаем микрофон…";
        changed();
        if (!connection.setMicrophoneEnabled(enabled)) { close("Не удалось переключить микрофон. Подключитесь заново."); return; }
        long session = generation;
        main.postDelayed(() -> {
            if (generation == session && microphoneRequest == request && microphonePending) close("Ноутбук не подтвердил состояние микрофона. Подключение остановлено.");
        }, 4000);
    }

    private void acknowledgeMicrophone(long session, boolean enabled) {
        if (generation != session || !connected || connection == null) return;
        try {
            if (microphonePending && requestedMicrophone != enabled) { close("Ноутбук не подтвердил выбранное состояние микрофона."); return; }
            if (enabled) {
                if (!microphonePending) { close("Получено неожиданное включение микрофона."); return; }
                BridgeConnection active = connection;
                audio.startMicrophone(samples -> active.sendMicrophone(samples));
            } else audio.stopMicrophone();
            microphone = enabled;
            microphonePending = false;
            status = enabled ? "Микрофон передаёт голос на ноутбук" : "Подключено. Микрофон выключен";
            foreground(enabled);
            changed();
        } catch (RuntimeException exception) { close("Микрофон недоступен. Проверьте разрешение и подключитесь заново."); }
    }

    private void close(String message) {
        if (connecting || connected) {
            android.util.Log.i("RemoteVoiceSession", "close generation=" + generation
                    + " connected=" + connected + " reason=" + message);
        }
        ++generation;
        screenOwnerRequest = 0;
        screenOffer = null;
        activePairing = null;
        BridgeConnection previous = connection;
        connection = null;
        // Release the Host's routes before any potentially blocking native audio stop.
        if (previous != null) previous.disconnect();
        if (previous != null && previous.readerWaitSite() != null) lastReaderWaitSite = previous.readerWaitSite();
        PhoneAudioEngine previousAudio = audio;
        audio = null;
        try {
            if (previousAudio != null) previousAudio.stop();
        } finally {
            releaseWifiPerformance();
            connecting = false;
            connected = false;
            microphone = false;
            microphonePending = false;
            status = message;
            stopForeground(STOP_FOREGROUND_REMOVE);
            changed();
            stopSelf();
            // Routes, native playback and performance leases are already released.
            // Preserve the scalar maximum even if periodic logging suppressed its event.
            if (previous != null) {
                try {
                    String maximum = previous.readerMaximum();
                    if (maximum != null) android.util.Log.i("RemoteVoiceSession", maximum);
                } catch (RuntimeException ignored) { /* Diagnostics cannot prevent cleanup. */ }
            }
        }
    }

    private void watchAudioProgress(long session, PhoneAudioEngine sessionAudio) {
        main.postDelayed(() -> {
            if (generation != session || !connected || audio != sessionAudio) return;
            try { sessionAudio.assertProgress(); }
            catch (IllegalStateException exception) { close(exception.getMessage()); return; }
            watchAudioProgress(session, sessionAudio);
        }, 250);
    }

    private void acquireWifiPerformance() {
        releaseWifiPerformance();
        if (wifiPerformance != null || wifiLatency != null) return; // A release needs a later retry.
        // Before Android14 high-perf also covers Home/screen-off. Newer Android
        // replaces it with low-latency; its foreground restrictions still apply.
        int mode = Build.VERSION.SDK_INT >= 34 ? WifiManager.WIFI_MODE_FULL_LOW_LATENCY : WifiManager.WIFI_MODE_FULL_HIGH_PERF;
        WifiManager.WifiLock candidate = null;
        try {
            WifiManager manager = (WifiManager) getApplicationContext().getSystemService(Context.WIFI_SERVICE);
            if (manager == null) return;
            candidate = manager.createWifiLock(mode, "RemoteVoice:AudioSession");
            candidate.setReferenceCounted(false);
            candidate.acquire();
            wifiPerformance = candidate;
            // Android29-33 uses low latency while foreground/screen-on and
            // automatically falls back to the high-perf lease in background.
            if (Build.VERSION.SDK_INT >= 29 && Build.VERSION.SDK_INT < 34) {
                try {
                    wifiLatency = manager.createWifiLock(WifiManager.WIFI_MODE_FULL_LOW_LATENCY, "RemoteVoice:ForegroundAudio");
                    wifiLatency.setReferenceCounted(false);
                    wifiLatency.acquire();
                } catch (RuntimeException ignored) {
                    wifiLatency = releaseWifiLease(wifiLatency);
                    android.util.Log.i("RemoteVoiceSession", "wifi_latency_unavailable");
                }
            }
        } catch (RuntimeException ignored) {
            wifiPerformance = candidate;
            releaseWifiPerformance();
            android.util.Log.i("RemoteVoiceSession", "wifi_performance_unavailable");
        }
    }

    private void releaseWifiPerformance() {
        wifiLatency = releaseWifiLease(wifiLatency);
        wifiPerformance = releaseWifiLease(wifiPerformance);
    }

    private WifiManager.WifiLock releaseWifiLease(WifiManager.WifiLock previous) {
        if (previous == null) return null;
        try {
            if (previous.isHeld()) previous.release();
            return null;
        } catch (RuntimeException ignored) {
            // Keep the handle for onDestroy/next close. Power hints must never
            // prevent socket/audio cleanup or leave the visible session active.
            android.util.Log.i("RemoteVoiceSession", "wifi_performance_release_pending");
            return previous;
        }
    }

    private void changed() { for (Runnable observer : new java.util.ArrayList<>(observers)) observer.run(); }

    private PendingIntent command(String action, int code) {
        return PendingIntent.getService(this, code, new Intent(this, BridgeService.class).setAction(action),
                PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);
    }

    private void foreground(boolean micType) {
        Intent openIntent = new Intent(this, MainActivity.class).addFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP | Intent.FLAG_ACTIVITY_CLEAR_TOP);
        PendingIntent open = PendingIntent.getActivity(this, 0, openIntent, PendingIntent.FLAG_IMMUTABLE);
        Notification.Builder builder = new Notification.Builder(this, CHANNEL).setSmallIcon(R.drawable.ic_remotevoice)
                .setContentTitle("RemoteVoice · " + host).setContentText(status).setContentIntent(open)
                .setOngoing(true).setOnlyAlertOnce(true).setCategory(Notification.CATEGORY_SERVICE)
                .setVisibility(Notification.VISIBILITY_PRIVATE)
                .addAction(new Notification.Action.Builder(null, "Отключиться", command(DISCONNECT, 1)).build());
        if (microphone) builder.addAction(new Notification.Action.Builder(null, "Выключить микрофон", command(MIC_OFF, 2)).build());
        if (Build.VERSION.SDK_INT >= 29) {
            int types = ServiceInfo.FOREGROUND_SERVICE_TYPE_MEDIA_PLAYBACK;
            if (micType && Build.VERSION.SDK_INT >= 30) types |= ServiceInfo.FOREGROUND_SERVICE_TYPE_MICROPHONE;
            startForeground(NOTIFICATION_ID, builder.build(), types);
        } else startForeground(NOTIFICATION_ID, builder.build());
    }

    @Override public void onDestroy() {
        observers.clear();
        close("Соединение остановлено");
        credentials.shutdown();
        main.removeCallbacksAndMessages(null);
        super.onDestroy();
    }
}
