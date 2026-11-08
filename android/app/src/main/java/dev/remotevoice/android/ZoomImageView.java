package dev.remotevoice.android;

import android.content.Context;
import android.annotation.SuppressLint;
import android.graphics.Bitmap;
import android.graphics.Matrix;
import android.view.GestureDetector;
import android.view.MotionEvent;
import android.view.ScaleGestureDetector;
import android.widget.ImageView;

/** Pinch and pan stay local to the displayed bitmap; they never send remote input. */
final class ZoomImageView extends ImageView {
    final ScreenViewport viewport=new ScreenViewport();
    private final Matrix matrix=new Matrix();
    private final ScaleGestureDetector scale;
    private final GestureDetector gestures;
    private int imageWidth,imageHeight;

    ZoomImageView(Context context) {
        super(context);setScaleType(ScaleType.MATRIX);setClickable(true);
        scale=new ScaleGestureDetector(context,new ScaleGestureDetector.SimpleOnScaleGestureListener() {
            @Override public boolean onScale(ScaleGestureDetector detector) {
                viewport.zoomBy(detector.getScaleFactor(),detector.getFocusX(),detector.getFocusY());updateMatrix();return true;
            }
        });
        gestures=new GestureDetector(context,new GestureDetector.SimpleOnGestureListener() {
            @Override public boolean onDown(MotionEvent event) { return true; }
            @Override public boolean onSingleTapConfirmed(MotionEvent event) { performClick();return true; }
            @Override public boolean onDoubleTap(MotionEvent event) {
                if (viewport.zoom()>1) viewport.reset();else viewport.zoomBy(2.5f,event.getX(),event.getY());
                updateMatrix();return true;
            }
            @Override public boolean onScroll(MotionEvent first,MotionEvent next,float dx,float dy) {
                if (scale.isInProgress() || viewport.zoom()<=1) return false;
                viewport.pan(-dx,-dy);updateMatrix();return true;
            }
        });
    }
    void showFrame(Bitmap bitmap) {
        setImageBitmap(bitmap);
        // ImageView matrices use drawable dimensions, including Android density.
        imageWidth=getDrawable().getIntrinsicWidth();imageHeight=getDrawable().getIntrinsicHeight();
        layoutViewport();
    }
    void clearFrame() { setImageDrawable(null);imageWidth=0;imageHeight=0;viewport.reset();updateMatrix(); }
    void resetZoom() { viewport.reset();updateMatrix(); }
    private void layoutViewport() { viewport.layout(getWidth(),getHeight(),imageWidth,imageHeight);updateMatrix(); }
    private void updateMatrix() {
        matrix.setScale(viewport.displayScale(),viewport.displayScale());matrix.postTranslate(viewport.left(),viewport.top());
        setImageMatrix(matrix);
    }
    @Override protected void onSizeChanged(int w,int h,int oldW,int oldH) { super.onSizeChanged(w,h,oldW,oldH);layoutViewport(); }
    @Override public boolean performClick() { return super.performClick(); }
    @SuppressLint("ClickableViewAccessibility") // GestureDetector's confirmed tap calls performClick above.
    @Override public boolean onTouchEvent(MotionEvent event) {
        if (getParent()!=null && (event.getPointerCount()>1 || viewport.zoom()>1)) getParent().requestDisallowInterceptTouchEvent(true);
        scale.onTouchEvent(event);gestures.onTouchEvent(event);
        if (getParent()!=null && (event.getActionMasked()==MotionEvent.ACTION_UP || event.getActionMasked()==MotionEvent.ACTION_CANCEL))
            getParent().requestDisallowInterceptTouchEvent(false);
        return true;
    }
}
