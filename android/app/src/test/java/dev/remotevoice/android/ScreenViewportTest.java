package dev.remotevoice.android;

import org.junit.Test;
import static org.junit.Assert.*;

public final class ScreenViewportTest {
    @Test public void fitCentersWithoutStretching() {
        ScreenViewport v=new ScreenViewport();v.layout(1080,2340,864,540);
        assertEquals(1.25f,v.displayScale(),.001f);
        assertEquals(0f,v.left(),.001f);assertEquals(832.5f,v.top(),.001f);
    }
    @Test public void pinchKeepsFocusOnTheSameImagePoint() {
        ScreenViewport v=new ScreenViewport();v.layout(1200,800,1920,1200);
        float x=(300-v.left())/v.displayScale(),y=(300-v.top())/v.displayScale();
        v.zoomBy(2,300,300);
        assertEquals(x,(300-v.left())/v.displayScale(),.001f);
        assertEquals(y,(300-v.top())/v.displayScale(),.001f);
    }
    @Test public void panNeverExposesEmptyEdgesOfZoomedImage() {
        ScreenViewport v=new ScreenViewport();v.layout(1200,800,1920,1200);v.zoomBy(3,600,400);
        v.pan(100000,-100000);
        assertEquals(0,v.left(),.001f);
        assertEquals(800-1200*v.displayScale(),v.top(),.001f);
    }
    @Test public void newFramesAndRotationPreserveZoom() {
        ScreenViewport v=new ScreenViewport();v.layout(1200,800,1920,1200);v.zoomBy(3,400,300);
        float zoom=v.zoom();v.layout(1200,800,1920,1200);assertEquals(zoom,v.zoom(),0);
        v.layout(800,1200,1920,1200);assertEquals(zoom,v.zoom(),0);
        assertTrue(Float.isFinite(v.left()));assertTrue(Float.isFinite(v.top()));
    }
    @Test public void scaleIsBoundedAndInvalidGesturesDoNotPoisonMatrix() {
        ScreenViewport v=new ScreenViewport();v.layout(1200,800,1920,1200);
        v.zoomBy(Float.NaN,1,1);v.zoomBy(Float.POSITIVE_INFINITY,1,1);assertEquals(1,v.zoom(),0);
        v.zoomBy(100,600,400);assertEquals(6,v.zoom(),0);
        v.zoomBy(.001f,600,400);assertEquals(1,v.zoom(),0);
        v.pan(Float.NaN,Float.POSITIVE_INFINITY);assertTrue(Float.isFinite(v.left()));
        v.zoomBy(2,600,400);v.reset();assertEquals(1,v.zoom(),0);
    }
}
