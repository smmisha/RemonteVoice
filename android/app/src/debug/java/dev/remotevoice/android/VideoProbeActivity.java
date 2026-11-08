package dev.remotevoice.android;

import android.app.Activity;
import android.app.Dialog;
import android.content.pm.ActivityInfo;
import android.graphics.Color;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.os.SystemClock;
import android.view.InputDevice;
import android.view.MotionEvent;
import android.view.Surface;
import android.view.ViewGroup;
import android.view.WindowManager;
import android.widget.FrameLayout;
import java.io.InputStream;
import java.io.ByteArrayOutputStream;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.List;

/** Debug-only, synthetic video/gesture/lifecycle probe. Never connects to a Host or starts audio. */
public final class VideoProbeActivity extends Activity {
    private final Handler main = new Handler(Looper.getMainLooper());
    private final List<byte[]> frames = new ArrayList<>();
    private FrameLayout root;
    private ZoomVideoView view;
    private H264Decoder decoder;
    private Dialog fullscreen;
    private Surface originalSurface;
    private int sent, cycle;
    private long decoded;
    private float zoom;
    private boolean completed, failed;

    @Override public void onCreate(Bundle state) {
        super.onCreate(state);
        getWindow().addFlags(WindowManager.LayoutParams.FLAG_SECURE);
        root = new FrameLayout(this); root.setBackgroundColor(Color.BLACK); setContentView(root);
        try (InputStream input = getAssets().open("synthetic-video.h264")) {
            ByteArrayOutputStream buffer = new ByteArrayOutputStream();
            byte[] chunk = new byte[4096]; int read;
            while ((read = input.read(chunk)) != -1) buffer.write(chunk, 0, read);
            byte[] stream = buffer.toByteArray();
            int start = -1;
            for (int i = 0; i + 5 < stream.length; ++i) {
                int prefix = stream[i] == 0 && stream[i+1] == 0 ?
                        (stream[i+2] == 1 ? 3 : stream[i+2] == 0 && stream[i+3] == 1 ? 4 : 0) : 0;
                if (prefix != 0 && (stream[i+prefix] & 31) == 9) {
                    if (start >= 0) frames.add(Arrays.copyOfRange(stream, start, i));
                    start = i; i += prefix;
                }
            }
            if (start >= 0) frames.add(Arrays.copyOfRange(stream, start, stream.length));
            require(frames.size() == 90, "fixture_count");
            startCycle();
            main.postDelayed(() -> { if (!completed) fail("probe_timeout"); }, 30000);
        } catch (Exception error) { fail("fixture_failed"); }
    }

    private void startCycle() {
        sent = 0; decoded = 0;
        view = new ZoomVideoView(this, 1280, 800, surface -> {
            originalSurface = surface;
            decoder = new H264Decoder(1280, 800, surface, new H264Decoder.Listener() {
                public void onInputAccepted() { main.postDelayed(VideoProbeActivity.this::feed, 33); }
                public void onFrameDecoded(long count) { main.post(() -> advance(count)); }
                public void onClosed(String reason) { main.post(() -> stopped(reason)); }
            });
            if (!decoder.start()) fail("start_failed"); else feed();
        });
        root.addView(view, new FrameLayout.LayoutParams(-1, (int)(240 * getResources().getDisplayMetrics().density)));
    }

    private void feed() {
        if (failed || completed || decoder == null || sent == frames.size()) return;
        if (!decoder.submit(frames.get(sent++))) fail("submit_failed");
    }

    private void advance(long count) {
        if (failed || completed) return;
        decoded = count;
        try {
            if (count == 15) {
                require(view.displayedFrames > 0, "no_surface_updates");
                fullscreen = new Dialog(this, android.R.style.Theme_Material_NoActionBar);
                FrameLayout container = new FrameLayout(this); container.setBackgroundColor(Color.BLACK);
                ((ViewGroup)view.getParent()).removeView(view);
                container.addView(view, new FrameLayout.LayoutParams(-1, -1));
                fullscreen.setContentView(container);
                fullscreen.getWindow().addFlags(WindowManager.LayoutParams.FLAG_SECURE);
                fullscreen.show(); fullscreen.getWindow().setLayout(-1, -1);
            }
            if (count == 30) {
                require(view.outputSurface() == originalSurface, "fullscreen_replaced_surface");
                pinch(); zoom = view.viewport.zoom(); require(zoom > 1.2f, "pinch_failed");
                setRequestedOrientation(ActivityInfo.SCREEN_ORIENTATION_LANDSCAPE);
            }
            if (count == 55) {
                require(view.getWidth() > view.getHeight(), "rotation_failed");
                require(view.viewport.zoom() == zoom, "rotation_lost_zoom");
                require(view.displayedFrames > 30, "fullscreen_frozen");
                ((ViewGroup)view.getParent()).removeView(view);
                fullscreen.dismiss(); fullscreen = null;
                root.addView(view, new FrameLayout.LayoutParams(-1, (int)(240 * getResources().getDisplayMetrics().density)));
            }
            if (count == 75) {
                require(view.outputSurface() == originalSurface, "collapse_replaced_surface");
                require(view.viewport.zoom() == zoom, "collapse_lost_zoom");
                view.resetZoom(); require(view.viewport.zoom() == 1, "fit_failed");
                setRequestedOrientation(ActivityInfo.SCREEN_ORIENTATION_PORTRAIT);
            }
            if (count == frames.size()) {
                require(view.displayedFrames >= 60, "rendering_stalled");
                view.hideFrame(); decoder.close();
            }
        } catch (AssertionError | RuntimeException error) { fail(error.getMessage()); }
    }

    private void stopped(String reason) {
        if (!"closed".equals(reason) || decoded != frames.size()) {
            if (view != null) {
                if ("cleanup_unconfirmed".equals(reason)) view.quarantine(); else view.release();
            }
            if (!failed) fail("codec_" + reason);
            return;
        }
        long displayed = view.displayedFrames;
        view.release();
        require(view.isReleased(), "surface_not_released");
        decoder = null;
        android.util.Log.i("RemoteVoiceVideoProbe", "cycle="+cycle+" decoded="+decoded+" displayed="+displayed+" cleanup=confirmed");
        if (++cycle < 2 && !failed) main.postDelayed(this::startCycle, 100);
        else if (!failed) {
            completed = true;
            android.util.Log.i("RemoteVoiceVideoProbe", "PASS cycles=2 mic=false host=false pixels_saved=false");
            finish();
        }
    }

    private void pinch() {
        float center = view.getWidth()/2f, y = view.getHeight()/2f;
        float unit = view.getWidth()/10f;
        long down = SystemClock.uptimeMillis();
        event(down, 0, MotionEvent.ACTION_DOWN, y, center-unit);
        event(down, 16, MotionEvent.ACTION_POINTER_DOWN | (1 << MotionEvent.ACTION_POINTER_INDEX_SHIFT), y, center-unit, center+unit);
        for (int step = 2; step <= 5; ++step) event(down, step*16, MotionEvent.ACTION_MOVE, y, center-unit*step, center+unit*step);
        event(down, 96, MotionEvent.ACTION_POINTER_UP | (1 << MotionEvent.ACTION_POINTER_INDEX_SHIFT), y, center-unit*5, center+unit*5);
        event(down, 112, MotionEvent.ACTION_UP, y, center-unit*5);
    }

    private void event(long down, int step, int action, float y, float... xs) {
        MotionEvent.PointerProperties[] properties = new MotionEvent.PointerProperties[xs.length];
        MotionEvent.PointerCoords[] coords = new MotionEvent.PointerCoords[xs.length];
        for (int i=0; i<xs.length; i++) {
            properties[i]=new MotionEvent.PointerProperties(); properties[i].id=i;
            properties[i].toolType=MotionEvent.TOOL_TYPE_FINGER;
            coords[i]=new MotionEvent.PointerCoords(); coords[i].x=xs[i]; coords[i].y=y;
            coords[i].pressure=1; coords[i].size=1;
        }
        MotionEvent motion=MotionEvent.obtain(down,down+step,action,xs.length,properties,coords,0,0,1,1,0,0,InputDevice.SOURCE_TOUCHSCREEN,0);
        try { view.dispatchTouchEvent(motion); } finally { motion.recycle(); }
    }

    private static void require(boolean value, String reason) { if (!value) throw new AssertionError(reason); }
    private void fail(String reason) {
        if (failed || completed) return;
        failed = true;
        android.util.Log.e("RemoteVoiceVideoProbe", "FAIL " + reason);
        if (view != null) view.hideFrame();
        if (decoder != null) decoder.close(); else if (view != null) view.release();
        if (fullscreen != null) { fullscreen.dismiss(); fullscreen = null; }
        finish();
    }
    @Override protected void onPause() { if (!completed && !failed) fail("activity_paused"); super.onPause(); }
}
