package dev.remontevoice.android;

import android.graphics.Bitmap;
import android.graphics.BitmapFactory;

/** In-memory decoder. Call on the preview worker, never on the audio callback. */
final class ScreenFrameDecoder {
    private ScreenFrameDecoder() {}

    static Bitmap decode(byte[] jpeg) {
        int[] size = ScreenFrameLimits.dimensions(jpeg);
        BitmapFactory.Options bounds = new BitmapFactory.Options();
        bounds.inJustDecodeBounds = true;
        BitmapFactory.decodeByteArray(jpeg, 0, jpeg.length, bounds);
        if (bounds.outWidth != size[0] || bounds.outHeight != size[1]
                || !"image/jpeg".equals(bounds.outMimeType)) throw new IllegalArgumentException();
        BitmapFactory.Options options = new BitmapFactory.Options();
        options.inPreferredConfig = Bitmap.Config.ARGB_8888;
        Bitmap bitmap = BitmapFactory.decodeByteArray(jpeg, 0, jpeg.length, options);
        if (bitmap == null) throw new IllegalArgumentException();
        if (bitmap.getWidth() != size[0] || bitmap.getHeight() != size[1]) {
            bitmap.recycle();
            throw new IllegalArgumentException();
        }
        return bitmap;
    }
}
