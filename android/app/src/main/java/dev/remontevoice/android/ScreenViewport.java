package dev.remontevoice.android;

/** Bitmap-independent, bounded fit/zoom/pan geometry shared by both preview surfaces. */
final class ScreenViewport {
    private int width,height,imageWidth,imageHeight;
    private float zoom=1,panX,panY;

    void layout(int width,int height,int imageWidth,int imageHeight) {
        if (width<=0 || height<=0 || imageWidth<=0 || imageHeight<=0) return;
        float oldWidth=this.imageWidth*displayScale(),oldHeight=this.imageHeight*displayScale();
        float fractionX=oldWidth>0?panX/oldWidth:0,fractionY=oldHeight>0?panY/oldHeight:0;
        this.width=width;this.height=height;this.imageWidth=imageWidth;this.imageHeight=imageHeight;
        panX=fractionX*imageWidth*displayScale();panY=fractionY*imageHeight*displayScale();
        clamp();
    }
    float zoom() { return zoom; }
    float displayScale() {
        return imageWidth>0 && imageHeight>0?Math.min((float)width/imageWidth,(float)height/imageHeight)*zoom:1;
    }
    float left() { return (width-imageWidth*displayScale())/2+panX; }
    float top() { return (height-imageHeight*displayScale())/2+panY; }
    void reset() { zoom=1;panX=0;panY=0; }
    void zoomBy(float factor,float focusX,float focusY) {
        if (!Float.isFinite(factor) || factor<=0 || !Float.isFinite(focusX) || !Float.isFinite(focusY)) return;
        float next=Math.max(1,Math.min(6,zoom*factor)),ratio=next/zoom;
        float left=focusX-(focusX-left())*ratio,top=focusY-(focusY-top())*ratio;
        zoom=next;
        panX=left-(width-imageWidth*displayScale())/2;
        panY=top-(height-imageHeight*displayScale())/2;
        clamp();
    }
    void pan(float dx,float dy) {
        if (!Float.isFinite(dx) || !Float.isFinite(dy)) return;
        panX+=dx;panY+=dy;clamp();
    }
    private void clamp() {
        float maxX=Math.max(0,(imageWidth*displayScale()-width)/2),maxY=Math.max(0,(imageHeight*displayScale()-height)/2);
        panX=Math.max(-maxX,Math.min(maxX,panX));panY=Math.max(-maxY,Math.min(maxY,panY));
    }
}
