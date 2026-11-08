package dev.remotevoice.android;

import android.Manifest;
import android.app.Activity;
import android.app.Dialog;
import android.content.ComponentName;
import android.content.Intent;
import android.content.ServiceConnection;
import android.content.pm.PackageManager;
import android.graphics.Color;
import android.graphics.Bitmap;
import android.graphics.Typeface;
import android.graphics.drawable.GradientDrawable;
import android.graphics.drawable.RippleDrawable;
import android.graphics.drawable.StateListDrawable;
import android.content.res.ColorStateList;
import android.content.res.Configuration;
import android.os.Build;
import android.os.Bundle;
import android.os.IBinder;
import android.os.Handler;
import android.os.Looper;
import android.text.InputType;
import android.text.Editable;
import android.text.TextWatcher;
import android.view.Gravity;
import android.view.View;
import android.view.ViewGroup;
import android.view.Surface;
import android.view.WindowInsets;
import android.view.WindowManager;
import android.widget.Button;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.FrameLayout;
import android.widget.ScrollView;
import android.widget.TextView;
import android.widget.Toast;

import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.RejectedExecutionException;

import com.google.zxing.integration.android.IntentIntegrator;
import com.google.zxing.integration.android.IntentResult;

/** Native, scrollable UI: explicit pairing, explicit microphone enable, visible connection state. */
public final class MainActivity extends Activity {
    private enum PreviewCloseReason {
        COLLAPSE, HIDDEN, RESTART, SERVICE_DISCONNECTED, SESSION_CHANGED,
        OFFER_ERROR, REQUEST_UNAVAILABLE, TRANSPORT_CLOSED, DECODE_FAILED,
        ACTIVITY_PAUSE, ACTIVITY_STOP
    }
    private static final int MIC_PERMISSION = 1;
    private static final int NOTIFICATION_PERMISSION = 2;
    private static final int INK = Color.rgb(235, 245, 240);
    private static final int MUTED = Color.rgb(164, 185, 174);
    private static final int GREEN = Color.rgb(128, 232, 186);
    private static final int BACKGROUND = 0xFF101815;
    private static final int SURFACE = 0xFF1B2922;
    private static final int CONTROL = 0xFF24392E;
    private static final int BORDER = 0xFF365144;
    private static final int ACCENT_TEXT = 0xFF102019;
    private static final int WARNING = 0xFFF6A696;
    private BridgeService service;
    private boolean bound;
    private boolean visible;
    private long pendingMicrophoneSession = -1;
    private boolean microphonePermissionGranted;
    private EditText link;
    private TextView status;
    private TextView endpoint;
    private Button connect;
    private Button microphone;
    private Button disconnect;
    private Button screenButton;
    private Button fullscreenButton;
    private Button scan;
    private LinearLayout pairingPanel;
    private LinearLayout audioPanel;
    private LinearLayout screenPanel;
    private LinearLayout sessionFooter;
    private TextView sessionLabel;
    private TextView pairingHint;
    private ScrollView page;
    private boolean showingSession;
    private TextView screenStatus;
    private ZoomImageView screenImage;
    private FrameLayout screenContainer;
    private ZoomVideoView videoView;
    private volatile H264Decoder videoDecoder;
    private boolean videoFrameReady;
    private Dialog fullscreenDialog;
    private ZoomImageView fullscreenImage;
    private Bitmap currentScreenBitmap;
    private long lastPreviewDiagnostic;
    private final Handler previewMain = new Handler(Looper.getMainLooper());
    private final Object previewLock = new Object();
    private volatile long previewEpoch;
    private boolean previewRequested;
    private BridgeService previewService;
    private long previewAudioSession;
    private long previewRequestId = -1;
    private volatile ScreenConnection screenConnection;
    private ExecutorService screenDecoder;
    private Bitmap pendingScreenBitmap;
    private volatile boolean previewNetworkClosed;
    private final Runnable renderObserver = this::render;

    private final ServiceConnection binding = new ServiceConnection() {
        public void onServiceConnected(ComponentName name, IBinder binder) {
            service = ((BridgeService.LocalBinder) binder).service();
            service.observe(renderObserver);
            consumeMicrophoneRequest();
        }
        public void onServiceDisconnected(ComponentName name) {
            stopPreview("Просмотр остановлен: подключение завершено.", PreviewCloseReason.SERVICE_DISCONNECTED);
            service = null;
            render();
        }
    };

    @Override public void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        if (savedInstanceState != null) {
            pendingMicrophoneSession = savedInstanceState.getLong("micSession", -1);
            microphonePermissionGranted = savedInstanceState.getBoolean("micPermissionGranted", false);
        }
        LinearLayout shell = new LinearLayout(this);
        shell.setOrientation(LinearLayout.VERTICAL);
        shell.setBackgroundColor(BACKGROUND);
        ScrollView scroll = new ScrollView(this);
        page = scroll;
        scroll.setFillViewport(true);
        scroll.setClipToPadding(false);
        shell.addView(scroll, new LinearLayout.LayoutParams(-1, 0, 1));
        LinearLayout root = new LinearLayout(this) {
            @Override protected void onMeasure(int widthSpec, int heightSpec) {
                int width = Math.min(View.MeasureSpec.getSize(widthSpec), dp(640));
                super.onMeasure(View.MeasureSpec.makeMeasureSpec(width, View.MeasureSpec.EXACTLY), heightSpec);
            }
        };
        root.setOrientation(LinearLayout.VERTICAL);
        root.setPadding(dp(20), dp(20), dp(20), dp(20));
        scroll.addView(root, new ScrollView.LayoutParams(-1, -2, Gravity.TOP | Gravity.CENTER_HORIZONTAL));
        setContentView(shell);
        shell.setOnApplyWindowInsetsListener((view, insets) -> {
            int top, bottom, left, right;
            if (Build.VERSION.SDK_INT >= 30) {
                android.graphics.Insets safe = insets.getInsets(WindowInsets.Type.systemBars() | WindowInsets.Type.displayCutout() | WindowInsets.Type.ime());
                top = safe.top; bottom = safe.bottom; left = safe.left; right = safe.right;
            } else {
                top = insets.getSystemWindowInsetTop(); bottom = insets.getSystemWindowInsetBottom();
                left = insets.getSystemWindowInsetLeft(); right = insets.getSystemWindowInsetRight();
            }
            shell.setPadding(left, top, right, bottom);
            return insets;
        });
        TextView eyebrow = text("ТЕЛЕФОН И НОУТБУК · ОДНО ПОДКЛЮЧЕНИЕ", 10, GREEN);
        eyebrow.setLetterSpacing(0.08f);
        root.addView(eyebrow);
        TextView title = text("RemoteVoice", 28, INK);
        title.setTypeface(null, Typeface.BOLD);
        root.addView(title, margin(0, 6, 0, 6));
        root.addView(text("Говорите с телефона. Слушайте ноутбук в наушниках.", 15, MUTED));

        LinearLayout panel = new LinearLayout(this);
        panel.setOrientation(LinearLayout.VERTICAL);
        panel.setPadding(dp(20), dp(16), dp(20), dp(16));
        panel.setBackground(shape(SURFACE, 20));
        root.addView(panel, margin(0, 20, 0, 16));
        sessionLabel = text("СОСТОЯНИЕ СЕАНСА", 11, MUTED);
        sessionLabel.setLetterSpacing(0.06f);
        panel.addView(sessionLabel);
        status = text("Готов к подключению", 19, INK);
        status.setTypeface(null, Typeface.BOLD);
        status.setAccessibilityLiveRegion(View.ACCESSIBILITY_LIVE_REGION_POLITE);
        panel.addView(status, margin(0, 8, 0, 0));
        endpoint = text("Одна локальная сеть с ноутбуком", 13, MUTED);
        panel.addView(endpoint, margin(0, 8, 0, 0));

        pairingPanel = card(root, "Подключить ноутбук");
        pairingHint = text("Откройте RemoteVoice на ноутбуке и отсканируйте QR-код.", 14, MUTED);
        pairingPanel.addView(pairingHint, margin(0, 8, 0, 16));
        connect = button("Подключиться", true);
        pairingPanel.addView(connect, margin(0, 0, 0, 12));
        connect.setOnClickListener(view -> beginConnection());
        scan = button("Сканировать QR-код", false);
        pairingPanel.addView(scan, margin(0, 0, 0, 16));
        scan.setOnClickListener(view -> new IntentIntegrator(this)
                .setDesiredBarcodeFormats(IntentIntegrator.QR_CODE)
                .setPrompt("QR-код находится в Windows Host RemoteVoice")
                .setBeepEnabled(false).setOrientationLocked(false).initiateScan());

        TextView label = text("Или вставьте ссылку сопряжения", 13, MUTED);
        pairingPanel.addView(label);
        link = new EditText(this);
        link.setId(View.generateViewId());
        label.setLabelFor(link.getId());
        link.setHint("remotevoice://pair?…");
        link.setTextSize(14);
        link.setTextColor(INK);
        link.setHintTextColor(MUTED);
        link.setInputType(InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_VARIATION_URI);
        link.setMaxLines(2);
        link.setPadding(dp(12), dp(12), dp(12), dp(12));
        link.setBackground(shape(CONTROL, 12));
        link.setSaveEnabled(false);
        link.setImportantForAutofill(View.IMPORTANT_FOR_AUTOFILL_NO_EXCLUDE_DESCENDANTS);
        pairingPanel.addView(link, margin(0, 8, 0, 0));
        link.addTextChangedListener(new TextWatcher() {
            public void beforeTextChanged(CharSequence value, int start, int count, int after) {}
            public void onTextChanged(CharSequence value, int start, int before, int count) { render(); }
            public void afterTextChanged(Editable value) {}
        });

        audioPanel = card(root, "Микрофон телефона");
        audioPanel.addView(text("Звук ноутбука → телефон", 14, GREEN), margin(0, 8, 0, 16));
        microphone = button("Включить микрофон", true);
        microphone.setMinHeight(dp(64));
        audioPanel.addView(microphone, margin(0, 0, 0, 12));
        microphone.setOnClickListener(view -> {
            if (service == null || !service.connected) return;
            if (service.microphone) send(BridgeService.MIC_OFF);
            else if (checkSelfPermission(Manifest.permission.RECORD_AUDIO) == PackageManager.PERMISSION_GRANTED) send(BridgeService.MIC_ON);
            else {
                pendingMicrophoneSession = service.sessionId();
                microphonePermissionGranted = false;
                requestPermissions(new String[]{Manifest.permission.RECORD_AUDIO}, MIC_PERMISSION);
            }
        });
        audioPanel.addView(text("Микрофон включается только по вашему нажатию. Звук ноутбука работает и с выключенным микрофоном. Для разговора используйте наушники.", 13, MUTED));
        screenPanel = card(root, "Экран ноутбука");
        screenButton = button("Показать экран ноутбука", false);
        screenPanel.addView(screenButton, margin(0, 12, 0, 10));
        screenButton.setOnClickListener(view -> {
            if (previewRequested) stopPreview("Экран скрыт.", PreviewCloseReason.HIDDEN);
            else beginPreview();
        });
        screenStatus = text("Разрешите просмотр в RemoteVoice на ноутбуке. Здесь можно смотреть экран без управления.", 13, MUTED);
        screenStatus.setAccessibilityLiveRegion(View.ACCESSIBILITY_LIVE_REGION_POLITE);
        screenPanel.addView(screenStatus, margin(0, 0, 0, 10));
        screenImage = new ZoomImageView(this);
        screenImage.setBackgroundColor(Color.BLACK);
        screenImage.setContentDescription("Экран ноутбука в реальном времени");
        screenImage.setVisibility(View.GONE);
        LinearLayout.LayoutParams screenParams = new LinearLayout.LayoutParams(-1, dp(220));
        screenContainer = new FrameLayout(this);
        screenContainer.setVisibility(View.GONE);
        screenContainer.addView(screenImage, new FrameLayout.LayoutParams(-1, -1));
        screenPanel.addView(screenContainer, screenParams);
        fullscreenButton = button("На весь экран", false);
        fullscreenButton.setVisibility(View.GONE);
        fullscreenButton.setOnClickListener(view -> openFullscreen());
        screenPanel.addView(fullscreenButton, margin(0, 12, 0, 0));
        root.addView(text("Прямое соединение внутри локальной сети. Звук и изображение не записываются.", 12, MUTED), margin(0, 4, 0, 0));
        sessionFooter = new LinearLayout(this);
        sessionFooter.setOrientation(LinearLayout.VERTICAL);
        sessionFooter.setPadding(dp(20), dp(12), dp(20), dp(12));
        sessionFooter.setBackgroundColor(SURFACE);
        sessionFooter.addView(text("Завершить сеанс и вернуть звук на ноутбук", 12, MUTED), margin(0, 0, 0, 8));
        disconnect = button("Отключиться", false);
        sessionFooter.addView(disconnect, margin(0, 0, 0, 0));
        disconnect.setOnClickListener(view -> send(BridgeService.DISCONNECT));
        shell.addView(sessionFooter, new LinearLayout.LayoutParams(-1, -2));
        handleIntent(getIntent());
        render();
    }

    private void beginConnection() {
        if (service != null && (service.connected || service.connecting)) return;
        pendingMicrophoneSession = -1;
        String value = link.getText().toString().trim();
        boolean fresh = !value.isEmpty();
        if (!fresh) value = getSharedPreferences("endpoint", MODE_PRIVATE).getString("link", "");
        try { PairingInfo.parse(value); }
        catch (IllegalArgumentException exception) { toast("Отсканируй QR-код в Windows Host или вставь полную ссылку."); return; }
        Intent intent = new Intent(this, BridgeService.class).setAction(BridgeService.CONNECT)
                .putExtra("link", value).putExtra("fresh", fresh);
        if ((getApplicationInfo().flags & android.content.pm.ApplicationInfo.FLAG_DEBUGGABLE) != 0)
            intent.putExtra("reader_wait_probe", getIntent().getBooleanExtra("reader_wait_probe", false));
        startForegroundService(intent);
        link.clearFocus();
        getSystemService(android.view.inputmethod.InputMethodManager.class)
                .hideSoftInputFromWindow(link.getWindowToken(), 0);
        link.setText("");
        if (Build.VERSION.SDK_INT >= 33 && checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED) {
            requestPermissions(new String[]{Manifest.permission.POST_NOTIFICATIONS}, NOTIFICATION_PERMISSION);
        }
    }

    private void send(String action) {
        if (visible && service != null) startService(new Intent(this, BridgeService.class).setAction(action));
    }

    private void render() {
        if (status == null || microphone == null || sessionFooter == null) return;
        boolean active = service != null && (service.connected || service.connecting);
        boolean connected = service != null && service.connected;
        boolean mic = service != null && service.microphone;
        status.setText(service == null ? "Готов к подключению" : service.status);
        endpoint.setText(active ? "Ноутбук · " + service.host : "Одна локальная сеть с ноутбуком");
        sessionLabel.setText(connected ? "● СЕАНС АКТИВЕН" : active ? "ПОДКЛЮЧЕНИЕ…" : "НЕТ АКТИВНОГО СЕАНСА");
        sessionLabel.setTextColor(connected ? GREEN : MUTED);
        pairingPanel.setVisibility(active ? View.GONE : View.VISIBLE);
        audioPanel.setVisibility(connected ? View.VISIBLE : View.GONE);
        screenPanel.setVisibility(connected ? View.VISIBLE : View.GONE);
        sessionFooter.setVisibility(active ? View.VISIBLE : View.GONE);
        if (showingSession != connected) {
            showingSession = connected;
            page.post(() -> page.scrollTo(0, 0));
        }
        microphone.setEnabled(connected && !service.microphonePending);
        microphone.setText(connected && service.microphonePending ? "Подтверждение…"
                : mic ? "Выключить микрофон" : "Включить микрофон");
        styleButton(microphone, true, mic ? WARNING : GREEN);
        String saved = getSharedPreferences("endpoint", MODE_PRIVATE).getString("link", "");
        boolean hasSaved = false;
        try { PairingInfo.parse(saved); hasSaved = true; }
        catch (IllegalArgumentException ignored) { /* An invalid old endpoint needs a new QR. */ }
        pairingHint.setText(hasSaved
                ? "Ноутбук сохранён. Подключитесь снова или отсканируйте QR-код другого ноутбука."
                : "Откройте RemoteVoice на ноутбуке и отсканируйте QR-код. После подключения появятся микрофон и просмотр экрана.");
        connect.setEnabled(!active && (hasSaved || !link.getText().toString().trim().isEmpty()));
        scan.setEnabled(!active);
        disconnect.setEnabled(active);
        link.setEnabled(!active);
        if (previewRequested) {
            if (!visible || service != previewService || service == null || !service.connected
                    || service.sessionId() != previewAudioSession) {
                stopPreview("Просмотр остановлен: подключение завершено.", PreviewCloseReason.SESSION_CHANGED);
            } else if (previewRequestId != -1) {
                BridgeService.ScreenOffer offer = service.takeScreenOffer(previewAudioSession, previewRequestId);
                if (offer != null) {
                    previewRequestId = -1;
                    if (offer.error != null) stopPreview(offer.error, PreviewCloseReason.OFFER_ERROR);
                    else openPreview(offer);
                }
            }
        }
        screenButton.setEnabled(visible && service != null && service.connected);
        screenButton.setText(previewRequested ? "Скрыть экран ноутбука" : "Показать экран ноутбука");
    }

    private void beginPreview() {
        if (!visible || service == null || !service.connected) return;
        stopPreview("", PreviewCloseReason.RESTART);
        previewService = service;
        previewAudioSession = service.sessionId();
        previewRequested = true;
        previewNetworkClosed = false;
        previewRequestId = service.requestScreen();
        if (previewRequestId == -1) {
            stopPreview("Предыдущий запрос ещё завершается. Попробуйте снова через несколько секунд.", PreviewCloseReason.REQUEST_UNAVAILABLE);
            return;
        }
        getWindow().addFlags(WindowManager.LayoutParams.FLAG_SECURE);
        screenStatus.setText("Запрашиваем экран ноутбука…");
        screenImage.setVisibility(View.VISIBLE);
        screenContainer.setVisibility(View.VISIBLE);
        screenButton.setText("Скрыть экран ноутбука");
    }

    private boolean currentPreview(long epoch) {
        return visible && previewRequested && !previewNetworkClosed && previewEpoch == epoch
                && service != null && service == previewService && service.connected
                && service.sessionId() == previewAudioSession;
    }

    private void openPreview(BridgeService.ScreenOffer offer) {
        final long epoch = previewEpoch;
        if (!currentPreview(epoch)) return;
        screenStatus.setText("Подключаем просмотр экрана…");
        screenDecoder = Executors.newSingleThreadExecutor(task -> {
            Thread thread = new Thread(task, "remotevoice-screen-decode");
            thread.setDaemon(true);
            return thread;
        });
        final ExecutorService decoder = screenDecoder;
        ScreenConnection preview = new ScreenConnection(offer.pairing, new ScreenConnection.Listener() {
            public void onReady() {
                previewMain.post(() -> {
                    if (currentPreview(epoch) && screenConnection != null) screenConnection.requestNext();
                });
            }
            public void onFrame(byte[] jpeg) {
                try {
                    decoder.execute(() -> decodePreview(jpeg, epoch));
                } catch (RejectedExecutionException ignored) { /* Hidden previews accept no work. */ }
            }
            public void onVideoReady(int width, int height, int fps) {
                previewMain.post(() -> {
                    if (!currentPreview(epoch)) return;
                    screenImage.setVisibility(View.GONE);
                    ZoomVideoView video = new ZoomVideoView(MainActivity.this, width, height,
                            surface -> startVideoDecoder(epoch, width, height, surface));
                    videoView = video;
                    video.setContentDescription("Экран ноутбука. Масштаб меняется двумя пальцами.");
                    screenContainer.addView(video, new FrameLayout.LayoutParams(-1, -1));
                    previewMain.postDelayed(() -> {
                        if (currentPreview(epoch) && videoDecoder == null)
                            stopPreview("Не удалось открыть видеоповерхность. Попробуйте снова.", PreviewCloseReason.DECODE_FAILED);
                    }, 8000);
                });
            }
            public void onVideoFrame(byte[] accessUnit) {
                H264Decoder codec = videoDecoder;
                if (previewEpoch != epoch || previewNetworkClosed) return;
                if (codec == null || !codec.submit(accessUnit)) {
                    previewMain.post(() -> {
                        if (currentPreview(epoch)) stopPreview("Не удалось декодировать видео. Откройте просмотр снова.", PreviewCloseReason.DECODE_FAILED);
                    });
                }
            }
            public void onClosed(String message) {
                synchronized (previewLock) {
                    if (previewEpoch != epoch) return;
                    previewNetworkClosed = true;
                    if (pendingScreenBitmap != null) pendingScreenBitmap.recycle();
                    pendingScreenBitmap = null;
                }
                previewMain.post(() -> {
                    if (previewEpoch == epoch && previewRequested) stopPreview(message, PreviewCloseReason.TRANSPORT_CLOSED);
                });
            }
        }, offer.video);
        screenConnection = preview;
        preview.connect(offer.ticket);
    }

    private void startVideoDecoder(long epoch, int width, int height, Surface surface) {
        if (!currentPreview(epoch) || videoView == null || videoDecoder != null) return;
        final ZoomVideoView owner = videoView;
        H264Decoder codec = new H264Decoder(width, height, surface, new H264Decoder.Listener() {
            private long measuredAt = System.nanoTime(), previousCount;
            public void onInputAccepted() {
                ScreenConnection connection = screenConnection;
                if (previewEpoch == epoch && !previewNetworkClosed && connection != null) connection.requestNext();
            }
            public void onFrameDecoded(long count) {
                if (count == 1) previewMain.post(() -> {
                    if (!currentPreview(epoch)) return;
                    videoFrameReady = true;
                    fullscreenButton.setVisibility(View.VISIBLE);
                    screenStatus.setText("Экран ноутбука · видео · только просмотр");
                });
                long now = System.nanoTime();
                if (now - measuredAt >= 3_000_000_000L) {
                    double fps = (count - previousCount) * 1_000_000_000.0 / (now - measuredAt);
                    long shown = count; measuredAt = now; previousCount = count;
                    previewMain.post(() -> {
                        if (currentPreview(epoch)) tracePreview("video frames="+shown+" fps="
                                +String.format(java.util.Locale.ROOT,"%.2f",fps)+" fullscreen="+(fullscreenDialog!=null));
                    });
                }
            }
            public void onClosed(String reason) {
                previewMain.post(() -> {
                    if (!"cleanup_unconfirmed".equals(reason)) owner.release();
                    else owner.quarantine();
                    if (previewEpoch == epoch && previewRequested)
                        stopPreview("Просмотр видео остановлен. Откройте его снова.", PreviewCloseReason.DECODE_FAILED);
                });
            }
        }, (getApplicationInfo().flags & android.content.pm.ApplicationInfo.FLAG_DEBUGGABLE) != 0);
        videoDecoder = codec;
        if (!codec.start()) {
            videoDecoder = null;
            owner.release();
            stopPreview("Предыдущий видеодекодер ещё завершается. Попробуйте снова.", PreviewCloseReason.DECODE_FAILED);
            return;
        }
        if (screenConnection != null) screenConnection.requestNext();
    }

    private void decodePreview(byte[] jpeg, long epoch) {
        final Bitmap bitmap;
        try { bitmap = ScreenFrameDecoder.decode(jpeg); }
        catch (RuntimeException | OutOfMemoryError exception) {
            previewMain.post(() -> {
                if (previewEpoch == epoch && previewRequested) stopPreview("Не удалось декодировать кадр экрана. Просмотр остановлен.", PreviewCloseReason.DECODE_FAILED);
            });
            return;
        }
        synchronized (previewLock) {
            if (previewEpoch != epoch || previewNetworkClosed) { bitmap.recycle(); return; }
            if (pendingScreenBitmap != null) pendingScreenBitmap.recycle();
            pendingScreenBitmap = bitmap;
        }
        previewMain.post(() -> displayPreview(epoch));
    }

    private void displayPreview(long epoch) {
        Bitmap bitmap;
        synchronized (previewLock) {
            if (previewEpoch != epoch) return;
            bitmap = pendingScreenBitmap;
            pendingScreenBitmap = null;
        }
        if (bitmap == null) return;
        if (!currentPreview(epoch)) { bitmap.recycle(); return; }
        // Let Android release the previous displayed bitmap after the render thread is done.
        currentScreenBitmap = bitmap;
        screenImage.showFrame(bitmap);
        fullscreenButton.setVisibility(View.VISIBLE);
        if (fullscreenImage != null) fullscreenImage.showFrame(bitmap);
        screenStatus.setText("Экран ноутбука · только просмотр");
        long now = System.nanoTime();
        if (now-lastPreviewDiagnostic >= 3_000_000_000L) {
            lastPreviewDiagnostic = now;
            tracePreview("frame width="+bitmap.getWidth()+" height="+bitmap.getHeight()
                    +" fullscreen="+(fullscreenDialog != null));
        }
        if (screenConnection != null) screenConnection.requestNext();
    }

    private void stopPreview(String message, PreviewCloseReason reason) {
        boolean wasPreview = previewRequested;
        synchronized (previewLock) {
            ++previewEpoch;
            previewRequested = false;
            previewNetworkClosed = true;
            if (pendingScreenBitmap != null) pendingScreenBitmap.recycle();
            pendingScreenBitmap = null;
        }
        if (previewService != null && previewRequestId != -1) previewService.cancelScreenRequest(previewRequestId);
        previewRequestId = -1;
        previewService = null;
        closeFullscreen(reason);
        H264Decoder previousVideo = videoDecoder;
        videoDecoder = null;
        ZoomVideoView previousView = videoView;
        videoView = null; videoFrameReady = false;
        if (previousView != null) {
            previousView.hideFrame();
            if (previousVideo == null) previousView.release();
        }
        if (previousVideo != null) previousVideo.close();
        currentScreenBitmap = null;
        if (fullscreenButton != null) fullscreenButton.setVisibility(View.GONE);
        if (screenImage != null) {
            screenImage.clearFrame();
            screenImage.setVisibility(View.GONE);
        }
        if (screenContainer != null) screenContainer.setVisibility(View.GONE);
        getWindow().clearFlags(WindowManager.LayoutParams.FLAG_SECURE);
        ScreenConnection previous = screenConnection;
        screenConnection = null;
        if (previous != null) previous.close();
        if (screenDecoder != null) screenDecoder.shutdownNow();
        screenDecoder = null;
        if (screenStatus != null && message != null && !message.isEmpty()) screenStatus.setText(message);
        if (screenButton != null) screenButton.setText("Показать экран ноутбука");
        if (wasPreview) tracePreview("cleared");
    }

    private void openFullscreen() {
        if (!currentPreview(previewEpoch) || (currentScreenBitmap == null && !videoFrameReady) || fullscreenDialog != null
                || isFinishing() || isDestroyed()) return;
        Dialog dialog = new Dialog(this, android.R.style.Theme_Material_NoActionBar);
        fullscreenDialog = dialog;
        FrameLayout root = new FrameLayout(this);
        root.setBackgroundColor(Color.BLACK);
        ZoomImageView image = new ZoomImageView(this);
        final ZoomVideoView video = videoView;
        fullscreenImage = video == null ? image : null;
        image.setContentDescription("Экран ноутбука. Масштаб меняется двумя пальцами, увеличенный кадр можно перемещать.");
        if (video == null) root.addView(image, new FrameLayout.LayoutParams(-1, -1));
        else {
            if (video.getParent() instanceof ViewGroup) ((ViewGroup)video.getParent()).removeView(video);
            root.addView(video, new FrameLayout.LayoutParams(-1, -1));
        }
        LinearLayout controls = new LinearLayout(this);
        controls.setOrientation(LinearLayout.HORIZONTAL);
        controls.setPadding(dp(12), dp(12), dp(12), dp(12));
        Button fit = button("Вписать", false);
        fit.setOnClickListener(view -> { if (video != null) video.resetZoom(); else image.resetZoom(); });
        controls.addView(fit);
        Button close = button("Свернуть", false);
        close.setOnClickListener(view -> closeFullscreen(PreviewCloseReason.COLLAPSE));
        LinearLayout.LayoutParams closeParams = new LinearLayout.LayoutParams(-2, -2);
        closeParams.setMargins(dp(8), 0, 0, 0);
        controls.addView(close, closeParams);
        root.addView(controls, new FrameLayout.LayoutParams(-2, -2, Gravity.TOP | Gravity.END));
        TextView hint = text("Масштаб — двумя пальцами · двойное нажатие — увеличить / вписать", 12, INK);
        hint.setPadding(dp(16), dp(8), dp(16), dp(8));
        hint.setBackgroundColor(0xCC101815);
        root.addView(hint, new FrameLayout.LayoutParams(-1, -2, Gravity.BOTTOM));
        dialog.setContentView(root);
        dialog.getWindow().addFlags(WindowManager.LayoutParams.FLAG_SECURE);
        dialog.setOnCancelListener(ignored -> tracePreview("fullscreen_cancel"));
        dialog.setOnDismissListener(ignored -> {
            image.clearFrame();
            if (video != null && video == videoView && previewRequested && !video.isReleased()) {
                if (video.getParent() instanceof ViewGroup) ((ViewGroup)video.getParent()).removeView(video);
                screenContainer.addView(video, new FrameLayout.LayoutParams(-1, -1));
            }
            if (fullscreenDialog == dialog) { fullscreenDialog = null; fullscreenImage = null; }
            tracePreview("fullscreen_closed");
        });
        root.setOnApplyWindowInsetsListener((view, insets) -> {
            if (Build.VERSION.SDK_INT >= 30) {
                android.graphics.Insets safe = insets.getInsets(WindowInsets.Type.systemBars() | WindowInsets.Type.displayCutout());
                view.setPadding(safe.left, safe.top, safe.right, safe.bottom);
            } else view.setPadding(insets.getSystemWindowInsetLeft(), insets.getSystemWindowInsetTop(),
                    insets.getSystemWindowInsetRight(), insets.getSystemWindowInsetBottom());
            return insets;
        });
        dialog.show();
        dialog.getWindow().setLayout(-1, -1);
        if (Build.VERSION.SDK_INT >= 30) {
            android.view.WindowInsetsController controller = dialog.getWindow().getInsetsController();
            if (controller != null) {
                controller.setSystemBarsBehavior(android.view.WindowInsetsController.BEHAVIOR_SHOW_TRANSIENT_BARS_BY_SWIPE);
                controller.hide(WindowInsets.Type.systemBars());
            }
        } else dialog.getWindow().getDecorView().setSystemUiVisibility(View.SYSTEM_UI_FLAG_FULLSCREEN
                | View.SYSTEM_UI_FLAG_HIDE_NAVIGATION | View.SYSTEM_UI_FLAG_IMMERSIVE_STICKY);
        if (video == null) image.showFrame(currentScreenBitmap);
        tracePreview("fullscreen_open secure=true");
    }

    private void closeFullscreen(PreviewCloseReason reason) {
        Dialog dialog = fullscreenDialog;
        fullscreenDialog = null;
        if (fullscreenImage != null) fullscreenImage.clearFrame();
        fullscreenImage = null;
        if (dialog != null) {
            tracePreview("fullscreen_close_requested reason=" + reason.name());
            dialog.dismiss();
        }
    }

    private void tracePreview(String event) {
        if ((getApplicationInfo().flags & android.content.pm.ApplicationInfo.FLAG_DEBUGGABLE) != 0) {
            android.util.Log.i("RemoteVoicePreview", event+" clock_ns="+System.nanoTime());
        }
    }

    @Override public void onConfigurationChanged(Configuration configuration) {
        super.onConfigurationChanged(configuration);
        // Rotation relays out both surfaces without restarting capture or audio.
    }

    @Override protected void onStart() {
        super.onStart();
        bound = bindService(new Intent(this, BridgeService.class), binding, BIND_AUTO_CREATE);
    }
    @Override protected void onResume() { super.onResume(); visible = true; consumeMicrophoneRequest(); render(); }
    @Override protected void onPause() {
        visible = false;
        stopPreview("Экран скрыт. Для просмотра нажмите кнопку снова.", PreviewCloseReason.ACTIVITY_PAUSE);
        super.onPause();
    }
    @Override protected void onStop() {
        stopPreview("Экран скрыт. Для просмотра нажмите кнопку снова.", PreviewCloseReason.ACTIVITY_STOP);
        if (service != null) service.stopObserving(renderObserver);
        if (bound) { unbindService(binding); bound = false; }
        service = null;
        super.onStop();
    }
    @Override protected void onNewIntent(Intent intent) { super.onNewIntent(intent); setIntent(intent); handleIntent(intent); }
    private void handleIntent(Intent intent) {
        if (Intent.ACTION_VIEW.equals(intent.getAction()) && intent.getData() != null) {
            setPairingLink(intent.getData().toString());
            intent.setData(null);
        }
    }
    private void setPairingLink(String value) {
        try { PairingInfo.parse(value); link.setText(value); toast("Ссылка готова. Нажми «Подключиться»."); }
        catch (IllegalArgumentException exception) { toast("Это не QR-код RemoteVoice. Получи новый код на ноутбуке."); }
    }
    @Override protected void onActivityResult(int requestCode, int resultCode, Intent data) {
        IntentResult result = IntentIntegrator.parseActivityResult(requestCode, resultCode, data);
        if (result != null) { if (result.getContents() != null) setPairingLink(result.getContents()); }
        else super.onActivityResult(requestCode, resultCode, data);
    }
    @Override public void onRequestPermissionsResult(int requestCode, String[] permissions, int[] grantResults) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults);
        if (requestCode == MIC_PERMISSION) {
            if (grantResults.length > 0 && grantResults[0] == PackageManager.PERMISSION_GRANTED) {
                microphonePermissionGranted = true;
                consumeMicrophoneRequest();
            } else {
                pendingMicrophoneSession = -1;
                toast("Микрофон остаётся выключенным. Разрешение можно изменить в настройках Android.");
            }
        }
    }
    private void consumeMicrophoneRequest() {
        if (pendingMicrophoneSession == -1 || service == null) return;
        if (!service.connected || service.sessionId() != pendingMicrophoneSession) {
            pendingMicrophoneSession = -1;
            return;
        }
        if (visible && microphonePermissionGranted && checkSelfPermission(Manifest.permission.RECORD_AUDIO) == PackageManager.PERMISSION_GRANTED) {
            pendingMicrophoneSession = -1;
            send(BridgeService.MIC_ON);
        }
    }
    @Override protected void onSaveInstanceState(Bundle state) {
        state.putLong("micSession", pendingMicrophoneSession);
        state.putBoolean("micPermissionGranted", microphonePermissionGranted);
        super.onSaveInstanceState(state);
    }
    private void toast(String message) { Toast.makeText(this, message, Toast.LENGTH_LONG).show(); }
    private int dp(int value) { return Math.round(value * getResources().getDisplayMetrics().density); }
    private TextView text(String value, int size, int color) {
        TextView view = new TextView(this); view.setText(value); view.setTextSize(size); view.setTextColor(color);
        view.setLineSpacing(dp(3), 1f); return view;
    }
    private Button button(String title, boolean primary) {
        Button button = new Button(this); button.setText(title); button.setAllCaps(false); button.setTextSize(16);
        styleButton(button, primary, GREEN);
        button.setStateListAnimator(null);
        button.setPadding(dp(16), dp(12), dp(16), dp(12)); button.setMinHeight(dp(52)); return button;
    }
    private void styleButton(Button button, boolean primary, int accent) {
        int background = primary ? accent : CONTROL;
        int foreground = primary ? ACCENT_TEXT : INK;
        int[][] states = new int[][]{new int[]{-android.R.attr.state_enabled}, new int[]{}};
        button.setTextColor(new ColorStateList(states, new int[]{0xFF83988B, foreground}));
        StateListDrawable surfaces = new StateListDrawable();
        surfaces.addState(states[0], shape(0xFF23342B, 16));
        GradientDrawable focused = shape(background, 16);
        focused.setStroke(dp(2), primary ? ACCENT_TEXT : GREEN);
        surfaces.addState(new int[]{android.R.attr.state_focused}, focused);
        surfaces.addState(states[1], shape(background, 16));
        button.setBackground(new RippleDrawable(ColorStateList.valueOf(primary ? 0x33102019 : 0x3380E8BA), surfaces, null));
    }
    private LinearLayout card(LinearLayout parent, String title) {
        LinearLayout panel = new LinearLayout(this);
        panel.setOrientation(LinearLayout.VERTICAL);
        panel.setPadding(dp(20), dp(20), dp(20), dp(20));
        GradientDrawable background = shape(SURFACE, 20);
        background.setStroke(dp(1), BORDER);
        panel.setBackground(background);
        TextView heading = text(title, 18, INK);
        heading.setTypeface(null, Typeface.BOLD);
        if (Build.VERSION.SDK_INT >= 28) heading.setAccessibilityHeading(true);
        panel.addView(heading);
        parent.addView(panel, margin(0, 0, 0, 16));
        return panel;
    }
    private GradientDrawable shape(int color, int radius) {
        GradientDrawable background = new GradientDrawable(); background.setColor(color); background.setCornerRadius(dp(radius)); return background;
    }
    private LinearLayout.LayoutParams margin(int left, int top, int right, int bottom) {
        LinearLayout.LayoutParams params = new LinearLayout.LayoutParams(-1, -2);
        params.setMargins(dp(left), dp(top), dp(right), dp(bottom)); return params;
    }
}
