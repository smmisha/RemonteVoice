package dev.remotevoice.android;

import android.app.Activity;
import android.app.Instrumentation;
import android.app.Dialog;
import android.content.Intent;
import android.content.pm.ActivityInfo;
import android.graphics.Bitmap;
import android.graphics.Matrix;
import android.os.Bundle;
import android.os.SystemClock;
import android.view.InputDevice;
import android.view.MotionEvent;
import android.view.WindowManager;
import android.widget.Button;
import java.lang.reflect.Field;
import java.util.function.Supplier;
import java.util.function.BooleanSupplier;
import java.util.concurrent.atomic.AtomicReference;

/** Real Android gesture detector with synthetic bitmap only; no audio/capture permission. */
public final class ViewerInstrumentation extends Instrumentation {
    private boolean live;
    @Override public void onCreate(Bundle arguments) { super.onCreate(arguments);live=arguments!=null && "true".equals(arguments.getString("live"));start(); }
    private static void require(boolean condition,String message) {
        if(!condition)throw new AssertionError(message);
    }
    private static void event(ZoomImageView view,long down,int step,int action,float... xs) {
        MotionEvent.PointerProperties[] properties=new MotionEvent.PointerProperties[xs.length];
        MotionEvent.PointerCoords[] coords=new MotionEvent.PointerCoords[xs.length];
        for(int i=0;i<xs.length;i++) {
            properties[i]=new MotionEvent.PointerProperties();properties[i].id=i;properties[i].toolType=MotionEvent.TOOL_TYPE_FINGER;
            coords[i]=new MotionEvent.PointerCoords();coords[i].x=xs[i];coords[i].y=350;coords[i].pressure=1;coords[i].size=1;
        }
        MotionEvent e=MotionEvent.obtain(down,down+step,action,xs.length,properties,coords,0,0,1,1,0,0,InputDevice.SOURCE_TOUCHSCREEN,0);
        try { view.dispatchTouchEvent(e); } finally { e.recycle(); }
    }
    @Override public void onStart() {
        AtomicReference<Throwable> failure=new AtomicReference<>();
        runOnMainSync(()->{
            Bitmap bitmap=Bitmap.createBitmap(864,540,Bitmap.Config.ARGB_8888);
            ZoomImageView view=new ZoomImageView(getTargetContext());
            try {
                view.layout(0,0,1080,700);view.showFrame(bitmap);
                float[] matrix=new float[9];view.getImageMatrix().getValues(matrix);
                require(Math.abs(view.getDrawable().getIntrinsicWidth()*matrix[Matrix.MSCALE_X]-1080)<.1,"Density-correct fit");
                long down=SystemClock.uptimeMillis();
                event(view,down,0,MotionEvent.ACTION_DOWN,400);
                event(view,down,16,MotionEvent.ACTION_POINTER_DOWN|(1<<MotionEvent.ACTION_POINTER_INDEX_SHIFT),400,680);
                event(view,down,32,MotionEvent.ACTION_MOVE,320,760);
                event(view,down,48,MotionEvent.ACTION_MOVE,230,850);
                event(view,down,64,MotionEvent.ACTION_MOVE,140,940);
                event(view,down,80,MotionEvent.ACTION_POINTER_UP|(1<<MotionEvent.ACTION_POINTER_INDEX_SHIFT),140,940);
                event(view,down,96,MotionEvent.ACTION_UP,140);
                require(view.viewport.zoom()>1.2,"Pinch must zoom through Android ScaleGestureDetector");
                float zoom=view.viewport.zoom();view.showFrame(bitmap);
                require(view.viewport.zoom()==zoom,"Incoming frame must preserve zoom");
                view.layout(0,0,700,1080);
                require(view.viewport.zoom()==zoom,"Rotation must preserve zoom");
                view.resetZoom();require(view.viewport.zoom()==1,"Fit control resets zoom");
                view.clearFrame();require(view.getDrawable()==null,"Closing viewer clears drawable");
            } catch(Throwable error) { failure.set(error); }
            finally { view.clearFrame();bitmap.recycle(); }
        });
        if(failure.get()==null && live)try { liveSession(); }catch(Throwable error){failure.set(error);}
        Bundle results=new Bundle();
        results.putString("stream",failure.get()==null?(live?"PASS: native pinch plus real fullscreen, secure surface, arriving frames, rotation, Back, Home, disconnect. No pixels saved.":"PASS: Android pinch, density, new-frame zoom, resize, fit, clear. Synthetic bitmap only."):"FAIL: "+failure.get());
        finish(failure.get()==null?Activity.RESULT_OK:Activity.RESULT_CANCELED,results);
    }
    private static Object field(Object object,String name) {
        try { Field f=object.getClass().getDeclaredField(name);f.setAccessible(true);return f.get(object); }
        catch(ReflectiveOperationException error){throw new AssertionError(error);}
    }
    private <T> T main(Supplier<T> action) {
        AtomicReference<T> value=new AtomicReference<>();AtomicReference<Throwable> error=new AtomicReference<>();
        runOnMainSync(()->{try{value.set(action.get());}catch(Throwable e){error.set(e);}});
        if(error.get()!=null)throw new AssertionError(error.get());return value.get();
    }
    private void waitFor(BooleanSupplier predicate) {
        long end=SystemClock.uptimeMillis()+15000;
        while(SystemClock.uptimeMillis()<end){if(main(()->predicate.getAsBoolean()))return;SystemClock.sleep(100);}
        throw new AssertionError("Expected live viewer state not observed");
    }
    private boolean connected(Activity activity) {
        Object service=field(activity,"service");return service!=null && Boolean.TRUE.equals(field(service,"connected"));
    }
    private void click(Activity activity,String name) { main(()->{((Button)field(activity,name)).performClick();return null;}); }
    private Activity launch() {
        Intent intent=new Intent().setClassName(getTargetContext().getPackageName(),"dev.remotevoice.android.MainActivity")
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK|Intent.FLAG_ACTIVITY_REORDER_TO_FRONT);
        return startActivitySync(intent);
    }
    private void liveSession() {
        Activity activity=launch();
        int orientation=activity.getRequestedOrientation();
        try {
            waitFor(()->field(activity,"service")!=null);
            click(activity,"connect");waitFor(()->connected(activity));
            require(main(()->Boolean.FALSE.equals(field(field(activity,"service"),"microphone"))),"Microphone must remain off");
            click(activity,"screenButton");waitFor(()->field(activity,"currentScreenBitmap")!=null);
            click(activity,"fullscreenButton");
            waitFor(()->field(activity,"fullscreenDialog")!=null);
            waitFor(()->{ZoomImageView image=(ZoomImageView)field(activity,"fullscreenImage");return image!=null && image.getWidth()>0 && image.getHeight()>0;});
            main(()->{
                Dialog d=(Dialog)field(activity,"fullscreenDialog");
                require(d.isShowing(),"Fullscreen dialog showing");
                require((d.getWindow().getAttributes().flags&WindowManager.LayoutParams.FLAG_SECURE)!=0,"Secure fullscreen surface");
                ZoomImageView image=(ZoomImageView)field(activity,"fullscreenImage");
                require(image.getWidth()>0 && image.getHeight()>0,"Fullscreen laid out");
                image.viewport.zoomBy(2.5f,image.getWidth()/2f,image.getHeight()/2f);image.showFrame((Bitmap)field(activity,"currentScreenBitmap"));
                return null;
            });
            SystemClock.sleep(7000);
            require(main(()->((ZoomImageView)field(activity,"fullscreenImage")).viewport.zoom()==2.5f),"Real arriving frames preserve zoom");
            main(()->{activity.setRequestedOrientation(ActivityInfo.SCREEN_ORIENTATION_LANDSCAPE);return null;});
            waitFor(()->{ZoomImageView image=(ZoomImageView)field(activity,"fullscreenImage");return image!=null && image.getWidth()>image.getHeight();});
            require(main(()->!activity.isDestroyed() && connected(activity)),"Rotation keeps activity/audio session");
            main(()->{((Dialog)field(activity,"fullscreenDialog")).onBackPressed();return null;});
            waitFor(()->field(activity,"fullscreenDialog")==null && field(activity,"currentScreenBitmap")!=null);
            click(activity,"fullscreenButton");
            main(()->{activity.moveTaskToBack(true);return null;});
            waitFor(()->field(activity,"fullscreenDialog")==null && field(activity,"currentScreenBitmap")==null);
            // Observe the service separately: Activity unbinds in onStop.
            Activity returned=launch();waitFor(()->connected(returned));
            require(main(()->!Boolean.TRUE.equals(field(returned,"previewRequested"))),"Returning must not reopen preview");
            click(returned,"disconnect");waitFor(()->!connected(returned));
        } finally {
            main(()->{activity.setRequestedOrientation(orientation);return null;});
            getTargetContext().startService(new Intent().setClassName(getTargetContext().getPackageName(),"dev.remotevoice.android.BridgeService")
                    .setAction("dev.remotevoice.DISCONNECT"));
        }
    }
}
