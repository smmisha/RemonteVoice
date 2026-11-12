package dev.remontevoice.android;

import android.app.Activity;
import android.app.Dialog;
import android.app.Instrumentation;
import android.content.Intent;
import android.content.pm.ActivityInfo;
import android.os.Bundle;
import android.os.SystemClock;
import android.view.Surface;
import android.view.WindowManager;
import android.widget.Button;
import java.lang.reflect.Field;
import java.util.concurrent.atomic.AtomicReference;
import java.util.function.BooleanSupplier;
import java.util.function.Supplier;

/** Debug-only checks through real MainActivity controls. Requires owner-enabled Host screen permission. */
public final class VideoMainInstrumentation extends Instrumentation {
    private String runId="manual";
    private boolean audioOnly;
    private boolean transitions;
    private boolean primingCancel;
    private boolean playbackReplay;
    private boolean playbackBurst;
    private boolean playbackStopReplay;
    private boolean playbackRecoveryStopReplay;
    private boolean playbackAdaptiveReplay;
    private int replayGapMillis=324;
    private int backgroundMillis=8000;
    private boolean strictAudio;
    private boolean readerWaitProbe;
    private boolean readerWaitControls;
    @Override public void onCreate(Bundle arguments) {
        super.onCreate(arguments);
        if(arguments!=null) {
            runId=arguments.getString("run_id","manual");
            audioOnly="audio_only".equals(arguments.getString("mode"));
            transitions="transitions".equals(arguments.getString("mode"));
            primingCancel="priming_cancel".equals(arguments.getString("mode"));
            playbackBurst="playback_burst".equals(arguments.getString("mode"));
            playbackStopReplay="playback_stop".equals(arguments.getString("mode"));
            playbackRecoveryStopReplay="playback_recovery_stop".equals(arguments.getString("mode"));
            playbackAdaptiveReplay="playback_adaptive".equals(arguments.getString("mode"));
            playbackReplay=playbackBurst || playbackAdaptiveReplay || "playback_replay".equals(arguments.getString("mode"));
            replayGapMillis=Integer.parseInt(arguments.getString("replay_gap_ms","324"));
            if(replayGapMillis<100 || replayGapMillis>1000) throw new IllegalArgumentException("Invalid replay gap");
            backgroundMillis=Integer.parseInt(arguments.getString("background_ms","8000"));
            if(backgroundMillis<1000 || backgroundMillis>300000) throw new IllegalArgumentException("Invalid background duration");
            strictAudio="true".equals(arguments.getString("strict_audio"));
            readerWaitProbe="true".equals(arguments.getString("reader_wait_probe"));
            readerWaitControls="reader_wait_controls".equals(arguments.getString("mode"));
        }
        start();
    }
    private static void require(boolean ok, String reason) { if (!ok) throw new AssertionError(reason); }
    private static Object field(Object object, String name) {
        try { Field f=object.getClass().getDeclaredField(name); f.setAccessible(true); return f.get(object); }
        catch (ReflectiveOperationException error) { throw new AssertionError(error); }
    }
    private <T> T main(Supplier<T> action) {
        AtomicReference<T> result=new AtomicReference<>(); AtomicReference<Throwable> error=new AtomicReference<>();
        runOnMainSync(() -> { try { result.set(action.get()); } catch (Throwable e) { error.set(e); } });
        if (error.get()!=null) throw new AssertionError(error.get());
        return result.get();
    }
    private void waitFor(BooleanSupplier predicate) {
        long deadline=SystemClock.uptimeMillis()+18000;
        while (SystemClock.uptimeMillis()<deadline) {
            if (main(predicate::getAsBoolean)) return;
            SystemClock.sleep(100);
        }
        throw new AssertionError("Expected viewer state not reached");
    }
    private Activity launch() {
        return startActivitySync(new Intent().setClassName(getTargetContext().getPackageName(),"dev.remontevoice.android.MainActivity")
                .putExtra("reader_wait_probe",readerWaitProbe)
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK|Intent.FLAG_ACTIVITY_CLEAR_TASK));
    }
    private boolean connected(Activity activity) {
        Object service=field(activity,"service"); return service!=null && ((BridgeService)service).connected;
    }
    private void click(Activity activity, String control) { main(() -> { ((Button)field(activity,control)).performClick(); return null; }); }
    private ZoomVideoView video(Activity activity) { return (ZoomVideoView)field(activity,"videoView"); }

    @Override public void onStart() {
        if (readerWaitControls) {
            Bundle result=new Bundle();
            try {
                ReaderWaitControlChecks.run(runId);
                result.putString("stream","PASS reader wait controls");
                android.util.Log.i("RemonteVoiceVideoMain","run="+runId+" PASS reader wait controls");
                finish(Activity.RESULT_OK,result);
            } catch (Throwable failure) {
                result.putString("stream","FAIL reader wait controls "+failure.getClass().getSimpleName());
                finish(Activity.RESULT_CANCELED,result);
            }
            return;
        }
        if (playbackRecoveryStopReplay) { runRecoveryStopReplay();return; }
        if (playbackStopReplay) { runPlaybackStopReplay();return; }
        if (playbackReplay) { runPlaybackReplay();return; }
        Activity activity=null;
        Throwable failure=null;
        try {
            android.util.Log.i("RemonteVoiceVideoMain","run="+runId+" request=foreground_initial");
            activity=launch(); final Activity owner=activity;
            waitFor(() -> field(owner,"service")!=null);
            if (!main(() -> connected(owner))) click(owner,"connect");
            waitFor(() -> connected(owner));
            require(main(() -> !((BridgeService)field(owner,"service")).microphone),"Phone microphone must stay off");
            if (primingCancel) { runPrimingCancellation(owner); }
            else if (transitions) { runTransitions(owner); }
            else if (audioOnly) { runAudioOnly(owner); } else {
                click(owner,"screenButton");
                waitFor(() -> video(owner)!=null && Boolean.TRUE.equals(field(owner,"videoFrameReady")) && video(owner).displayedFrames>5);
                Surface original=main(() -> video(owner).outputSurface());
                long before=main(() -> video(owner).displayedFrames), started=SystemClock.uptimeMillis();
                SystemClock.sleep(15000);
                require(main(() -> connected(owner)),"Audio disconnected during inline video");
                click(owner,"fullscreenButton");
                waitFor(() -> field(owner,"fullscreenDialog")!=null && video(owner).getHeight()>0);
                main(() -> {
                    Dialog dialog=(Dialog)field(owner,"fullscreenDialog");
                    require((dialog.getWindow().getAttributes().flags&WindowManager.LayoutParams.FLAG_SECURE)!=0,"Fullscreen must be secure");
                    video(owner).viewport.zoomBy(2.5f,video(owner).getWidth()/2f,video(owner).getHeight()/2f);
                    require(video(owner).outputSurface()==original,"Fullscreen changed decoder surface");
                    owner.setRequestedOrientation(ActivityInfo.SCREEN_ORIENTATION_LANDSCAPE); return null;
                });
                waitFor(() -> video(owner).getWidth()>video(owner).getHeight());
                SystemClock.sleep(15000);
                main(() -> {
                    require(video(owner).viewport.zoom()==2.5f,"New frames or rotation lost zoom");
                    ((Dialog)field(owner,"fullscreenDialog")).onBackPressed(); return null;
                });
                waitFor(() -> field(owner,"fullscreenDialog")==null);
                SystemClock.sleep(10000);
                long after=main(() -> video(owner).displayedFrames), elapsed=SystemClock.uptimeMillis()-started;
                require(after-before>400,"Video remained too slow for motion");
                android.util.Log.i("RemonteVoiceVideoMain","frames="+(after-before)+" interval_ms="+elapsed+" mode=h264 surface_preserved=true");
                ZoomVideoView retired=main(() -> video(owner));
                main(() -> { owner.moveTaskToBack(true); return null; });
                android.util.Log.i("RemonteVoiceVideoMain","run="+runId+" stage=wait_hidden_cleanup");
                waitFor(() -> field(owner,"videoView")==null && field(owner,"fullscreenDialog")==null && retired.isReleased());
                // startActivitySync waits for a NEW Activity instance; after Home
                // reuse the existing instance asynchronously instead of waiting
                // forever for an onCreate that must not occur.
                // Opening the app after Home is a user action, simulated by the
                // external ADB controller. MIUI can reject a background self-launch.
                android.util.Log.i("RemonteVoiceVideoMain","run="+runId+" request=foreground_return");
                Activity returned=owner;
                waitFor(() -> Boolean.TRUE.equals(field(returned,"visible")));
                waitFor(() -> connected(returned));
                require(main(() -> !Boolean.TRUE.equals(field(returned,"previewRequested"))),"Home must not reopen preview");
                require(main(() -> !((BridgeService)field(returned,"service")).microphone),"Microphone changed unexpectedly");
                click(returned,"screenButton");
                waitFor(() -> video(returned)!=null && video(returned).displayedFrames>5);
                click(returned,"screenButton");
                waitFor(() -> field(returned,"videoView")==null);
                click(returned,"disconnect");
                waitFor(() -> !connected(returned));
            }
        } catch (Throwable error) { failure=error; }
        finally {
            if (activity!=null) {
                Activity owner=activity;
                main(() -> { owner.setRequestedOrientation(ActivityInfo.SCREEN_ORIENTATION_UNSPECIFIED); return null; });
            }
            getTargetContext().startService(new Intent(getTargetContext(),BridgeService.class).setAction(BridgeService.DISCONNECT));
        }
        Bundle result=new Bundle();
        result.putString("stream",failure==null?(primingCancel?"PASS priming cancellation cycles=6":transitions?"PASS transition diagnostic cycles=6":audioOnly?"PASS audio-only background comparison":"PASS H264 MainActivity inline/fullscreen/zoom/rotation/Back/Home/reopen/disconnect; microphone stayed off; pixels not saved")
                :"FAIL "+failure);
        android.util.Log.i("RemonteVoiceVideoMain",result.getString("stream"));
        finish(failure==null?Activity.RESULT_OK:Activity.RESULT_CANCELED,result);
    }
    private void runRecoveryStopReplay() {
        PhoneAudioEngine engine=null;Thread writer=null;Throwable failure=null;
        try {
            android.util.Log.i("RemonteVoiceVideoMain","run="+runId+" request=foreground_initial");
            Activity owner=launch();waitFor(()->field(owner,"service")!=null);
            require(main(()->!connected(owner) && !((BridgeService)field(owner,"service")).microphone),
                    "Recovery stop requires disconnected microphone-off session");
            engine=new PhoneAudioEngine(getTargetContext(),message->{});engine.startPlayback();
            Object playback=field(engine,"playback");writer=(Thread)field(playback,"thread");
            android.media.AudioTrack track=(android.media.AudioTrack)field(playback,"track");
            float[] packet=new float[960];
            for(int i=0;i<120;i++) { engine.offer(packet);SystemClock.sleep(10); }
            SystemClock.sleep(650);engine.offer(packet);
            long deadline=SystemClock.uptimeMillis()+1500;
            while(((Number)field(playback,"rebufferCount")).longValue()==0 && SystemClock.uptimeMillis()<deadline) SystemClock.sleep(1);
            require(((Number)field(playback,"rebufferCount")).longValue()>0,"Recovery stop did not exercise underrun");
            long before=((Number)field(playback,"writtenSamples")).longValue();
            int frames=((Number)field(playback,"bufferFrames")).intValue();
            for(int i=0;i<36;i++) engine.offer(packet);
            boolean pending=false;deadline=SystemClock.uptimeMillis()+1500;
            while(SystemClock.uptimeMillis()<deadline) {
                pending=writer.getState()==Thread.State.WAITING && track.getPlayState()!=android.media.AudioTrack.PLAYSTATE_PLAYING
                    && ((Number)field(playback,"writtenSamples")).longValue()>=before+frames*2;
                if(pending) break;SystemClock.sleep(1);
            }
            require(pending,"Recovery did not wait for app reserve after filling native buffer");
            long started=System.nanoTime();engine.stop();
            long stopMs=java.util.concurrent.TimeUnit.NANOSECONDS.toMillis(System.nanoTime()-started);
            writer.join(2000);
            require(!writer.isAlive(),"Recovery reserve writer survived stop");
            require(((java.util.concurrent.BlockingQueue<?>)field(playback,"queue")).isEmpty(),"Recovery stop retained PCM");
            require(stopMs<100,"Recovery reserve blocked lifecycle control");
            android.util.Log.i("RemonteVoiceVideoMain","run="+runId+" recovery_stop stop_ms="+stopMs+" queue_empty=true worker_stopped=true");
        } catch(Throwable error) { failure=error; }
        finally {
            if(engine!=null) engine.stop();
            if(writer!=null) {
                try { writer.join(2000); } catch(InterruptedException error) { Thread.currentThread().interrupt();failure=error; }
                if(writer.isAlive()) failure=new AssertionError("Recovery writer remained alive");
            }
        }
        Bundle result=new Bundle();result.putString("stream",failure==null?"PASS recovery gate cancellation":"FAIL "+failure);
        android.util.Log.i("RemonteVoiceVideoMain",result.getString("stream"));
        finish(failure==null?Activity.RESULT_OK:Activity.RESULT_CANCELED,result);
    }

    private void runPlaybackStopReplay() {
        PhoneAudioEngine engine=null;
        Thread writer=null,producer=null;
        Throwable failure=null;
        try {
            android.util.Log.i("RemonteVoiceVideoMain","run="+runId+" request=foreground_initial");
            Activity owner=launch();
            waitFor(()->field(owner,"service")!=null);
            require(main(()->!connected(owner) && !((BridgeService)field(owner,"service")).microphone),
                    "Stop replay requires disconnected microphone-off session");
            engine=new PhoneAudioEngine(getTargetContext(),message->{});
            engine.startPlayback();
            Object playback=field(engine,"playback");
            writer=(Thread)field(playback,"thread");
            android.media.AudioTrack track=(android.media.AudioTrack)field(playback,"track");
            float[] packet=new float[960];
            for(int i=0;i<80;i++) engine.offer(packet);
            track.pause();
            java.util.concurrent.BlockingQueue<?> queue=(java.util.concurrent.BlockingQueue<?>)field(playback,"queue");
            for(int i=0;i<80 && queue.remainingCapacity()>0;i++) engine.offer(packet);
            require(queue.remainingCapacity()==0,"Stop replay must fill queue while native output is paused");
            PhoneAudioEngine target=engine;
            producer=new Thread(()->target.offer(packet),"RemonteVoice-test-offer");
            producer.start();
            long waitUntil=SystemClock.uptimeMillis()+50;
            while(producer.getState()!=Thread.State.TIMED_WAITING && SystemClock.uptimeMillis()<waitUntil) SystemClock.sleep(1);
            require(producer.getState()==Thread.State.TIMED_WAITING,"Producer did not reach bounded queue wait");
            long before=System.nanoTime();
            engine.stop();
            long stopMs=java.util.concurrent.TimeUnit.NANOSECONDS.toMillis(System.nanoTime()-before);
            producer.join(1000);writer.join(2000);
            require(!producer.isAlive() && !writer.isAlive(),"Cancelled handoff or native writer remained alive");
            require(queue.isEmpty(),"Retired playback retained a late packet after stop");
            require(stopMs<100,"Queue wait blocked the engine stop lock");
            android.util.Log.i("RemonteVoiceVideoMain","run="+runId+" stop_replay stop_ms="+stopMs+" queue_empty=true workers_stopped=true");
        } catch(Throwable error) { failure=error; }
        finally {
            if(engine!=null) engine.stop();
            for(Thread thread:new Thread[]{producer,writer}) if(thread!=null) {
                try { thread.join(2000); } catch(InterruptedException error) { Thread.currentThread().interrupt();failure=error; }
                if(thread.isAlive()) failure=new AssertionError("Stop replay worker remained alive");
            }
        }
        Bundle result=new Bundle();
        result.putString("stream",failure==null?"PASS stopped pending playback handoff":"FAIL "+failure);
        android.util.Log.i("RemonteVoiceVideoMain",result.getString("stream"));
        finish(failure==null?Activity.RESULT_OK:Activity.RESULT_CANCELED,result);
    }

    private void runPlaybackReplay() {
        PhoneAudioEngine engine=null;
        Thread writer=null;
        Throwable failure=null;
        try {
            android.util.Log.i("RemonteVoiceVideoMain","run="+runId+" request=foreground_initial");
            Activity owner=launch();
            waitFor(()->field(owner,"service")!=null);
            require(main(()->!connected(owner) && !((BridgeService)field(owner,"service")).microphone),
                    "Replay requires a disconnected microphone-off session");
            AtomicReference<String> audioError=new AtomicReference<>();
            engine=new PhoneAudioEngine(getTargetContext(),audioError::set);
            engine.startPlayback();
            Object playback=field(engine,"playback");
            writer=(Thread)field(playback,"thread");
            android.media.AudioTrack track=(android.media.AudioTrack)field(playback,"track");
            float[] packet=new float[960];
            for(int sample=0;sample<480;sample++) {
                packet[sample*2]=(float)(.01*Math.sin(2*Math.PI*400*sample/48000));
                packet[sample*2+1]=(float)(.01*Math.sin(2*Math.PI*600*sample/48000));
            }
            long started=System.nanoTime();
            int gaps=0;
            int recoveryUnderruns=-1;
            long recoveryRebuffers=-1;
            for(int frame=0;frame<1200;frame++) {
                // Replay the measured startup catch-up (fast callbacks) as a
                // bounded fixture, separate from a receive outage.
                long catchUp=playbackBurst?Math.max(0,Math.min(48,frame-70))*8_000_000L:0;
                long delay=started+frame*10_000_000L-catchUp-System.nanoTime();
                if(delay>0) java.util.concurrent.TimeUnit.NANOSECONDS.sleep(delay);
                if(!playbackBurst && (frame==200 || frame==700)) {
                    long before=System.nanoTime();
                    SystemClock.sleep(replayGapMillis);
                    long gapMs=java.util.concurrent.TimeUnit.NANOSECONDS.toMillis(System.nanoTime()-before);
                    require(gapMs>=replayGapMillis && gapMs<replayGapMillis+100,"Replay gap outside measured test range");
                    android.util.Log.i("RemonteVoiceVideoMain","run="+runId+" replay_gap_ms="+gapMs+" frame="+frame);
                    gaps++;
                }
                // Immutable fixture reused deliberately; no PCM is recorded or saved.
                engine.offer(packet);
                if(frame%10==0) engine.assertProgress();
                require(audioError.get()==null,"Replay audio device failed");
                if(playbackAdaptiveReplay && frame==600) {
                    recoveryUnderruns=track.getUnderrunCount();
                    recoveryRebuffers=((Number)field(playback,"rebufferCount")).longValue();
                    android.util.Log.i("RemonteVoiceVideoMain","run="+runId+" recovery_baseline underruns="
                            +recoveryUnderruns+" rebuffer="+recoveryRebuffers);
                }
            }
            int underruns=track.getUnderrunCount();
            long dropped=((Number)field(playback,"droppedFrames")).longValue();
            long rebuffer=((Number)field(playback,"rebufferCount")).longValue();
            long played=((Number)field(playback,"playedFrames")).longValue();
            long handoffs=((Number)field(playback,"backpressureWaits")).longValue();
            android.util.Log.i("RemonteVoiceVideoMain","run="+runId+" replay_result gaps="+gaps
                    +" packets=1200 underruns="+underruns+" dropped="+dropped+" rebuffer="+rebuffer+" played_frames="+played
                    +" backpressure_waits="+handoffs);
            if(playbackBurst) require(handoffs>0,"Burst did not exercise bounded handoff");
            if(playbackAdaptiveReplay) {
                require(recoveryUnderruns>0 && recoveryRebuffers>0,"First long gap did not exercise recovery");
                require(underruns==recoveryUnderruns && rebuffer==recoveryRebuffers && dropped==0 && played>48000,
                        "Repeated receive gap exhausted the recovered reserve");
            } else {
                require(gaps==(playbackBurst?0:2) && underruns==0 && dropped==0 && rebuffer==0 && played>48000,
                        "Controlled replay did not preserve uninterrupted playback");
            }
        } catch(Throwable error) { failure=error; }
        finally {
            if(engine!=null) engine.stop();
            if(writer!=null) {
                try { writer.join(2000); }
                catch(InterruptedException error) { Thread.currentThread().interrupt();failure=error; }
                if(writer.isAlive()) failure=new AssertionError("Replay writer did not stop");
            }
        }
        Bundle result=new Bundle();
        result.putString("stream",failure==null?(playbackAdaptiveReplay?"PASS adaptive playback recovery gaps=2 packets=1200"
                :"PASS controlled playback replay gaps="+(playbackBurst?0:2)+" packets=1200"):"FAIL "+failure);
        android.util.Log.i("RemonteVoiceVideoMain",result.getString("stream"));
        finish(failure==null?Activity.RESULT_OK:Activity.RESULT_CANCELED,result);
    }

    private void runPrimingCancellation(Activity owner) throws InterruptedException {
        int waitingObserved=0;
        for (int cycle=0;cycle<6;cycle++) {
            if (cycle>0) { click(owner,"connect");waitFor(()->connected(owner)); }
            BridgeService session=main(()->(BridgeService)field(owner,"service"));
            require(main(()->!session.microphone),"Cancellation enabled microphone");
            Object engine=main(()->field(session,"audio"));
            Object playback=field(engine,"playback");
            require(playback!=null,"Playback unavailable before cancellation");
            Thread writer=(Thread)field(playback,"thread");
            int queued=((java.util.concurrent.BlockingQueue<?>)field(playback,"queue")).size();
            PlaybackBufferPolicy buffering=(PlaybackBufferPolicy)field(playback,"buffering");
            int target=buffering.primingPackets(480,((Number)field(playback,"bufferFrames")).intValue());
            boolean wasWaiting=writer.getState()==Thread.State.WAITING && queued<target;
            if (wasWaiting) waitingObserved++;
            click(owner,"disconnect");
            writer.join(2000);
            require(!writer.isAlive(),"Playback writer survived cancelled priming");
            waitFor(()->!connected(owner) && field(session,"audio")==null);
            android.util.Log.i("RemonteVoiceVideoMain","run="+runId+" phase=priming_cancel_"+cycle
                    +" waiting_observed="+wasWaiting+" queued="+queued+" target="+target+" writer_exited=true microphone=false");
        }
        require(waitingObserved>0,"No pending priming observed; cancellation check inconclusive");
    }

    private void runAudioOnly(Activity owner) {
        BridgeService session=main(() -> (BridgeService)field(owner,"service"));
        for(int cycle=0;cycle<2;cycle++) {
            SystemClock.sleep(5000);
            audioStats(session,"active_"+cycle);
            main(() -> { owner.moveTaskToBack(true); return null; });
            SystemClock.sleep(backgroundMillis);
            audioStats(session,"background_"+cycle);
            android.util.Log.i("RemonteVoiceVideoMain","run="+runId+" request=foreground_audio_"+cycle);
            waitFor(() -> Boolean.TRUE.equals(field(owner,"visible")) && connected(owner));
            SystemClock.sleep(4000);
            audioStats(session,"returned_"+cycle);
            require(main(() -> !session.microphone),"Microphone must remain off");
        }
        click(owner,"disconnect");
        waitFor(() -> !connected(owner));
        if (readerWaitProbe) {
            ReaderWaitSite observer=main(() -> session.lastReaderWaitSite);
            require(observer!=null,"Reader wait observer was not enabled");
            require(observer.awaitStopped(1500),"Reader wait observer cleanup still pending");
            android.util.Log.i("RemonteVoiceSession",observer.maximum());
        }
    }
    private void runTransitions(Activity owner) {
        BridgeService session=main(() -> (BridgeService)field(owner,"service"));
        for(int cycle=0;cycle<6;cycle++) {
            boolean withVideo=(cycle%2)==1;
            final int index=cycle;
            if(withVideo) {
                click(owner,"screenButton");
                waitFor(() -> video(owner)!=null && video(owner).displayedFrames>10);
                click(owner,"fullscreenButton");
                waitFor(() -> field(owner,"fullscreenDialog")!=null);
            }
            SystemClock.sleep(3000);
            audioStats(session,"before_"+cycle+"_video="+withVideo);
            ZoomVideoView retired=main(() -> video(owner));
            main(() -> { owner.moveTaskToBack(true); return null; });
            waitFor(() -> !Boolean.TRUE.equals(field(owner,"visible")));
            if(withVideo) waitFor(() -> retired.isReleased());
            SystemClock.sleep(6000);
            audioStats(session,"background_"+cycle);
            android.util.Log.i("RemonteVoiceVideoMain","run="+runId+" request=foreground_cycle_"+index);
            waitFor(() -> Boolean.TRUE.equals(field(owner,"visible")) && connected(owner));
            require(main(() -> !Boolean.TRUE.equals(field(owner,"previewRequested"))),"Returning reopened preview");
            SystemClock.sleep(3000);
            audioStats(session,"returned_"+cycle);
            require(main(() -> !session.microphone),"Microphone must remain off");
        }
        click(owner,"disconnect");
        waitFor(() -> !connected(owner));
    }
    private void audioStats(BridgeService session,String phase) {
        main(() -> {
            Object engine=field(session,"audio");
            require(engine!=null,"Audio engine stopped in "+phase);
            Object playback=field(engine,"playback");
            require(playback!=null,"Playback stopped in "+phase);
            if(strictAudio) {
                require(((Number)field(playback,"observedUnderruns")).intValue()==0,"Playback underrun in "+phase);
                require(((Number)field(playback,"droppedFrames")).longValue()==0,"Playback queue dropped frames in "+phase);
                require(((Number)field(playback,"rebufferCount")).intValue()==0,"Playback rebuffered in "+phase);
            }
            android.util.Log.i("RemonteVoiceVideoMain","run="+runId+" phase="+phase
                    +" clock_ns="+System.nanoTime()
                    +" underruns="+field(playback,"observedUnderruns")
                    +" rebuffer_count="+field(playback,"rebufferCount")
                    +" rebuffer_ns="+field(playback,"rebufferNanos")
                    +" max_gap_ns="+field(playback,"maxReceiveGap"));
            return null;
        });
    }

}
