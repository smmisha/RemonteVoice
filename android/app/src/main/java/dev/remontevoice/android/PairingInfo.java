package dev.remontevoice.android;

import java.net.URI;
import java.net.URISyntaxException;
import java.net.URLDecoder;
import java.nio.charset.StandardCharsets;
import java.util.HashMap;
import java.util.Locale;
import java.util.Map;

/** The bounded local-network identity carried by a RemonteVoice pairing link. */
public final class PairingInfo {
    public final String host;
    public final int port;
    public final String fingerprint;
    public final String pin;

    private PairingInfo(String host, int port, String fingerprint, String pin) {
        this.host = host;
        this.port = port;
        this.fingerprint = fingerprint;
        this.pin = pin;
    }

    public static PairingInfo parse(String link) {
        if (link == null || link.length() > 2048 || !link.equals(link.trim())) {
            throw invalid();
        }
        final URI uri;
        try {
            uri = new URI(link);
        } catch (URISyntaxException ex) {
            throw invalid();
        }
        if (!"remontevoice".equalsIgnoreCase(uri.getScheme())
                || !"pair".equalsIgnoreCase(uri.getHost())
                || uri.getUserInfo() != null || uri.getPort() != -1
                || uri.getFragment() != null || !"".equals(uri.getRawPath())
                || uri.getRawQuery() == null) {
            throw invalid();
        }
        Map<String, String> values = new HashMap<>();
        for (String entry : uri.getRawQuery().split("&", -1)) {
            int separator = entry.indexOf('=');
            if (separator <= 0 || separator != entry.lastIndexOf('=')) {
                throw invalid();
            }
            final String key;
            final String value;
            try {
                key = URLDecoder.decode(entry.substring(0, separator), StandardCharsets.UTF_8.name());
                value = URLDecoder.decode(entry.substring(separator + 1), StandardCharsets.UTF_8.name());
            } catch (java.io.UnsupportedEncodingException | IllegalArgumentException ex) {
                throw invalid();
            }
            if (!(key.equals("host") || key.equals("port")
                    || key.equals("fingerprint") || key.equals("pin"))
                    || values.put(key, value) != null) {
                throw invalid();
            }
        }
        String host = values.get("host");
        String portText = values.get("port");
        String fingerprint = values.get("fingerprint");
        String pin = values.get("pin");
        if (values.size() != 4 || !isPrivateIPv4(host)
                || portText == null || !portText.matches("[0-9]{1,5}")
                || fingerprint == null || !fingerprint.matches("[0-9a-fA-F]{64}")
                || pin == null || !pin.matches("[0-9]{8}")) {
            throw invalid();
        }
        int port = Integer.parseInt(portText);
        if (port < 1 || port > 65535) {
            throw invalid();
        }
        return new PairingInfo(host, port, fingerprint.toUpperCase(Locale.ROOT), pin);
    }

    public String webSocketUrl() {
        return "wss://" + host + ":" + port + "/";
    }

    private static boolean isPrivateIPv4(String host) {
        if (host == null) {
            return false;
        }
        String[] parts = host.split("\\.", -1);
        if (parts.length != 4) {
            return false;
        }
        int[] octets = new int[4];
        for (int i = 0; i < parts.length; i++) {
            if (!parts[i].matches("0|[1-9][0-9]{0,2}")) {
                return false;
            }
            octets[i] = Integer.parseInt(parts[i]);
            if (octets[i] > 255) {
                return false;
            }
        }
        return octets[0] == 10
                || (octets[0] == 172 && octets[1] >= 16 && octets[1] <= 31)
                || (octets[0] == 192 && octets[1] == 168);
    }

    private static IllegalArgumentException invalid() {
        return new IllegalArgumentException("Некорректная ссылка сопряжения RemonteVoice.");
    }
}
