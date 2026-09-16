package br.com.videodownloader.clipboardsync;

import android.app.AlertDialog;
import android.content.ClipData;
import android.content.ClipboardManager;
import android.content.ContentValues;
import android.content.Context;
import android.content.Intent;
import android.database.Cursor;
import android.net.DhcpInfo;
import android.net.Uri;
import android.net.wifi.WifiManager;
import android.os.Build;
import android.os.Bundle;
import android.os.Environment;
import android.os.Handler;
import android.os.Looper;
import android.provider.DocumentsContract;
import android.provider.MediaStore;
import android.provider.OpenableColumns;
import android.util.Base64;
import android.view.View;
import android.webkit.MimeTypeMap;
import android.widget.Button;
import android.widget.LinearLayout;
import android.widget.TextView;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.BufferedInputStream;
import java.io.BufferedOutputStream;
import java.io.BufferedReader;
import java.io.ByteArrayOutputStream;
import java.io.File;
import java.io.FileInputStream;
import java.io.FileOutputStream;
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
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.text.SimpleDateFormat;
import java.util.ArrayList;
import java.util.Collections;
import java.util.Date;
import java.util.HashSet;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.Set;
import java.util.TreeSet;
import java.util.UUID;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.zip.ZipEntry;
import java.util.zip.ZipInputStream;
import java.util.zip.ZipOutputStream;

public class MainActivity extends android.app.Activity {
    private static final int DISCOVERY_PORT = 8766;
    private static final String DISCOVERY_REQUEST = "CLIPSYNC_DISCOVER_V1";
    private static final String PREFS = "clipboard_sync";
    private static final String API_CLIPBOARD = "/v1/clipboard";
    private static final String API_FILE_OFFERS = "/v2/offers";
    private static final int REQUEST_UPLOAD_FILE = 4101;
    private static final int REQUEST_UPLOAD_FOLDER = 4102;
    private static final int REQUEST_SAVE_FILE = 4103;
    private static final int REQUEST_SAVE_FOLDER = 4104;
    private static final long MAX_TRANSFER_BYTES = 4L * 1024L * 1024L * 1024L;
    private static final long MAX_UNPACKED_BYTES = 4L * 1024L * 1024L * 1024L;
    private static final long MAX_TRANSFER_WAIT_MS = 5L * 60L * 1000L;
    private static final int MAX_ZIP_ENTRIES = 20000;
    private static final int BUFFER_SIZE = 64 * 1024;

    private final ExecutorService executor = Executors.newSingleThreadExecutor();
    private final ExecutorService filePollExecutor = Executors.newSingleThreadExecutor();
    private final ExecutorService fileTransferExecutor = Executors.newSingleThreadExecutor();
    private final Handler mainHandler = new Handler(Looper.getMainLooper());
    private final AtomicBoolean pollInFlight = new AtomicBoolean(false);
    private final Runnable offerPollRunnable = new Runnable() {
        @Override
        public void run() {
            if (!activityResumed || destroyed) {
                return;
            }
            refreshOffers();
            mainHandler.postDelayed(this, 5000);
        }
    };

    private TextView status;
    private TextView offersStatus;
    private TextView fileStatus;
    private Button captureButton;
    private Button sendButton;
    private Button receiveFileButton;
    private Button sendFileButton;
    private volatile Endpoint endpoint;
    private WifiManager.MulticastLock multicastLock;
    private volatile boolean activityResumed;
    private volatile boolean destroyed;
    private volatile List<FileOffer> offers = Collections.emptyList();
    private boolean fileTransferBusy;
    private boolean filePickerPending;
    private FileOffer pendingLegacyOffer;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        setContentView(createContentView());
        acquireMulticastLock();
        discoverInBackground();
        setFileStatus("As ofertas são verificadas enquanto o aplicativo está aberto.");
    }

    @Override
    protected void onResume() {
        super.onResume();
        activityResumed = true;
        mainHandler.removeCallbacks(offerPollRunnable);
        refreshOffers();
        mainHandler.postDelayed(offerPollRunnable, 5000);
    }

    @Override
    protected void onPause() {
        activityResumed = false;
        mainHandler.removeCallbacks(offerPollRunnable);
        super.onPause();
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
        status.setPadding(0, 0, 0, padding / 2);
        root.addView(status, new LinearLayout.LayoutParams(-1, -2));

        captureButton = new Button(this);
        captureButton.setText("Capturar clipboard do PC");
        captureButton.setOnClickListener(v -> runAction(true));
        root.addView(captureButton, buttonParams());

        sendButton = new Button(this);
        sendButton.setText("Enviar clipboard para o PC");
        sendButton.setOnClickListener(v -> runAction(false));
        root.addView(sendButton, buttonParams());

        offersStatus = new TextView(this);
        offersStatus.setText("Verificando ofertas de arquivo...");
        offersStatus.setTextSize(15);
        offersStatus.setPadding(0, padding / 2, 0, padding / 2);
        root.addView(offersStatus, new LinearLayout.LayoutParams(-1, -2));

        receiveFileButton = new Button(this);
        receiveFileButton.setText("Receber arquivo");
        receiveFileButton.setOnClickListener(v -> showOffers());
        root.addView(receiveFileButton, buttonParams());

        sendFileButton = new Button(this);
        sendFileButton.setText("Enviar arquivo");
        sendFileButton.setOnClickListener(v -> showUploadChoices());
        root.addView(sendFileButton, buttonParams());

        fileStatus = new TextView(this);
        fileStatus.setTextSize(14);
        fileStatus.setPadding(0, padding / 2, 0, 0);
        root.addView(fileStatus, new LinearLayout.LayoutParams(-1, -2));
        refreshFileButtons();
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

    private void setFileStatus(String value) {
        runOnUiThread(() -> {
            if (!destroyed && fileStatus != null) {
                fileStatus.setText(value);
            }
        });
    }

    private void setOffersStatus(String value) {
        runOnUiThread(() -> {
            if (!destroyed && offersStatus != null) {
                offersStatus.setText(value);
            }
        });
    }

    private void refreshFileButtons() {
        if (receiveFileButton == null || sendFileButton == null) {
            return;
        }
        boolean enabled = !fileTransferBusy && !filePickerPending;
        receiveFileButton.setEnabled(enabled && !offers.isEmpty());
        sendFileButton.setEnabled(enabled);
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

    private synchronized Endpoint getEndpoint() throws IOException {
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
        URL url = new URL("http://" + target.host + ":" + target.port + API_CLIPBOARD);
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

    private void refreshOffers() {
        if (!activityResumed || destroyed || !pollInFlight.compareAndSet(false, true)) {
            return;
        }
        filePollExecutor.execute(() -> {
            try {
                Endpoint target = getEndpoint();
                JSONObject response = fileJsonRequest(
                        target, "GET", API_FILE_OFFERS, null, deviceHeaders());
                JSONArray array = response.optJSONArray("offers");
                List<FileOffer> found = new ArrayList<>();
                if (array != null) {
                    for (int index = 0; index < array.length(); index++) {
                        JSONObject item = array.optJSONObject(index);
                        if (item == null) {
                            continue;
                        }
                        String id = item.optString("id", "");
                        String name = item.optString("name", "");
                        String kind = item.optString("kind", "").toLowerCase(Locale.ROOT);
                        if (id.isEmpty() || name.isEmpty()
                                || (!"file".equals(kind) && !"folder".equals(kind))) {
                            continue;
                        }
                        found.add(new FileOffer(
                                id,
                                name,
                                kind,
                                Math.max(0, item.optLong("size", 0)),
                                item.optString("sender_name", "PC"),
                                formatExpiry(item.optDouble("expires_in", 0))));
                    }
                }
                List<FileOffer> snapshot = Collections.unmodifiableList(found);
                runOnUiThread(() -> {
                    if (activityResumed && !destroyed) {
                        offers = snapshot;
                        renderOffers();
                    }
                });
            } catch (Exception error) {
                runOnUiThread(() -> {
                    if (activityResumed && !destroyed && offers.isEmpty() && !fileTransferBusy) {
                        offersStatus.setText("Não foi possível consultar ofertas: " + error.getMessage());
                    }
                });
            } finally {
                pollInFlight.set(false);
            }
        });
    }

    private void renderOffers() {
        if (offers.isEmpty()) {
            offersStatus.setText("Nenhuma oferta de arquivo. Busca ativa enquanto o app está aberto.");
        } else {
            FileOffer first = offers.get(0);
            String suffix = offers.size() == 1 ? "" : " (+" + (offers.size() - 1) + ")";
            offersStatus.setText("Oferta de " + first.senderName + ": " + first.name + suffix);
        }
        refreshFileButtons();
    }

    private void showOffers() {
        List<FileOffer> current = offers;
        if (current.isEmpty()) {
            setFileStatus("Não há ofertas disponíveis no momento.");
            return;
        }
        String[] labels = new String[current.size()];
        for (int index = 0; index < current.size(); index++) {
            labels[index] = offerLabel(current.get(index));
        }
        new AlertDialog.Builder(this)
                .setTitle("Ofertas de arquivo")
                .setItems(labels, (dialog, which) -> showOfferActions(current.get(which)))
                .setNegativeButton("Fechar", null)
                .show();
    }

    private String offerLabel(FileOffer offer) {
        String kind = "folder".equals(offer.kind) ? "Pasta" : "Arquivo";
        String label = kind + ": " + offer.name + " — " + offer.senderName
                + " (" + formatBytes(offer.size) + ")";
        if (!offer.expiresAt.isEmpty()) {
            label += "\nExpira: " + offer.expiresAt;
        }
        return label;
    }

    private void showOfferActions(FileOffer offer) {
        new AlertDialog.Builder(this)
                .setTitle(offer.name)
                .setMessage(offerLabel(offer))
                .setPositiveButton("Receber arquivo", (dialog, which) -> beginReceiving(offer))
                .setNeutralButton("Recusar", (dialog, which) -> declineOffer(offer))
                .setNegativeButton("Cancelar", null)
                .show();
    }

    private void beginReceiving(FileOffer offer) {
        if (fileTransferBusy || filePickerPending) {
            return;
        }
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            startReceiving(offer, null);
            return;
        }

        pendingLegacyOffer = offer;
        filePickerPending = true;
        fileTransferBusy = true;
        refreshFileButtons();
        setFileStatus("Escolha Downloads como destino para salvar " + offer.name + ".");
        try {
            Intent intent;
            if ("folder".equals(offer.kind)) {
                intent = new Intent(Intent.ACTION_OPEN_DOCUMENT_TREE);
                intent.putExtra(DocumentsContract.EXTRA_INITIAL_URI, downloadsInitialUri());
                startActivityForResult(intent, REQUEST_SAVE_FOLDER);
            } else {
                intent = new Intent(Intent.ACTION_CREATE_DOCUMENT);
                intent.addCategory(Intent.CATEGORY_OPENABLE);
                intent.setType("*/*");
                intent.putExtra(Intent.EXTRA_TITLE, safeSegment(offer.name, "arquivo"));
                intent.putExtra(DocumentsContract.EXTRA_INITIAL_URI, downloadsInitialUri());
                startActivityForResult(intent, REQUEST_SAVE_FILE);
            }
        } catch (Exception error) {
            pendingLegacyOffer = null;
            filePickerPending = false;
            fileTransferBusy = false;
            refreshFileButtons();
            setFileStatus("Não foi possível abrir o seletor de Downloads: " + error.getMessage());
        }
    }

    private Uri downloadsInitialUri() {
        return DocumentsContract.buildDocumentUri(
                "com.android.externalstorage.documents", "primary:Download");
    }

    private void declineOffer(FileOffer offer) {
        if (fileTransferBusy || filePickerPending) {
            return;
        }
        fileTransferBusy = true;
        refreshFileButtons();
        setFileStatus("Recusando oferta...");
        fileTransferExecutor.execute(() -> {
            try {
                Endpoint target = getEndpoint();
                fileJsonRequest(target, "POST", offerRoute(offer.id, "decline"),
                        identityPayload(), Collections.emptyMap());
                setFileStatus("Oferta recusada: " + offer.name);
            } catch (Exception error) {
                setFileStatus("Não foi possível recusar a oferta: " + error.getMessage());
            } finally {
                runOnUiThread(() -> {
                    fileTransferBusy = false;
                    refreshFileButtons();
                });
                refreshOffers();
            }
        });
    }

    private JSONObject awaitOfferStatus(
            Endpoint target, String offerId, String desired, String waitingMessage) throws Exception {
        long deadline = System.currentTimeMillis() + MAX_TRANSFER_WAIT_MS;
        long lastUpdate = 0;
        while (System.currentTimeMillis() < deadline) {
            if (Thread.currentThread().isInterrupted()) {
                throw new IOException("transferência cancelada");
            }
            JSONObject response = fileJsonRequest(target, "GET",
                    offerRoute(offerId, "status"), null, statusHeaders());
            String current = response.optString("status", "").toLowerCase(Locale.ROOT);
            if (desired.equals(current)) {
                return response;
            }
            if ("expired".equals(current)) {
                throw new IOException("a oferta expirou antes da transferência");
            }
            if ("cancelled".equals(current) || "failed".equals(current)) {
                throw new IOException("a transferência foi cancelada ou falhou");
            }
            if ("completed".equals(current)) {
                throw new IOException("a oferta já foi concluída");
            }
            long now = System.currentTimeMillis();
            if (now - lastUpdate >= 5000) {
                setFileStatus(waitingMessage);
                lastUpdate = now;
            }
            Thread.sleep(500);
        }
        throw new IOException("tempo esgotado aguardando a outra máquina");
    }

    private void startReceiving(FileOffer offer, Uri legacyDestination) {
        fileTransferBusy = true;
        filePickerPending = false;
        refreshFileButtons();
        setFileStatus("Reservando oferta: " + offer.name + "...");
        fileTransferExecutor.execute(() -> receiveOffer(offer, legacyDestination));
    }

    private void receiveOffer(FileOffer offer, Uri legacyDestination) {
        File temporaryFile = null;
        try {
            Endpoint target = getEndpoint();
            fileJsonRequest(target, "POST", offerRoute(offer.id, "accept"),
                    identityPayload(), Collections.emptyMap());
            JSONObject readyState = awaitOfferStatus(
                    target, offer.id, "ready", "Aguardando o envio de " + offer.name + " pelo PC...");
            setFileStatus("Baixando " + offer.name + "...");

            DownloadResult download = downloadOffer(
                    target, offer, readyState.optString("sha256", ""));
            temporaryFile = download.file;
            String kind = download.kind;
            String savedLocation;
            if ("file".equals(kind)) {
                if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
                    savedLocation = saveFileToDownloads(download.file, download.name);
                } else {
                    if (legacyDestination == null) {
                        throw new IOException("destino de Downloads não selecionado");
                    }
                    saveFileToUri(download.file, legacyDestination);
                    savedLocation = "o destino escolhido em Downloads";
                }
            } else {
                if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
                    savedLocation = extractZipToDownloads(download.file, download.name);
                } else {
                    if (legacyDestination == null) {
                        throw new IOException("destino de Downloads não selecionado");
                    }
                    savedLocation = extractZipToTree(download.file, download.name, legacyDestination);
                }
            }

            String completionNote = "";
            try {
                fileJsonRequest(target, "POST", transferRoute(offer.id, "complete"),
                        deviceIdPayload(), Collections.emptyMap());
            } catch (Exception error) {
                completionNote = " O arquivo foi salvo, mas não foi possível confirmar a conclusão ao PC: "
                        + error.getMessage();
            }
            setFileStatus("Recebido em " + savedLocation + "." + completionNote);
        } catch (HttpStatusException error) {
            if (error.statusCode == 409) {
                setFileStatus("Outra máquina já aceitou esta oferta.");
            } else if (error.statusCode == 410) {
                setFileStatus("Esta oferta expirou.");
            } else {
                setFileStatus("Falha ao receber " + offer.name + ": " + error.getMessage());
            }
        } catch (Exception error) {
            setFileStatus("Falha ao receber " + offer.name + ": " + error.getMessage());
        } finally {
            if (temporaryFile != null && temporaryFile.exists()) {
                temporaryFile.delete();
            }
            runOnUiThread(() -> {
                fileTransferBusy = false;
                refreshFileButtons();
            });
            refreshOffers();
        }
    }

    private DownloadResult downloadOffer(
            Endpoint target, FileOffer offer, String expectedSha256) throws Exception {
        String route = transferRoute(offer.id, "content");
        HttpURLConnection connection = openConnection(target, route, "GET", "application/octet-stream");
        connection.setRequestProperty("X-Clipboard-Device-Id", deviceId());
        File output = File.createTempFile("clipsync-download-", ".bin", getCacheDir());
        boolean complete = false;
        try {
            int code = connection.getResponseCode();
            if (code >= 400) {
                throw new HttpStatusException(code,
                        "PC respondeu HTTP " + code + ": " + readLimited(connection.getErrorStream(), 8192));
            }
            String responseName = decodeNameHeader(connection.getHeaderField("X-Clipboard-File-Name"));
            String responseKind = connection.getHeaderField("X-Clipboard-File-Kind");
            String name = responseName == null || responseName.isEmpty() ? offer.name : responseName;
            String kind = responseKind == null ? offer.kind : responseKind.toLowerCase(Locale.ROOT);
            if (!"file".equals(kind) && !"folder".equals(kind)) {
                throw new IOException("o PC informou um tipo de arquivo inválido");
            }
            if (!kind.equals(offer.kind)) {
                throw new IOException("o tipo do conteúdo recebido não corresponde à oferta");
            }
            long announcedLength = parseLong(connection.getHeaderField("Content-Length"), -1);
            if (announcedLength > MAX_TRANSFER_BYTES) {
                throw new IOException("o arquivo excede o limite de 4 GiB do aplicativo");
            }
            long received;
            try (InputStream input = new BufferedInputStream(connection.getInputStream());
                 OutputStream stream = new BufferedOutputStream(new FileOutputStream(output))) {
                received = copyStream(input, stream, MAX_TRANSFER_BYTES, total ->
                        setFileStatus("Baixando " + offer.name + "... " + formatBytes(total)));
            }
            if (announcedLength >= 0 && received != announcedLength) {
                throw new IOException("o download terminou incompleto (" + formatBytes(received)
                        + " de " + formatBytes(announcedLength) + ")");
            }
            if (received != offer.size) {
                throw new IOException("o tamanho recebido não corresponde à oferta");
            }
            if (!expectedSha256.isEmpty()
                    && !expectedSha256.equalsIgnoreCase(sha256(output))) {
                throw new IOException("a verificação SHA-256 do download falhou");
            }
            complete = true;
            return new DownloadResult(output, name, kind, received);
        } finally {
            connection.disconnect();
            if (!complete) {
                output.delete();
            }
        }
    }

    private String saveFileToDownloads(File source, String requestedName) throws IOException {
        String fileName = safeSegment(requestedName, "arquivo");
        String relativePath = Environment.DIRECTORY_DOWNLOADS + "/";
        ContentValues values = new ContentValues();
        values.put(MediaStore.MediaColumns.DISPLAY_NAME,
                uniqueDownloadsFileName(relativePath, fileName));
        values.put(MediaStore.MediaColumns.MIME_TYPE, mimeTypeFor(fileName));
        values.put(MediaStore.MediaColumns.RELATIVE_PATH, relativePath);
        values.put(MediaStore.MediaColumns.IS_PENDING, 1);
        Uri destination = getContentResolver().insert(
                MediaStore.Downloads.EXTERNAL_CONTENT_URI, values);
        if (destination == null) {
            throw new IOException("não foi possível criar o arquivo em Downloads");
        }
        boolean published = false;
        try {
            copyFile(source, destination);
            ContentValues ready = new ContentValues();
            ready.put(MediaStore.MediaColumns.IS_PENDING, 0);
            if (getContentResolver().update(destination, ready, null, null) <= 0) {
                throw new IOException("não foi possível publicar o arquivo em Downloads");
            }
            published = true;
            return "Downloads/" + values.getAsString(MediaStore.MediaColumns.DISPLAY_NAME);
        } finally {
            if (!published) {
                getContentResolver().delete(destination, null, null);
            }
        }
    }

    private String uniqueDownloadsFileName(String relativePath, String desiredName) {
        String base = desiredName;
        String extension = "";
        int dot = desiredName.lastIndexOf('.');
        if (dot > 0) {
            base = desiredName.substring(0, dot);
            extension = desiredName.substring(dot);
        }
        for (int index = 0; index < 1000; index++) {
            String candidate = index == 0 ? desiredName
                    : base + " (" + index + ")" + extension;
            Cursor cursor = null;
            try {
                cursor = getContentResolver().query(
                        MediaStore.Downloads.EXTERNAL_CONTENT_URI,
                        new String[] {MediaStore.MediaColumns._ID},
                        MediaStore.MediaColumns.RELATIVE_PATH + "=? AND "
                                + MediaStore.MediaColumns.DISPLAY_NAME + "=?",
                        new String[] {relativePath, candidate},
                        null);
                if (cursor == null || !cursor.moveToFirst()) {
                    return candidate;
                }
            } catch (Exception ignored) {
                return base + " (" + System.currentTimeMillis() + ")" + extension;
            } finally {
                if (cursor != null) {
                    cursor.close();
                }
            }
        }
        return base + " (" + System.currentTimeMillis() + ")" + extension;
    }

    private String extractZipToDownloads(File zipFile, String requestedFolderName) throws IOException {
        List<ZipEntryInfo> entries = validateZip(zipFile);
        String folderName = safeSegment(requestedFolderName, "pasta_recebida")
                + "_" + new SimpleDateFormat("yyyyMMdd_HHmmss", Locale.US).format(new Date())
                + "_" + UUID.randomUUID().toString().substring(0, 8);
        String downloadsRoot = Environment.DIRECTORY_DOWNLOADS + "/" + folderName + "/";
        List<Uri> pendingFiles = new ArrayList<>();
        boolean published = false;
        try {
            extractZipToMediaStore(zipFile, entries, downloadsRoot, pendingFiles);
            for (Uri uri : pendingFiles) {
                ContentValues ready = new ContentValues();
                ready.put(MediaStore.MediaColumns.IS_PENDING, 0);
                if (getContentResolver().update(uri, ready, null, null) <= 0) {
                    throw new IOException("não foi possível publicar todos os arquivos em Downloads");
                }
            }
            published = true;
            return "Downloads/" + folderName;
        } finally {
            if (!published) {
                for (Uri uri : pendingFiles) {
                    getContentResolver().delete(uri, null, null);
                }
            }
        }
    }

    private void extractZipToMediaStore(
            File zipFile, List<ZipEntryInfo> entries, String downloadsRoot, List<Uri> created)
            throws IOException {
        Set<String> expected = new HashSet<>();
        for (ZipEntryInfo entry : entries) {
            expected.add(entry.path.toLowerCase(Locale.ROOT));
        }
        Set<String> extracted = new HashSet<>();
        long[] totalBytes = {0};
        try (ZipInputStream zip = new ZipInputStream(
                new BufferedInputStream(new FileInputStream(zipFile)))) {
            ZipEntry item;
            while ((item = zip.getNextEntry()) != null) {
                String path = normalizeZipPath(item);
                String key = path.toLowerCase(Locale.ROOT);
                if (!expected.contains(key) || !extracted.add(key)) {
                    throw new IOException("o ZIP mudou durante a extração");
                }
                if (item.isDirectory()) {
                    zip.closeEntry();
                    continue;
                }
                int slash = path.lastIndexOf('/');
                String parentPath = slash < 0 ? "" : path.substring(0, slash + 1);
                String fileName = path.substring(slash + 1);
                String relativePath = downloadsRoot + parentPath;
                ContentValues values = new ContentValues();
                values.put(MediaStore.MediaColumns.DISPLAY_NAME,
                        uniqueDownloadsFileName(relativePath, fileName));
                values.put(MediaStore.MediaColumns.MIME_TYPE, mimeTypeFor(fileName));
                values.put(MediaStore.MediaColumns.RELATIVE_PATH, relativePath);
                values.put(MediaStore.MediaColumns.IS_PENDING, 1);
                Uri destination = getContentResolver().insert(
                        MediaStore.Downloads.EXTERNAL_CONTENT_URI, values);
                if (destination == null) {
                    throw new IOException("não foi possível criar um arquivo da pasta em Downloads");
                }
                created.add(destination);
                try (OutputStream output = new BufferedOutputStream(
                        getContentResolver().openOutputStream(destination, "w"))) {
                    if (output == null) {
                        throw new IOException("não foi possível gravar um arquivo em Downloads");
                    }
                    totalBytes[0] += copyStream(zip, output,
                            MAX_UNPACKED_BYTES - totalBytes[0], null);
                }
                zip.closeEntry();
            }
        }
        if (extracted.size() != expected.size()) {
            throw new IOException("o ZIP não continha todos os itens validados");
        }
    }

    private String extractZipToTree(File zipFile, String requestedFolderName, Uri treeUri)
            throws IOException {
        List<ZipEntryInfo> entries = validateZip(zipFile);
        String folderName = safeSegment(requestedFolderName, "pasta_recebida");
        List<Uri> created = new ArrayList<>();
        try {
            String treeDocumentId = DocumentsContract.getTreeDocumentId(treeUri);
            Uri treeRoot = DocumentsContract.buildDocumentUriUsingTree(treeUri, treeDocumentId);
            Uri destinationRoot = createUniqueSafDocument(
                    treeUri, treeRoot, DocumentsContract.Document.MIME_TYPE_DIR, folderName);
            created.add(destinationRoot);

            java.util.HashMap<String, Uri> directories = new java.util.HashMap<>();
            directories.put("", destinationRoot);
            Set<String> expected = new HashSet<>();
            for (ZipEntryInfo entry : entries) {
                expected.add(entry.path.toLowerCase(Locale.ROOT));
            }
            Set<String> extracted = new HashSet<>();
            long[] totalBytes = {0};
            try (ZipInputStream zip = new ZipInputStream(
                    new BufferedInputStream(new FileInputStream(zipFile)))) {
                ZipEntry item;
                while ((item = zip.getNextEntry()) != null) {
                    String path = normalizeZipPath(item);
                    String key = path.toLowerCase(Locale.ROOT);
                    if (!expected.contains(key) || !extracted.add(key)) {
                        throw new IOException("o ZIP mudou durante a extração");
                    }
                    String[] segments = path.split("/");
                    int directoryCount = item.isDirectory() ? segments.length : segments.length - 1;
                    String currentPath = "";
                    Uri parent = destinationRoot;
                    for (int index = 0; index < directoryCount; index++) {
                        currentPath = currentPath.isEmpty()
                                ? segments[index] : currentPath + "/" + segments[index];
                        Uri known = directories.get(currentPath);
                        if (known == null) {
                            known = createUniqueSafDocument(treeUri, parent,
                                    DocumentsContract.Document.MIME_TYPE_DIR, segments[index]);
                            created.add(known);
                            directories.put(currentPath, known);
                        }
                        parent = known;
                    }
                    if (!item.isDirectory()) {
                        String fileName = segments[segments.length - 1];
                        Uri destination = createUniqueSafDocument(
                                treeUri, parent, mimeTypeFor(fileName), fileName);
                        created.add(destination);
                        try (OutputStream output = new BufferedOutputStream(
                                getContentResolver().openOutputStream(destination, "w"))) {
                            if (output == null) {
                                throw new IOException("não foi possível gravar um arquivo na pasta escolhida");
                            }
                            totalBytes[0] += copyStream(zip, output,
                                    MAX_UNPACKED_BYTES - totalBytes[0], null);
                        }
                    }
                    zip.closeEntry();
                }
            }
            if (extracted.size() != expected.size()) {
                throw new IOException("o ZIP não continha todos os itens validados");
            }
            return "pasta escolhida/" + folderName;
        } catch (Exception error) {
            for (int index = created.size() - 1; index >= 0; index--) {
                try {
                    DocumentsContract.deleteDocument(getContentResolver(), created.get(index));
                } catch (Exception ignored) {
                    // Melhor esforço para remover arquivos parciais do seletor SAF.
                }
            }
            if (error instanceof IOException) {
                throw (IOException) error;
            }
            throw new IOException("não foi possível extrair a pasta no destino escolhido", error);
        }
    }

    private Uri createUniqueSafDocument(
            Uri treeUri, Uri parent, String mimeType, String desiredName) throws IOException {
        Set<String> usedNames = new HashSet<>();
        try {
            String parentId = DocumentsContract.getDocumentId(parent);
            Uri children = DocumentsContract.buildChildDocumentsUriUsingTree(treeUri, parentId);
            Cursor cursor = getContentResolver().query(
                    children,
                    new String[] {DocumentsContract.Document.COLUMN_DISPLAY_NAME},
                    null,
                    null,
                    null);
            if (cursor != null) {
                try {
                    while (cursor.moveToNext()) {
                        usedNames.add(cursor.getString(0).toLowerCase(Locale.ROOT));
                    }
                } finally {
                    cursor.close();
                }
            }
        } catch (Exception ignored) {
            // O provedor pode não oferecer listagem; createDocument continuará sem sobrescrever.
        }
        String base = desiredName;
        String extension = "";
        int dot = desiredName.lastIndexOf('.');
        if (!DocumentsContract.Document.MIME_TYPE_DIR.equals(mimeType) && dot > 0) {
            base = desiredName.substring(0, dot);
            extension = desiredName.substring(dot);
        }
        for (int index = 0; index < 1000; index++) {
            String candidate = index == 0 ? desiredName : base + " (" + index + ")" + extension;
            if (!usedNames.contains(candidate.toLowerCase(Locale.ROOT))) {
                Uri createdDocument = DocumentsContract.createDocument(
                        getContentResolver(), parent, mimeType, candidate);
                if (createdDocument == null) {
                    throw new IOException("o provedor de arquivos não criou " + candidate);
                }
                return createdDocument;
            }
        }
        throw new IOException("não foi possível escolher um nome livre no destino");
    }

    private void saveFileToUri(File source, Uri destination) throws IOException {
        boolean complete = false;
        try {
            copyFile(source, destination);
            complete = true;
        } finally {
            if (!complete) {
                try {
                    DocumentsContract.deleteDocument(getContentResolver(), destination);
                } catch (Exception ignored) {
                    // O seletor SAF pode não permitir apagar a gravação parcial.
                }
            }
        }
    }

    private void copyFile(File source, Uri destination) throws IOException {
        try (InputStream input = new BufferedInputStream(new FileInputStream(source));
             OutputStream output = new BufferedOutputStream(
                     getContentResolver().openOutputStream(destination, "w"))) {
            if (output == null) {
                throw new IOException("não foi possível abrir o destino para gravação");
            }
            copyStream(input, output, MAX_TRANSFER_BYTES, total ->
                    setFileStatus("Salvando em Downloads... " + formatBytes(total)));
        }
    }

    private List<ZipEntryInfo> validateZip(File zipFile) throws IOException {
        List<ZipEntryInfo> entries = new ArrayList<>();
        Set<String> seen = new HashSet<>();
        Set<String> filePaths = new HashSet<>();
        TreeSet<String> paths = new TreeSet<>();
        long totalBytes = 0;
        try (ZipInputStream zip = new ZipInputStream(
                new BufferedInputStream(new FileInputStream(zipFile)))) {
            ZipEntry entry;
            byte[] buffer = new byte[BUFFER_SIZE];
            while ((entry = zip.getNextEntry()) != null) {
                if (entries.size() >= MAX_ZIP_ENTRIES) {
                    throw new IOException("a pasta tem mais de " + MAX_ZIP_ENTRIES + " itens");
                }
                String path = normalizeZipPath(entry);
                String key = path.toLowerCase(Locale.ROOT);
                if (!seen.add(key)) {
                    throw new IOException("o ZIP contém nomes repetidos: " + path);
                }
                String[] parts = path.split("/");
                String parent = "";
                for (int index = 0; index < parts.length - 1; index++) {
                    parent = parent.isEmpty() ? parts[index] : parent + "/" + parts[index];
                    if (filePaths.contains(parent.toLowerCase(Locale.ROOT))) {
                        throw new IOException("o ZIP tenta usar um arquivo como pasta: " + path);
                    }
                }
                if (!entry.isDirectory()) {
                    String prefix = key + "/";
                    String next = paths.ceiling(prefix);
                    if (next != null && next.startsWith(prefix)) {
                        throw new IOException("o ZIP contém conflito entre arquivo e pasta: " + path);
                    }
                    filePaths.add(key);
                }
                paths.add(key);
                entries.add(new ZipEntryInfo(path, entry.isDirectory()));

                int count;
                while ((count = zip.read(buffer)) != -1) {
                    if (totalBytes > MAX_UNPACKED_BYTES - count) {
                        throw new IOException("a pasta descompactada excede o limite de 4 GiB");
                    }
                    totalBytes += count;
                }
                zip.closeEntry();
            }
        }
        return entries;
    }

    private String normalizeZipPath(ZipEntry entry) throws IOException {
        String raw = entry.getName();
        if (raw == null || raw.isEmpty() || raw.startsWith("/") || raw.startsWith("\\")
                || raw.indexOf('\\') >= 0 || raw.indexOf(':') >= 0
                || raw.indexOf('\0') >= 0) {
            throw new IOException("o ZIP contém um caminho absoluto ou inválido");
        }
        boolean directory = entry.isDirectory();
        String normalized = directory && raw.endsWith("/")
                ? raw.substring(0, raw.length() - 1) : raw;
        if (normalized.isEmpty()) {
            throw new IOException("o ZIP contém uma entrada sem nome");
        }
        String[] parts = normalized.split("/", -1);
        for (String part : parts) {
            if (!isSafeZipSegment(part)) {
                throw new IOException("o ZIP contém um nome de caminho inseguro");
            }
        }
        return normalized;
    }

    private boolean isSafeZipSegment(String value) {
        if (value.isEmpty() || ".".equals(value) || "..".equals(value)
                || value.endsWith(".") || value.endsWith(" ")) {
            return false;
        }
        for (int index = 0; index < value.length(); index++) {
            char ch = value.charAt(index);
            if (Character.isISOControl(ch) || ch == ':' || ch == '\\' || ch == '/'
                    || ch == '*' || ch == '?' || ch == '"' || ch == '<'
                    || ch == '>' || ch == '|') {
                return false;
            }
        }
        return true;
    }

    private FolderZipResult zipFolder(Uri treeUri) throws IOException {
        File outputFile = File.createTempFile("clipsync-folder-", ".zip", getCacheDir());
        boolean complete = false;
        try (ZipOutputStream zip = new ZipOutputStream(
                new BufferedOutputStream(new FileOutputStream(outputFile)))) {
            String rootId = DocumentsContract.getTreeDocumentId(treeUri);
            long[] expandedBytes = {0};
            long[] fileCount = {0};
            addFolderContents(treeUri, rootId, "", zip, new HashSet<>(), new HashSet<>(),
                    expandedBytes, fileCount, 0);
            zip.finish();
            if (outputFile.length() > MAX_TRANSFER_BYTES) {
                throw new IOException("o ZIP da pasta excede o limite de 4 GiB");
            }
            complete = true;
            return new FolderZipResult(outputFile, expandedBytes[0], fileCount[0]);
        } catch (Exception error) {
            if (error instanceof IOException) {
                throw (IOException) error;
            }
            throw new IOException("não foi possível preparar o ZIP da pasta", error);
        } finally {
            if (!complete) {
                outputFile.delete();
            }
        }
    }

    private void addFolderContents(
            Uri treeUri,
            String documentId,
            String relativePath,
            ZipOutputStream zip,
            Set<String> visitedDirectories,
            Set<String> seenPaths,
            long[] totalBytes,
            long[] fileCount,
            int depth) throws IOException {
        if (depth > 64) {
            throw new IOException("a pasta tem níveis demais");
        }
        if (!visitedDirectories.add(documentId)) {
            throw new IOException("o provedor de arquivos retornou uma pasta repetida");
        }
        Uri childrenUri = DocumentsContract.buildChildDocumentsUriUsingTree(treeUri, documentId);
        String[] projection = {
                DocumentsContract.Document.COLUMN_DOCUMENT_ID,
                DocumentsContract.Document.COLUMN_DISPLAY_NAME,
                DocumentsContract.Document.COLUMN_MIME_TYPE
        };
        Cursor cursor = getContentResolver().query(childrenUri, projection, null, null, null);
        if (cursor == null) {
            throw new IOException("não foi possível listar os itens da pasta escolhida");
        }
        try {
            while (cursor.moveToNext()) {
                String childId = cursor.getString(0);
                String displayName = cursor.getString(1);
                String mimeType = cursor.getString(2);
                String segment = safeZipSegment(displayName);
                String path = relativePath.isEmpty() ? segment : relativePath + "/" + segment;
                if (seenPaths.size() >= MAX_ZIP_ENTRIES
                        || !seenPaths.add(path.toLowerCase(Locale.ROOT))) {
                    throw new IOException("a pasta tem muitos itens ou nomes repetidos");
                }
                if (DocumentsContract.Document.MIME_TYPE_DIR.equals(mimeType)) {
                    ZipEntry directory = new ZipEntry(path + "/");
                    zip.putNextEntry(directory);
                    zip.closeEntry();
                    addFolderContents(treeUri, childId, path, zip,
                            visitedDirectories, seenPaths, totalBytes, fileCount, depth + 1);
                } else {
                    fileCount[0]++;
                    ZipEntry fileEntry = new ZipEntry(path);
                    zip.putNextEntry(fileEntry);
                    Uri documentUri = DocumentsContract.buildDocumentUriUsingTree(treeUri, childId);
                    InputStream input = getContentResolver().openInputStream(documentUri);
                    if (input == null) {
                        throw new IOException("não foi possível ler " + displayName);
                    }
                    try (InputStream stream = new BufferedInputStream(input)) {
                        totalBytes[0] += copyStream(stream, zip,
                                MAX_UNPACKED_BYTES - totalBytes[0], null);
                    }
                    zip.closeEntry();
                }
            }
        } finally {
            cursor.close();
        }
    }

    private String safeZipSegment(String value) throws IOException {
        if (value == null || !isSafeZipSegment(value)) {
            throw new IOException("o provedor retornou um nome de arquivo inválido");
        }
        return value;
    }

    private void showUploadChoices() {
        if (fileTransferBusy || filePickerPending) {
            return;
        }
        new AlertDialog.Builder(this)
                .setTitle("Enviar para o PC")
                .setItems(new String[] {"Escolher arquivo", "Escolher pasta"}, (dialog, which) -> {
                    if (which == 0) {
                        chooseUploadFile();
                    } else {
                        chooseUploadFolder();
                    }
                })
                .setNegativeButton("Cancelar", null)
                .show();
    }

    private String sha256(File file) throws IOException {
        final MessageDigest digest;
        try {
            digest = MessageDigest.getInstance("SHA-256");
        } catch (NoSuchAlgorithmException error) {
            throw new IOException("SHA-256 não está disponível neste Android", error);
        }
        try (InputStream input = new BufferedInputStream(new FileInputStream(file))) {
            byte[] buffer = new byte[BUFFER_SIZE];
            int count;
            long total = 0;
            while ((count = input.read(buffer)) != -1) {
                if (count == 0) {
                    continue;
                }
                digest.update(buffer, 0, count);
                total += count;
                if (total % (16L * 1024L * 1024L) < count) {
                    setFileStatus("Verificando conteúdo... " + formatBytes(total));
                }
            }
        }
        byte[] hash = digest.digest();
        StringBuilder result = new StringBuilder(hash.length * 2);
        for (byte value : hash) {
            result.append(String.format(Locale.ROOT, "%02x", value & 0xff));
        }
        return result.toString();
    }

    private void chooseUploadFile() {
        Intent intent = new Intent(Intent.ACTION_OPEN_DOCUMENT);
        intent.addCategory(Intent.CATEGORY_OPENABLE);
        intent.setType("*/*");
        intent.putExtra(Intent.EXTRA_LOCAL_ONLY, true);
        launchUploadPicker(intent, REQUEST_UPLOAD_FILE);
    }

    private void chooseUploadFolder() {
        Intent intent = new Intent(Intent.ACTION_OPEN_DOCUMENT_TREE);
        intent.putExtra(Intent.EXTRA_LOCAL_ONLY, true);
        launchUploadPicker(intent, REQUEST_UPLOAD_FOLDER);
    }

    private void launchUploadPicker(Intent intent, int requestCode) {
        try {
            filePickerPending = true;
            refreshFileButtons();
            startActivityForResult(intent, requestCode);
        } catch (Exception error) {
            filePickerPending = false;
            refreshFileButtons();
            setFileStatus("Não foi possível abrir o seletor: " + error.getMessage());
        }
    }

    @Override
    protected void onActivityResult(int requestCode, int resultCode, Intent data) {
        super.onActivityResult(requestCode, resultCode, data);
        if (requestCode == REQUEST_UPLOAD_FILE || requestCode == REQUEST_UPLOAD_FOLDER) {
            filePickerPending = false;
            Uri uri = data == null ? null : data.getData();
            if (resultCode != RESULT_OK || uri == null) {
                refreshFileButtons();
                setFileStatus("Seleção cancelada.");
                return;
            }
            beginUpload(uri, requestCode == REQUEST_UPLOAD_FOLDER ? "folder" : "file");
            return;
        }
        if (requestCode == REQUEST_SAVE_FILE || requestCode == REQUEST_SAVE_FOLDER) {
            FileOffer offer = pendingLegacyOffer;
            pendingLegacyOffer = null;
            filePickerPending = false;
            Uri uri = data == null ? null : data.getData();
            if (resultCode != RESULT_OK || uri == null || offer == null) {
                fileTransferBusy = false;
                refreshFileButtons();
                setFileStatus("Escolha do destino cancelada.");
                return;
            }
            startReceiving(offer, uri);
        }
    }

    private void beginUpload(Uri uri, String kind) {
        fileTransferBusy = true;
        refreshFileButtons();
        setFileStatus("Preparando " + ("folder".equals(kind) ? "a pasta" : "o arquivo") + "...");
        fileTransferExecutor.execute(() -> uploadSelection(uri, kind));
    }

    private void uploadSelection(Uri uri, String kind) {
        File temporaryFile = null;
        try {
            String name;
            long expandedSize;
            long fileCount;
            if ("folder".equals(kind)) {
                name = queryTreeName(uri);
                FolderZipResult archive = zipFolder(uri);
                temporaryFile = archive.zipFile;
                expandedSize = archive.expandedSize;
                fileCount = archive.fileCount;
            } else {
                name = queryDisplayName(uri);
                // Preparar um snapshot torna o tamanho e o hash estáveis até o PUT.
                temporaryFile = copyUriToTemp(uri);
                expandedSize = temporaryFile.length();
                fileCount = 1;
            }
            name = safeSegment(name, "arquivo");
            long length = temporaryFile.length();
            if (length > MAX_TRANSFER_BYTES || expandedSize > MAX_UNPACKED_BYTES) {
                throw new IOException("o arquivo excede o limite de 4 GiB do aplicativo");
            }
            setFileStatus("Calculando verificação de " + name + "...");
            String sha256 = sha256(temporaryFile);
            Endpoint target = getEndpoint();
            JSONObject offer = new JSONObject()
                    .put("device_id", deviceId())
                    .put("device_name", deviceName())
                    .put("name", name)
                    .put("kind", kind)
                    .put("size", length)
                    .put("sha256", sha256)
                    .put("expanded_size", expandedSize)
                    .put("file_count", fileCount);
            JSONObject created = fileJsonRequest(
                    target, "POST", API_FILE_OFFERS, offer, Collections.emptyMap());
            String offerId = created.optString("id", "");
            if (offerId.isEmpty()) {
                throw new IOException("o PC não retornou o identificador da oferta");
            }
            setFileStatus("Oferta criada. Aguardando outra máquina aceitar " + name + "...");
            awaitOfferStatus(target, offerId, "accepted",
                    "Aguardando aceitação de " + name + "...");
            setFileStatus("Oferta aceita. Enviando " + name + "...");
            uploadTransfer(target, offerId, temporaryFile);
            awaitOfferStatus(target, offerId, "ready",
                    "O PC está verificando o conteúdo enviado...");
            setFileStatus("Enviado: " + name + " (" + formatBytes(length) + ").");
        } catch (Exception error) {
            setFileStatus("Falha ao enviar: " + error.getMessage());
        } finally {
            if (temporaryFile != null && temporaryFile.exists()) {
                temporaryFile.delete();
            }
            runOnUiThread(() -> {
                fileTransferBusy = false;
                refreshFileButtons();
            });
            refreshOffers();
        }
    }

    private void uploadTransfer(Endpoint target, String offerId, File source) throws Exception {
        long length = source.length();
        HttpURLConnection connection = openConnection(
                target, transferRoute(offerId, "content"), "PUT", "application/json");
        connection.setDoOutput(true);
        connection.setRequestProperty("Content-Type", "application/octet-stream");
        connection.setRequestProperty("X-Clipboard-Device-Id", deviceId());
        connection.setFixedLengthStreamingMode(length);
        try {
            try (InputStream input = new BufferedInputStream(new FileInputStream(source));
                 OutputStream output = new BufferedOutputStream(connection.getOutputStream())) {
                long sent = copyStream(input, output, length, total ->
                        setFileStatus("Enviando... " + formatBytes(total)
                                + " de " + formatBytes(length)));
                if (sent != length) {
                    throw new IOException("o conteúdo preparado mudou durante o envio");
                }
            }
            int code = connection.getResponseCode();
            String response = readLimited(
                    code >= 400 ? connection.getErrorStream() : connection.getInputStream(), 65536);
            if (code >= 400) {
                throw new HttpStatusException(code, "PC respondeu HTTP " + code + ": " + response);
            }
        } finally {
            connection.disconnect();
        }
    }

    private File copyUriToTemp(Uri uri) throws IOException {
        File temporaryFile = File.createTempFile("clipsync-upload-", ".bin", getCacheDir());
        boolean complete = false;
        try {
            InputStream opened = getContentResolver().openInputStream(uri);
            if (opened == null) {
                throw new IOException("não foi possível ler o arquivo selecionado");
            }
            try (InputStream input = new BufferedInputStream(opened);
                 OutputStream output = new BufferedOutputStream(new FileOutputStream(temporaryFile))) {
                copyStream(input, output, MAX_TRANSFER_BYTES, null);
            }
            complete = true;
            return temporaryFile;
        } finally {
            if (!complete) {
                temporaryFile.delete();
            }
        }
    }

    private String queryTreeName(Uri treeUri) {
        try {
            String documentId = DocumentsContract.getTreeDocumentId(treeUri);
            Uri documentUri = DocumentsContract.buildDocumentUriUsingTree(treeUri, documentId);
            return queryDisplayName(documentUri);
        } catch (Exception ignored) {
            return "pasta";
        }
    }

    private String queryDisplayName(Uri uri) {
        Cursor cursor = null;
        try {
            cursor = getContentResolver().query(
                    uri, new String[] {OpenableColumns.DISPLAY_NAME}, null, null, null);
            if (cursor != null && cursor.moveToFirst()) {
                String name = cursor.getString(0);
                if (name != null && !name.isEmpty()) {
                    return name;
                }
            }
        } catch (Exception ignored) {
            // Alguns provedores não implementam OpenableColumns.
        } finally {
            if (cursor != null) {
                cursor.close();
            }
        }
        return "arquivo";
    }

    private JSONObject fileJsonRequest(
        Endpoint target, String method, String path, JSONObject body,
            Map<String, String> headers) throws Exception {
        HttpURLConnection connection = openConnection(
                target, path, method, "application/json", headers);
        try {
            if (body != null) {
                byte[] bytes = body.toString().getBytes(StandardCharsets.UTF_8);
                connection.setDoOutput(true);
                connection.setRequestProperty("Content-Type", "application/json; charset=utf-8");
                connection.setFixedLengthStreamingMode(bytes.length);
                try (OutputStream output = connection.getOutputStream()) {
                    output.write(bytes);
                }
            }
            int code = connection.getResponseCode();
            String response = readLimited(
                    code >= 400 ? connection.getErrorStream() : connection.getInputStream(), 65536);
            if (code >= 400) {
                throw new HttpStatusException(code, "PC respondeu HTTP " + code + ": " + response);
            }
            return response.isEmpty() ? new JSONObject() : new JSONObject(response);
        } finally {
            connection.disconnect();
        }
    }

    private HttpURLConnection openConnection(
            Endpoint target, String path, String method, String accept) throws IOException {
        return openConnection(target, path, method, accept, Collections.emptyMap());
    }

    private HttpURLConnection openConnection(
            Endpoint target, String path, String method, String accept,
            Map<String, String> headers) throws IOException {
        URL url = new URL("http://" + target.host + ":" + target.port + path);
        HttpURLConnection connection = (HttpURLConnection) url.openConnection();
        connection.setRequestMethod(method);
        connection.setConnectTimeout(5000);
        connection.setReadTimeout(30000);
        connection.setUseCaches(false);
        connection.setRequestProperty("X-Clipboard-Token", target.token);
        connection.setRequestProperty("Accept", accept);
        for (Map.Entry<String, String> entry : headers.entrySet()) {
            connection.setRequestProperty(entry.getKey(), entry.getValue());
        }
        return connection;
    }

    private JSONObject identityPayload() throws Exception {
        return new JSONObject()
                .put("device_id", deviceId())
                .put("device_name", deviceName());
    }

    private JSONObject deviceIdPayload() throws Exception {
        return new JSONObject().put("device_id", deviceId());
    }

    private String deviceId() {
        synchronized (this) {
            android.content.SharedPreferences prefs =
                    getSharedPreferences(PREFS, MODE_PRIVATE);
            String saved = prefs.getString("android_device_id", null);
            if (saved != null && !saved.isEmpty()) {
                return saved;
            }
            String created = UUID.randomUUID().toString();
            prefs.edit().putString("android_device_id", created).commit();
            return created;
        }
    }

    private String deviceName() {
        String model = Build.MODEL == null ? "" : Build.MODEL.trim();
        if (model.isEmpty()) {
            model = "Android";
        } else {
            model = "Android " + model;
        }
        return model.length() > 120 ? model.substring(0, 120) : model;
    }

    private Map<String, String> deviceHeaders() {
        java.util.HashMap<String, String> headers = new java.util.HashMap<>();
        headers.put("X-Clipboard-Device-Id", deviceId());
        headers.put("X-Clipboard-Device-Name", encodeName(deviceName()));
        return headers;
    }

    private Map<String, String> statusHeaders() {
        java.util.HashMap<String, String> headers = new java.util.HashMap<>();
        headers.put("X-Clipboard-Device-Id", deviceId());
        return headers;
    }

    private String offerRoute(String id, String action) {
        return "/v2/offers/" + Uri.encode(id) + "/" + action;
    }

    private String transferRoute(String id, String action) {
        return "/v2/transfers/" + Uri.encode(id) + "/" + action;
    }

    private String encodeName(String value) {
        return Base64.encodeToString(
                value.getBytes(StandardCharsets.UTF_8),
                Base64.URL_SAFE | Base64.NO_PADDING | Base64.NO_WRAP);
    }

    private String decodeNameHeader(String value) {
        if (value == null || value.trim().isEmpty()) {
            return null;
        }
        try {
            byte[] bytes = Base64.decode(
                    value.trim(), Base64.URL_SAFE | Base64.NO_WRAP);
            return new String(bytes, StandardCharsets.UTF_8);
        } catch (IllegalArgumentException error) {
            return null;
        }
    }

    private String safeSegment(String value, String fallback) {
        if (value == null) {
            return fallback;
        }
        StringBuilder clean = new StringBuilder();
        for (int index = 0; index < value.length(); index++) {
            char ch = value.charAt(index);
            if (Character.isISOControl(ch) || ch == '/' || ch == '\\' || ch == ':'
                    || ch == '*' || ch == '?' || ch == '"' || ch == '<'
                    || ch == '>' || ch == '|') {
                clean.append('_');
            } else {
                clean.append(ch);
            }
        }
        String result = clean.toString().trim();
        while (result.startsWith(".")) {
            result = result.substring(1);
        }
        while (result.endsWith(".")) {
            result = result.substring(0, result.length() - 1);
        }
        if (result.isEmpty() || ".".equals(result) || "..".equals(result)) {
            return fallback;
        }
        if (result.length() > 180) {
            result = result.substring(0, 180);
        }
        return result;
    }

    private String mimeTypeFor(String fileName) {
        int dot = fileName.lastIndexOf('.');
        if (dot >= 0 && dot + 1 < fileName.length()) {
            String mime = MimeTypeMap.getSingleton()
                    .getMimeTypeFromExtension(fileName.substring(dot + 1).toLowerCase(Locale.ROOT));
            if (mime != null) {
                return mime;
            }
        }
        return "application/octet-stream";
    }

    private long copyStream(
            InputStream input, OutputStream output, long maximum, ProgressCallback callback)
            throws IOException {
        byte[] buffer = new byte[BUFFER_SIZE];
        long total = 0;
        long reported = 0;
        int count;
        while ((count = input.read(buffer)) != -1) {
            if (count == 0) {
                continue;
            }
            if (total > maximum - count) {
                throw new IOException("o conteúdo excede o limite de 4 GiB do aplicativo");
            }
            output.write(buffer, 0, count);
            total += count;
            if (callback != null && total - reported >= 4L * 1024L * 1024L) {
                callback.onProgress(total);
                reported = total;
            }
        }
        if (callback != null && total != reported) {
            callback.onProgress(total);
        }
        return total;
    }

    private String readLimited(InputStream stream, int maximumBytes) throws IOException {
        if (stream == null) {
            return "";
        }
        ByteArrayOutputStream result = new ByteArrayOutputStream();
        byte[] buffer = new byte[4096];
        int remaining = maximumBytes;
        try (InputStream input = stream) {
            int count;
            while (remaining > 0 && (count = input.read(buffer, 0, Math.min(buffer.length, remaining))) != -1) {
                result.write(buffer, 0, count);
                remaining -= count;
            }
        }
        return new String(result.toByteArray(), StandardCharsets.UTF_8);
    }

    private String readAll(InputStream stream) throws IOException {
        if (stream == null) {
            return "";
        }
        StringBuilder result = new StringBuilder();
        try (BufferedReader reader = new BufferedReader(
                new InputStreamReader(stream, StandardCharsets.UTF_8))) {
            String line;
            while ((line = reader.readLine()) != null) {
                result.append(line);
            }
        }
        return result.toString();
    }

    private long parseLong(String value, long fallback) {
        if (value == null) {
            return fallback;
        }
        try {
            return Long.parseLong(value);
        } catch (NumberFormatException ignored) {
            return fallback;
        }
    }

    private String formatBytes(long bytes) {
        if (bytes < 1024) {
            return bytes + " B";
        }
        if (bytes < 1024L * 1024L) {
            return String.format(Locale.getDefault(), "%.1f KiB", bytes / 1024.0);
        }
        if (bytes < 1024L * 1024L * 1024L) {
            return String.format(Locale.getDefault(), "%.1f MiB",
                    bytes / (1024.0 * 1024.0));
        }
        return String.format(Locale.getDefault(), "%.2f GiB",
                bytes / (1024.0 * 1024.0 * 1024.0));
    }

    private String formatExpiry(double seconds) {
        return "em " + Math.max(0, (long) Math.ceil(seconds)) + " s";
    }

    @Override
    protected void onDestroy() {
        destroyed = true;
        activityResumed = false;
        mainHandler.removeCallbacks(offerPollRunnable);
        if (multicastLock != null && multicastLock.isHeld()) {
            multicastLock.release();
        }
        executor.shutdownNow();
        filePollExecutor.shutdownNow();
        fileTransferExecutor.shutdownNow();
        super.onDestroy();
    }

    private interface ProgressCallback {
        void onProgress(long bytes);
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

    private static class FileOffer {
        final String id;
        final String name;
        final String kind;
        final long size;
        final String senderName;
        final String expiresAt;

        FileOffer(String id, String name, String kind, long size, String senderName, String expiresAt) {
            this.id = id;
            this.name = name;
            this.kind = kind;
            this.size = size;
            this.senderName = senderName == null || senderName.isEmpty() ? "PC" : senderName;
            this.expiresAt = expiresAt == null ? "" : expiresAt;
        }
    }

    private static class FolderZipResult {
        final File zipFile;
        final long expandedSize;
        final long fileCount;

        FolderZipResult(File zipFile, long expandedSize, long fileCount) {
            this.zipFile = zipFile;
            this.expandedSize = expandedSize;
            this.fileCount = fileCount;
        }
    }

    private static class DownloadResult {
        final File file;
        final String name;
        final String kind;
        final long size;

        DownloadResult(File file, String name, String kind, long size) {
            this.file = file;
            this.name = name;
            this.kind = kind;
            this.size = size;
        }
    }

    private static class ZipEntryInfo {
        final String path;
        final boolean directory;

        ZipEntryInfo(String path, boolean directory) {
            this.path = path;
            this.directory = directory;
        }
    }

    private static class HttpStatusException extends IOException {
        final int statusCode;

        HttpStatusException(int statusCode, String message) {
            super(message);
            this.statusCode = statusCode;
        }
    }
}
