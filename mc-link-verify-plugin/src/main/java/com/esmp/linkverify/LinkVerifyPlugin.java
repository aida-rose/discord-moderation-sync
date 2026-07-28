package com.esmp.linkverify;

import org.bukkit.Bukkit;
import org.bukkit.ChatColor;
import org.bukkit.command.Command;
import org.bukkit.command.CommandExecutor;
import org.bukkit.command.CommandSender;
import org.bukkit.entity.Player;
import org.bukkit.plugin.java.JavaPlugin;

import java.io.IOException;
import java.lang.reflect.Method;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.time.Duration;
import java.util.Locale;
import java.util.UUID;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

public final class LinkVerifyPlugin extends JavaPlugin implements CommandExecutor {
    private static final Pattern JSON_STRING_FIELD = Pattern.compile("\"%s\"\\s*:\\s*\"((?:\\\\.|[^\"])*)\"");
    private static final Pattern JSON_BOOLEAN_FIELD = Pattern.compile("\"%s\"\\s*:\\s*(true|false)");

    private HttpClient httpClient;

    @Override
    public void onEnable() {
        saveDefaultConfig();
        httpClient = HttpClient.newBuilder()
            .connectTimeout(Duration.ofSeconds(8))
            .build();

        if (getCommand("link") != null) {
            getCommand("link").setExecutor(this);
        }
    }

    @Override
    public boolean onCommand(CommandSender sender, Command command, String label, String[] args) {
        if (!(sender instanceof Player player)) {
            sender.sendMessage(color("&cOnly players can use this command."));
            return true;
        }

        if (args.length != 1) {
            player.sendMessage(message("usage"));
            return true;
        }

        String code = args[0].trim().toUpperCase(Locale.ROOT);
        if (code.isEmpty()) {
            player.sendMessage(message("usage"));
            return true;
        }

        String token = getConfig().getString("bot.token", "");
        String verifyUrl = getConfig().getString("bot.verify-url", "");

        if (token == null || token.isBlank() || "change-me".equals(token) || verifyUrl == null || verifyUrl.isBlank()) {
            player.sendMessage(message("server-error"));
            getLogger().warning("LinkVerify is not configured. Set bot.verify-url and bot.token in config.yml.");
            return true;
        }

        FloodgateInfo floodgate = floodgateInfo(player.getUniqueId());
        String platform = floodgate.isBedrock() ? "bedrock" : "java";
        String payload = jsonPayload(code, platform, player, floodgate);

        player.sendMessage(message("linking"));

        Bukkit.getScheduler().runTaskAsynchronously(this, () -> sendVerification(player, verifyUrl, token, payload));
        return true;
    }

    private void sendVerification(Player player, String verifyUrl, String token, String payload) {
        HttpRequest request = HttpRequest.newBuilder()
            .uri(URI.create(verifyUrl))
            .timeout(Duration.ofSeconds(12))
            .header("Content-Type", "application/json")
            .header("X-MC-Verify-Token", token)
            .POST(HttpRequest.BodyPublishers.ofString(payload))
            .build();

        try {
            HttpResponse<String> response = httpClient.send(request, HttpResponse.BodyHandlers.ofString());
            VerificationResponse parsed = parseResponse(response.body());

            Bukkit.getScheduler().runTask(this, () -> handleResponse(player, response.statusCode(), parsed));
        } catch (IOException | InterruptedException | IllegalArgumentException error) {
            if (error instanceof InterruptedException) {
                Thread.currentThread().interrupt();
            }

            getLogger().warning("Verification request failed: " + error.getMessage());
            Bukkit.getScheduler().runTask(this, () -> {
                if (player.isOnline()) {
                    player.sendMessage(message("server-error"));
                }
            });
        }
    }

    private void handleResponse(Player player, int statusCode, VerificationResponse response) {
        if (!player.isOnline()) {
            return;
        }

        if (statusCode >= 200 && statusCode < 300 && response.ok()) {
            player.sendMessage(message("success"));

            if (response.message() != null && !response.message().isBlank()) {
                player.sendMessage(color("&7" + response.message()));
            }

            if (response.kick() && getConfig().getBoolean("kick.enabled", true)) {
                long delay = getConfig().getLong("kick.delay-ticks", 20L);
                String kickMessage = getConfig().getString("kick.message", "Your Minecraft account has been linked.");
                Bukkit.getScheduler().runTaskLater(this, () -> {
                    if (player.isOnline()) {
                        player.kickPlayer(color(kickMessage));
                    }
                }, Math.max(0L, delay));
            }

            return;
        }

        String message = response.message();
        if (message == null || message.isBlank()) {
            message = "The bot rejected this verification request.";
        }

        player.sendMessage(message("failure").replace("{message}", message));
    }

    private FloodgateInfo floodgateInfo(UUID uuid) {
        try {
            Class<?> apiClass = Class.forName("org.geysermc.floodgate.api.FloodgateApi");
            Object api = apiClass.getMethod("getInstance").invoke(null);

            Method isFloodgatePlayer = apiClass.getMethod("isFloodgatePlayer", UUID.class);
            boolean bedrock = Boolean.TRUE.equals(isFloodgatePlayer.invoke(api, uuid));

            if (!bedrock) {
                return FloodgateInfo.javaPlayer();
            }

            Method getPlayer = apiClass.getMethod("getPlayer", UUID.class);
            Object floodgatePlayer = getPlayer.invoke(api, uuid);

            String username = "";
            String xuid = "";

            if (floodgatePlayer != null) {
                username = reflectString(floodgatePlayer, "getUsername");
                xuid = reflectString(floodgatePlayer, "getXuid");
            }

            return new FloodgateInfo(true, username, xuid);
        } catch (ReflectiveOperationException | LinkageError error) {
            return FloodgateInfo.javaPlayer();
        }
    }

    private String reflectString(Object target, String methodName) {
        try {
            Object value = target.getClass().getMethod(methodName).invoke(target);
            return value == null ? "" : String.valueOf(value);
        } catch (ReflectiveOperationException error) {
            return "";
        }
    }

    private String jsonPayload(String code, String platform, Player player, FloodgateInfo floodgate) {
        return "{"
            + "\"code\":\"" + jsonEscape(code) + "\","
            + "\"platform\":\"" + jsonEscape(platform) + "\","
            + "\"player_name\":\"" + jsonEscape(player.getName()) + "\","
            + "\"player_uuid\":\"" + jsonEscape(player.getUniqueId().toString()) + "\","
            + "\"is_bedrock\":" + floodgate.isBedrock() + ","
            + "\"bedrock_username\":\"" + jsonEscape(floodgate.username()) + "\","
            + "\"xuid\":\"" + jsonEscape(floodgate.xuid()) + "\""
            + "}";
    }

    private VerificationResponse parseResponse(String json) {
        if (json == null || json.isBlank()) {
            return new VerificationResponse(false, false, "");
        }

        return new VerificationResponse(
            jsonBoolean(json, "ok"),
            jsonBoolean(json, "kick"),
            jsonString(json, "message")
        );
    }

    private boolean jsonBoolean(String json, String field) {
        Matcher matcher = Pattern.compile(String.format(JSON_BOOLEAN_FIELD.pattern(), Pattern.quote(field))).matcher(json);
        return matcher.find() && "true".equalsIgnoreCase(matcher.group(1));
    }

    private String jsonString(String json, String field) {
        Matcher matcher = Pattern.compile(String.format(JSON_STRING_FIELD.pattern(), Pattern.quote(field))).matcher(json);

        if (!matcher.find()) {
            return "";
        }

        return jsonUnescape(matcher.group(1));
    }

    private String jsonEscape(String value) {
        if (value == null) {
            return "";
        }

        return value
            .replace("\\", "\\\\")
            .replace("\"", "\\\"")
            .replace("\b", "\\b")
            .replace("\f", "\\f")
            .replace("\n", "\\n")
            .replace("\r", "\\r")
            .replace("\t", "\\t");
    }

    private String jsonUnescape(String value) {
        return value
            .replace("\\\"", "\"")
            .replace("\\\\", "\\")
            .replace("\\n", "\n")
            .replace("\\r", "\r")
            .replace("\\t", "\t")
            .replace("\\b", "\b")
            .replace("\\f", "\f");
    }

    private String message(String key) {
        return color(getConfig().getString("messages." + key, ""));
    }

    private String color(String value) {
        return ChatColor.translateAlternateColorCodes('&', value == null ? "" : value);
    }

    private record FloodgateInfo(boolean isBedrock, String username, String xuid) {
        static FloodgateInfo javaPlayer() {
            return new FloodgateInfo(false, "", "");
        }
    }

    private record VerificationResponse(boolean ok, boolean kick, String message) {
    }
}
