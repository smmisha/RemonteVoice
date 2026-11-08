package dev.remotevoice.android;

import android.content.Context;
import android.security.keystore.KeyGenParameterSpec;
import android.security.keystore.KeyProperties;
import android.util.Base64;

import java.nio.charset.StandardCharsets;
import java.security.GeneralSecurityException;
import java.security.KeyStore;
import java.util.Arrays;

import javax.crypto.Cipher;
import javax.crypto.KeyGenerator;
import javax.crypto.SecretKey;
import javax.crypto.spec.GCMParameterSpec;

/** Pairing token encrypted with a non-exportable Android Keystore AES key; backup disabled. */
final class CredentialStore {
    private static final String ALIAS = "dev.remotevoice.android.pairing.v1";
    private final Context context;
    CredentialStore(Context context) { this.context = context.getApplicationContext(); }

    String load(String fingerprint) throws GeneralSecurityException {
        String value = context.getSharedPreferences("pairing", Context.MODE_PRIVATE).getString(fingerprint, null);
        if (value == null) return null;
        try {
            byte[] combined = Base64.decode(value, Base64.NO_WRAP);
            if (combined.length < 29 || combined.length > 1024) throw new GeneralSecurityException("Invalid stored token");
            Cipher cipher = Cipher.getInstance("AES/GCM/NoPadding");
            cipher.init(Cipher.DECRYPT_MODE, key(), new GCMParameterSpec(128, Arrays.copyOf(combined, 12)));
            cipher.updateAAD(fingerprint.getBytes(StandardCharsets.US_ASCII));
            byte[] plaintext = cipher.doFinal(combined, 12, combined.length - 12);
            try { return new String(plaintext, StandardCharsets.UTF_8); }
            finally { Arrays.fill(plaintext, (byte) 0); }
        } catch (IllegalArgumentException exception) { throw new GeneralSecurityException("Invalid stored token", exception); }
    }

    void save(String fingerprint, String token) throws GeneralSecurityException {
        Cipher cipher = Cipher.getInstance("AES/GCM/NoPadding");
        cipher.init(Cipher.ENCRYPT_MODE, key());
        cipher.updateAAD(fingerprint.getBytes(StandardCharsets.US_ASCII));
        byte[] plaintext = token.getBytes(StandardCharsets.UTF_8);
        byte[] encrypted;
        try { encrypted = cipher.doFinal(plaintext); }
        finally { Arrays.fill(plaintext, (byte) 0); }
        byte[] iv = cipher.getIV();
        if (iv.length != 12) throw new GeneralSecurityException("Unsupported nonce");
        byte[] combined = new byte[iv.length + encrypted.length];
        System.arraycopy(iv, 0, combined, 0, iv.length);
        System.arraycopy(encrypted, 0, combined, iv.length, encrypted.length);
        if (!context.getSharedPreferences("pairing", Context.MODE_PRIVATE).edit()
                .putString(fingerprint, Base64.encodeToString(combined, Base64.NO_WRAP)).commit()) {
            throw new GeneralSecurityException("Cannot persist pairing token");
        }
    }

    private SecretKey key() throws GeneralSecurityException {
        try {
            KeyStore store = KeyStore.getInstance("AndroidKeyStore");
            store.load(null);
            if (store.containsAlias(ALIAS)) return (SecretKey) store.getKey(ALIAS, null);
            KeyGenerator generator = KeyGenerator.getInstance(KeyProperties.KEY_ALGORITHM_AES, "AndroidKeyStore");
            generator.init(new KeyGenParameterSpec.Builder(ALIAS, KeyProperties.PURPOSE_ENCRYPT | KeyProperties.PURPOSE_DECRYPT)
                    .setBlockModes(KeyProperties.BLOCK_MODE_GCM).setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE).build());
            return generator.generateKey();
        } catch (java.io.IOException exception) { throw new GeneralSecurityException("Keystore unavailable", exception); }
    }
}
