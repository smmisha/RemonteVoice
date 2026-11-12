package dev.remontevoice.android;

import android.annotation.SuppressLint;
import android.content.Context;
import android.graphics.Color;
import android.graphics.Matrix;
import android.graphics.SurfaceTexture;
import android.view.GestureDetector;
import android.view.MotionEvent;
import android.view.ScaleGestureDetector;
import android.view.Surface;
import android.view.TextureView;
import android.view.ViewGroup;
import android.widget.FrameLayout;
import java.util.function.Consumer;

/** One retained decoder surface moves between inline/fullscreen without restarting video. */
final class ZoomVideoView extends FrameLayout implements TextureView.SurfaceTextureListener {
    private static Object[] quarantinedSurface;
    final ScreenViewport viewport = new ScreenViewport();
    private final TextureView texture;
    private final int videoWidth, videoHeight;
    private final ScaleGestureDetector scale;
    private final GestureDetector gestures;
    private SurfaceTexture retained;
    private Surface surface;
    private Consumer<Surface> ready;
    private boolean released;
    long displayedFrames;

    ZoomVideoView(Context context, int width, int height, Consumer<Surface> ready) {
        super(context);
        videoWidth = width; videoHeight = height; this.ready = ready;
        setBackgroundColor(Color.BLACK); setClipChildren(true); setClickable(true);
        texture = new TextureView(context);
        texture.setOpaque(false);
        texture.setSurfaceTextureListener(this);
        addView(texture, new FrameLayout.LayoutParams(-1, -1));
        scale = new ScaleGestureDetector(context, new ScaleGestureDetector.SimpleOnScaleGestureListener() {
            @Override public boolean onScale(ScaleGestureDetector detector) {
                viewport.zoomBy(detector.getScaleFactor(), detector.getFocusX(), detector.getFocusY());
                updateTransform(); return true;
            }
        });
        gestures = new GestureDetector(context, new GestureDetector.SimpleOnGestureListener() {
            @Override public boolean onDown(MotionEvent event) { return true; }
            @Override public boolean onSingleTapConfirmed(MotionEvent event) { performClick(); return true; }
            @Override public boolean onDoubleTap(MotionEvent event) {
                if (viewport.zoom() > 1) viewport.reset();
                else viewport.zoomBy(2.5f, event.getX(), event.getY());
                updateTransform(); return true;
            }
            @Override public boolean onScroll(MotionEvent first, MotionEvent next, float dx, float dy) {
                if (scale.isInProgress() || viewport.zoom() <= 1) return false;
                viewport.pan(-dx, -dy); updateTransform(); return true;
            }
        });
    }

    void resetZoom() { viewport.reset(); updateTransform(); }
    boolean hasSurface() { return !released && surface != null && surface.isValid(); }
    Surface outputSurface() { return surface; }
    boolean isReleased() { return released; }

    private void updateTransform() {
        if (getWidth() <= 0 || getHeight() <= 0) return;
        Matrix transform = new Matrix();
        transform.setScale(videoWidth * viewport.displayScale() / getWidth(),
                videoHeight * viewport.displayScale() / getHeight());
        transform.postTranslate(viewport.left(), viewport.top());
        texture.setTransform(transform);
    }

    @Override protected void onSizeChanged(int w, int h, int oldW, int oldH) {
        super.onSizeChanged(w, h, oldW, oldH);
        viewport.layout(w, h, videoWidth, videoHeight); updateTransform();
    }

    @Override public void onSurfaceTextureAvailable(SurfaceTexture incoming, int width, int height) {
        if (released) return;
        if (retained == null) {
            retained = incoming;
            retained.setDefaultBufferSize(videoWidth, videoHeight);
            surface = new Surface(retained);
            Consumer<Surface> callback = ready; ready = null;
            if (callback != null) callback.accept(surface);
        } else if (incoming != retained) {
            // setSurfaceTexture releases the temporary replacement texture.
            // The retained one is still owned by this preview and its codec.
            texture.setSurfaceTexture(retained);
        }
        updateTransform();
    }

    @Override public boolean onSurfaceTextureDestroyed(SurfaceTexture value) {
        // Reparenting to/from fullscreen must not abandon the codec's buffer
        // queue. release() is called only AFTER the decoder confirms cleanup.
        return released;
    }
    @Override public void onSurfaceTextureSizeChanged(SurfaceTexture value, int width, int height) { updateTransform(); }
    @Override public void onSurfaceTextureUpdated(SurfaceTexture value) { ++displayedFrames; }

    /** Hide immediately on revocation, but retain native resources until codec teardown. */
    void hideFrame() {
        ready = null;
        setVisibility(GONE);
        if (getParent() instanceof ViewGroup) ((ViewGroup)getParent()).removeView(this);
    }

    /** Main thread, after codec.release() (or if no decoder was ever started). */
    void release() {
        if (released) return;
        hideFrame();
        if (surface != null) surface.release();
        surface = null;
        if (retained != null) retained.release();
        retained = null; released = true;
        texture.setSurfaceTextureListener(null);
    }

    /** A failed codec.release keeps its buffer queue alive, without retaining this Activity. */
    void quarantine() {
        hideFrame();
        quarantinedSurface = new Object[]{surface, retained};
        texture.setSurfaceTextureListener(null);
    }

    @Override public boolean onInterceptTouchEvent(MotionEvent event) { return true; }
    @Override public boolean performClick() { return super.performClick(); }
    @SuppressLint("ClickableViewAccessibility")
    @Override public boolean onTouchEvent(MotionEvent event) {
        if (getParent() != null && (event.getPointerCount() > 1 || viewport.zoom() > 1))
            getParent().requestDisallowInterceptTouchEvent(true);
        scale.onTouchEvent(event); gestures.onTouchEvent(event);
        if (getParent() != null && (event.getActionMasked() == MotionEvent.ACTION_UP
                || event.getActionMasked() == MotionEvent.ACTION_CANCEL))
            getParent().requestDisallowInterceptTouchEvent(false);
        return true;
    }
}
