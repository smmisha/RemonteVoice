package dev.remontevoice.android;

import static org.junit.Assert.*;
import java.io.ByteArrayOutputStream;
import java.util.Arrays;
import java.util.zip.Deflater;
import org.junit.Test;

public final class LosslessAudioProtocolTest {
    static byte[] golden() {
        // Python raw-DEFLATE + byte lanes for [0,-0,1.25,1e-40] repeated 240 times.
        String hex="0363606038348a47c360340d0cd93420c6c0c0308a47c360340d0cc934b080918161148f86c1681a189269a0c19e8161148f86c1681a1892690000";
        byte[] bytes=new byte[hex.length()/2];
        for(int i=0;i<bytes.length;i++)bytes[i]=(byte)Integer.parseInt(hex.substring(i*2,i*2+2),16);
        return bytes;
    }

    @Test public void pythonGoldenPreservesEveryFloatBit() {
        float[] values=AudioProtocol.decodeSystem(golden());
        int[] expected={0,0x80000000,0x3fa00000,Float.floatToRawIntBits(1e-40f)};
        assertEquals(960,values.length);
        for(int i=0;i<values.length;i++)assertEquals(expected[i%4],Float.floatToRawIntBits(values[i]));
    }

    static byte[] packed(byte[] lanes) {
        Deflater deflater=new Deflater(1,true);
        try {
            deflater.setInput(lanes);deflater.finish();
            ByteArrayOutputStream out=new ByteArrayOutputStream();out.write(3);byte[] chunk=new byte[256];
            while(!deflater.finished()){int count=deflater.deflate(chunk);out.write(chunk,0,count);}
            return out.toByteArray();
        } finally {deflater.end();}
    }

    @Test public void rejectsTruncationTrailingStreamsAndWrongExpandedLengths() {
        byte[] valid=golden();
        byte[] trailing=Arrays.copyOf(valid,valid.length+1);
        for(byte[] packet:new byte[][]{Arrays.copyOf(valid,valid.length-1),trailing,
                packed(new byte[3839]),packed(new byte[3841]),packed(new byte[100000]),new byte[]{3,1,2,3}}) {
            assertThrows(IllegalArgumentException.class,()->AudioProtocol.decodeSystem(packet));
        }
    }

    @Test public void rejectsNonfiniteSamplesAfterInflation() {
        byte[] lanes=new byte[3840];lanes[2*960]=(byte)0x80;lanes[3*960]=(byte)0x7f;
        assertThrows(IllegalArgumentException.class,()->AudioProtocol.decodeSystem(packed(lanes)));
    }
}
