package br.com.videodownloader.clipboardsync;

import android.content.ClipData;
import android.content.ClipboardManager;
import android.content.Context;
import android.net.DhcpInfo;
import android.os.Bundle;
import android.view.View;
import android.widget.Button;
import android.widget.LinearLayout;
import android.widget.TextView;
import android.net.wifi.WifiManager;

import org.json.JSONObject;

import java.io.BufferedReader;
import java.io.IOException;
import java.io.InputStream;
import java.io.InputStreamReader;
import java.io.OutputStream;
import java.net.DatagramPacket;
import java.net.DatagramSocket;
import java.net.HttpURLConnection;
import java.net.InetAddress;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

public class MainActivity extends android.app.Activity {
    private static final int DISCOVERY_PORT = 8766;
    private static final String DISCOVERY_REQUEST = "CLIPSYNC_DISCOVER_V1";
    private static final String PREFS = "clipboard_sync";

    private final ExecutorService executor = Executors.newSingleThreadExecutor();
    private TextView status;
    private Button captureButton;
    private Button sendButton;
    private Endpoint endpoint;
    private WifiManager.MulticastLock multicastLock;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        setContentView(createContentView());
        acquireMulticastLock();
        discoverInBackground();
    }

    private void acquireMulticastLock() {
        WifiManager wifi = (WifiManager) getApplicationContext()
                .getSystemService(Context.WIFI_SERVICE);
        if (wifi == null) {
            return;
        }
        multicastLock = wifi.createMulticastLock("ClipboardSyncDiscovery");
        multicastLock.setReferenceCounted(false);
        multicastLock.acquire();
    }

    private View createContentView() {
        LinearLayout root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        int padding = (int) (24 * getResources().getDisplayMetrics().density);
        root.setPadding(padding, padding, padding, padding);

        TextView title = new TextView(this);
        title.setText("Clipboard Sync");
        title.setTextSize(26);
        title.setPadding(0, 0, 0, padding / 2);
        root.addView(title, new LinearLayout.LayoutParams(-1, -2));

        status = new TextView(this);
        status.setText("Procurando o PC na rede local...");
        status.setTextSize(15);
        status.setPadding(0, 0, 0, padding);
        root.addView(status, new LinearLayout.LayoutParams(-1, -2));

        captureButton = new Button(this);
        captureButton.setText("Capturar clipboard do PC");
        captureButton.setOnClickListener(v -> runAction(true));
        root.addView(captureButton, buttonParams());

        sendButton = new Button(this);
        sendButton.setText("Enviar clipboard para o PC");
        sendButton.setOnClickListener(v -> runAction(false));
        root.addView(sendButton, buttonParams());
        return root;
    }

    private LinearLayout.LayoutParams buttonParams() {
        LinearLayout.LayoutParams params = new LinearLayout.LayoutParams(-1, -2);
        params.bottomMargin = 12;
        return params;
    }

    private void setBusy(boolean busy) {
        captureButton.setEnabled(!busy);
        sendButton.setEnabled(!busy);
    }

    private void setStatus(String value) {
        runOnUiThread(() -> status.setText(value));
    }

    private void discoverInBackground() {
        executor.execute(() -> {
            try {
                Endpoint found = discover();
                endpoint = found;
                getPreferences(MODE_PRIVATE).edit()
                        .putString("host", found.host)
                        .putInt("port", found.port)
                        .putString("token", found.token)
                        .apply();
                setStatus("PC encontrado: " + found.host);
            } catch (Exception error) {
                setStatus("PC não encontrado. Verifique se o servidor está rodando.");
            }
        });
    }

    private Endpoint getEndpoint() throws IOException {
        if (endpoint != null) {
            return endpoint;
        }
        Endpoint found = discover();
        endpoint = found;
        return found;
    }

    private Endpoint discover() throws IOException {
        byte[] request = DISCOVERY_REQUEST.getBytes(StandardCharsets.UTF_8);
        try (DatagramSocket socket = new DatagramSocket()) {
            socket.setBroadcast(true);
            socket.setSoTimeout(1200);
            List<InetAddress> broadcastAddresses = getBroadcastAddresses();
            for (int attempt = 0; attempt < 3; attempt++) {
                for (InetAddress broadcastAddress : broadcastAddresses) {
                    DatagramPacket packet = new DatagramPacket(
                            request, request.length, broadcastAddress, DISCOVERY_PORT);
                    socket.send(packet);
                }
                long deadline = System.currentTimeMillis() + 1000;
                while (System.currentTimeMillis() < deadline) {
                    byte[] buffer = new byte[1024];
                    DatagramPacket response = new DatagramPacket(buffer, buffer.length);
                    try {
                        socket.receive(response);
                    } catch (java.net.SocketTimeoutException timeout) {
                        break;
                    }
                    Endpoint parsed = parseDiscovery(
                            new String(response.getData(), response.getOffset(), response.getLength(), StandardCharsets.UTF_8),
                            response.getAddress().getHostAddress());
                    if (parsed != null) {
                        return parsed;
                    }
                }
            }
        }
        throw new IOException("nenhum servidor Clipboard Sync respondeu");
    }

    private List<InetAddress> getBroadcastAddresses() throws IOException {
        List<InetAddress> addresses = new ArrayList<>();
        addresses.add(InetAddress.getByName("255.255.255.255"));

        WifiManager wifi = (WifiManager) getApplicationContext()
                .getSystemService(Context.WIFI_SERVICE);
        if (wifi == null) {
            return addresses;
        }
        DhcpInfo dhcp = wifi.getDhcpInfo();
        if (dhcp == null || dhcp.ipAddress == 0 || dhcp.netmask == 0) {
            return addresses;
        }

        // DhcpInfo usa a ordem de bytes little-endian nos inteiros IPv4.
        int broadcast = (dhcp.ipAddress & dhcp.netmask) | ~dhcp.netmask;
        InetAddress subnetBroadcast = InetAddress.getByAddress(new byte[] {
                (byte) (broadcast & 0xff),
                (byte) ((broadcast >> 8) & 0xff),
                (byte) ((broadcast >> 16) & 0xff),
                (byte) ((broadcast >> 24) & 0xff)
        });
        if (!addresses.contains(subnetBroadcast)) {
            addresses.add(subnetBroadcast);
        }
        return addresses;
    }

    private Endpoint parseDiscovery(String value, String host) {
        String[] parts = value.split("\\|", 4);
        if (parts.length != 4 || !"CLIPSYNC/1".equals(parts[0])) {
            return null;
        }
        try {
            return new Endpoint(host, Integer.parseInt(parts[1]), parts[2], parts[3]);
        } catch (NumberFormatException error) {
            return null;
        }
    }

    private void runAction(boolean captureFromPc) {
        setBusy(true);
        setStatus(captureFromPc ? "Capturando o clipboard do PC..." : "Enviando o clipboard para o PC...");
        executor.execute(() -> {
            try {
                Endpoint target = getEndpoint();
                if (captureFromPc) {
                    JSONObject response = request(target, "GET", null);
                    String text = response.optString("text", "");
                    ClipboardManager clipboard = (ClipboardManager) getSystemService(Context.CLIPBOARD_SERVICE);
                    clipboard.setPrimaryClip(ClipData.newPlainText("PC", text));
                    setStatus("Clipboard do PC capturado (" + text.length() + " caracteres).");
                } else {
                    ClipboardManager clipboard = (ClipboardManager) getSystemService(Context.CLIPBOARD_SERVICE);
                    if (!clipboard.hasPrimaryClip()) {
                        throw new IOException("o clipboard do celular está vazio");
                    }
                    CharSequence value = clipboard.getPrimaryClip().getItemAt(0).coerceToText(this);
                    String text = value == null ? "" : value.toString();
                    JSONObject payload = new JSONObject().put("text", text);
                    request(target, "POST", payload.toString());
                    setStatus("Clipboard enviado para o PC (" + text.length() + " caracteres).");
                }
            } catch (Exception error) {
                endpoint = null;
                setStatus("Erro: " + error.getMessage());
            } finally {
                runOnUiThread(() -> setBusy(false));
            }
        });
    }

    private JSONObject request(Endpoint target, String method, String body) throws Exception {
        URL url = new URL("http://" + target.host + ":" + target.port + "/v1/clipboard");
        HttpURLConnection connection = (HttpURLConnection) url.openConnection();
        connection.setRequestMethod(method);
        connection.setConnectTimeout(5000);
        connection.setReadTimeout(10000);
        connection.setRequestProperty("X-Clipboard-Token", target.token);
        connection.setRequestProperty("Accept", "application/json");
        if (body != null) {
            byte[] bytes = body.getBytes(StandardCharsets.UTF_8);
            connection.setDoOutput(true);
            connection.setRequestProperty("Content-Type", "application/json; charset=utf-8");
            connection.setFixedLengthStreamingMode(bytes.length);
            try (OutputStream output = connection.getOutputStream()) {
                output.write(bytes);
            }
        }
        int code = connection.getResponseCode();
        InputStream stream = code >= 400 ? connection.getErrorStream() : connection.getInputStream();
        String response = readAll(stream);
        connection.disconnect();
        if (code >= 400) {
            throw new IOException("PC respondeu HTTP " + code + ": " + response);
        }
        return new JSONObject(response);
    }

    private String readAll(InputStream stream) throws IOException {
        if (stream == null) {
            return "";
        }
        StringBuilder result = new StringBuilder();
        try (BufferedReader reader = new BufferedReader(new InputStreamReader(stream, StandardCharsets.UTF_8))) {
            String line;
            while ((line = reader.readLine()) != null) {
                result.append(line);
            }
        }
        return result.toString();
    }

    @Override
    protected void onDestroy() {
        if (multicastLock != null && multicastLock.isHeld()) {
            multicastLock.release();
        }
        executor.shutdownNow();
        super.onDestroy();
    }

    private static class Endpoint {
        final String host;
        final int port;
        final String token;
        final String name;

        Endpoint(String host, int port, String token, String name) {
            this.host = host;
            this.port = port;
            this.token = token;
            this.name = name;
        }
    }
}
