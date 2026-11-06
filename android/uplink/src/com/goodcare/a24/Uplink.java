package com.goodcare.a24;

import android.content.Context;
import android.content.ContextWrapper;
import android.content.AttributionSource;
import android.media.AudioAttributes;
import android.media.AudioDeviceInfo;
import android.media.AudioFormat;
import android.media.AudioManager;
import android.media.AudioTrack;
import android.os.Looper;
import android.os.Process;
import java.io.DataInputStream;
import java.io.File;
import java.io.FileInputStream;
import java.lang.reflect.InvocationTargetException;
import org.json.JSONObject;

/** USB/ADB shell-only PSTN injection. Never falls back to media playback. */
public final class Uplink {
    /** Keep the attribution UID/package aligned with the actual ADB shell. */
    static final class ShellContext extends ContextWrapper {
        private final AttributionSource attribution;
        ShellContext(Context base) {
            super(base);
            attribution=new AttributionSource.Builder(Process.myUid())
                .setPackageName("com.android.shell").build();
        }
        @Override public String getPackageName() {return "com.android.shell";}
        public String getOpPackageName() {return "com.android.shell";}
        @Override public AttributionSource getAttributionSource() {return attribution;}
        @Override public Context getApplicationContext() {return this;}
    }
    static synchronized void event(String kind, String message) {
        try {
            JSONObject value=new JSONObject();
            value.put("kind",kind);value.put("message",message);
            System.out.println(value.toString());System.out.flush();
        } catch (Exception ignored) { }
    }
    static Object invoke(Object object,String method,Class<?>[] types,Object... args) throws Exception {
        try { return object.getClass().getMethod(method,types).invoke(object,args); }
        catch (InvocationTargetException error) {
            Throwable cause=error.getCause();
            if(cause instanceof Exception)throw (Exception)cause;
            throw error;
        }
    }
    public static void main(String[] args) {
        AudioTrack track=null;
        boolean stopped=false;
        int exitCode=0;
        try {
            if(Process.myUid()!=2000)throw new SecurityException("ADB shell uid 2000 required");
            if(Looper.myLooper()==null)Looper.prepareMainLooper();
            Class<?> threadClass=Class.forName("android.app.ActivityThread");
            Object thread=threadClass.getMethod("systemMain").invoke(null);
            Context system=(Context)threadClass.getMethod("getSystemContext").invoke(thread);
            Context base=system.createPackageContext("com.android.shell",Context.CONTEXT_IGNORE_SECURITY);
            event("identity","base attribution uid="+base.getAttributionSource().getUid()
                +" package="+base.getAttributionSource().getPackageName());
            Context shell=new ShellContext(base);
            event("identity","AudioTrack attribution uid="+shell.getAttributionSource().getUid()
                +" package="+shell.getAttributionSource().getPackageName());
            if(shell.checkSelfPermission("android.permission.CALL_AUDIO_INTERCEPTION")!=0)
                throw new SecurityException("CALL_AUDIO_INTERCEPTION not granted");
            AudioManager manager=(AudioManager)shell.getSystemService(Context.AUDIO_SERVICE);
            boolean supported=(Boolean)invoke(manager,"isPstnCallAudioInterceptable",new Class<?>[0]);
            event("capability","PSTN interception support="+supported+"; mode="+manager.getMode());
            if(!supported)throw new UnsupportedOperationException("PSTN interception unavailable");
            if(args.length==1 && args[0].equals("--probe"))return;
            if(manager.getMode()!=AudioManager.MODE_IN_CALL)throw new IllegalStateException("Connect a cellular call first");
            if(args.length!=3 || !args[0].equals("--play-file"))
                throw new IllegalArgumentException("Expected --play-file <GCAR packet> <stop marker>");
            if(!args[1].matches("/data/local/tmp/goodcare-uplink-[a-f0-9]{32}[.]gcar")
                    || !args[2].equals(args[1]+".stop"))
                throw new IllegalArgumentException("Invalid session file paths");
            File packet=new File(args[1]);
            File stopFile=new File(args[2]);
            if(packet.length()<18 || packet.length()>960016)
                throw new IllegalArgumentException("Invalid packet file size "+packet.length());
            int rate,channels,length;
            byte[] pcm;
            try(DataInputStream input=new DataInputStream(new FileInputStream(packet))) {
                if(input.readInt()!=0x47434152)throw new IllegalArgumentException("Invalid GCAR input");
                rate=input.readInt();channels=input.readInt();length=input.readInt();
                if(rate!=16000 || channels!=1 || length<2 || length>960000 || length%2!=0 || packet.length()!=length+16)
                    throw new IllegalArgumentException("Expected complete PCM16 mono 16kHz packet, maximum 30 seconds");
                pcm=new byte[length];input.readFully(pcm);
            }
            event("input","USB packet file read complete: "+length+" PCM bytes");
            if(stopFile.exists()) {event("stopped","Stopped before track creation");return;}
            AudioAttributes.Builder attributes=new AudioAttributes.Builder().setContentType(AudioAttributes.CONTENT_TYPE_SPEECH);
            int usage=AudioAttributes.class.getField("USAGE_CALL_ASSISTANT").getInt(null);
            invoke(attributes,"setSystemUsage",new Class<?>[]{int.class},usage);
            AudioFormat format=new AudioFormat.Builder().setEncoding(AudioFormat.ENCODING_PCM_16BIT)
                .setSampleRate(rate).setChannelMask(AudioFormat.CHANNEL_OUT_MONO).build();
            AudioTrack.Builder builder=new AudioTrack.Builder().setAudioAttributes(attributes.build())
                .setAudioFormat(format).setTransferMode(AudioTrack.MODE_STREAM)
                .setBufferSizeInBytes(Math.max(6400,AudioTrack.getMinBufferSize(rate,AudioFormat.CHANNEL_OUT_MONO,AudioFormat.ENCODING_PCM_16BIT)));
            invoke(builder,"setContext",new Class<?>[]{Context.class},shell);
            int redirect=AudioManager.class.getField("CALL_REDIRECT_PSTN").getInt(null);
            invoke(builder,"setCallRedirectionMode",new Class<?>[]{int.class},redirect);
            event("track_config","Creating PSTN injection track: PCM16 mono 16000Hz; system usage="+usage+" redirect="+redirect);
            track=builder.build();
            if(track.getState()!=AudioTrack.STATE_INITIALIZED)throw new IllegalStateException("AudioTrack initialization failed");
            track.play();
            // Establish the route using silence before allowing any original recording.
            byte[] silence=new byte[640];
            if(track.write(silence,0,silence.length,AudioTrack.WRITE_BLOCKING)!=silence.length)
                throw new IllegalStateException("Could not prime injection track");
            AudioDeviceInfo route=null;
            long deadline=System.currentTimeMillis()+2000;
            while(System.currentTimeMillis()<deadline && !stopFile.exists()) {
                route=track.getRoutedDevice();
                if(route!=null)break;
                Thread.sleep(20);
            }
            if(route==null || route.getType()!=AudioDeviceInfo.TYPE_TELEPHONY)
                throw new IllegalStateException("Telephony TX route unconfirmed; recording NOT sent");
            event("route","Telephony TX selected; remote audibility still unverified");
            int offset=0;
            long started=System.currentTimeMillis();
            long timeout=started+length/32+7000;
            while(offset<length && !stopFile.exists()) {
                if(manager.getMode()!=AudioManager.MODE_IN_CALL)throw new IllegalStateException("Call audio mode ended");
                if(System.currentTimeMillis()>timeout)throw new IllegalStateException("Injection timed out");
                route=track.getRoutedDevice();
                if(route==null || route.getType()!=AudioDeviceInfo.TYPE_TELEPHONY)
                    throw new IllegalStateException("Telephony TX route lost");
                int count=track.write(pcm,offset,Math.min(640,length-offset),AudioTrack.WRITE_NON_BLOCKING);
                if(count<0)throw new IllegalStateException("AudioTrack write error "+count);
                offset+=count;
                if(count==0)Thread.sleep(10);
            }
            long frames=(length+silence.length)/2;
            while(!stopFile.exists() && Integer.toUnsignedLong(track.getPlaybackHeadPosition())<frames) {
                if(System.currentTimeMillis()>timeout)throw new IllegalStateException("Playback drain timed out");
                if(manager.getMode()!=AudioManager.MODE_IN_CALL)throw new IllegalStateException("Call ended during playback");
                Thread.sleep(20);
            }
            stopped=stopFile.exists();
            event(stopped?"stopped":"written",stopped?"Injection stopped":"PCM accepted and drained; ask the remote listener to verify");
        } catch(Throwable error) {
            event("error",error.getClass().getSimpleName()+": "+error.getMessage());
            exitCode=1;
        } finally {
            if(track!=null) { try{track.pause();track.flush();track.release();}catch(Exception ignored){} }
            System.exit(exitCode);
        }
    }
}
