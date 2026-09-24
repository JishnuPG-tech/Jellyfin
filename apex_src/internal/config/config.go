package config

import (
	"os"
	"strconv"
	"strings"
)

type Config struct {
	TelegramAPIID        int
	TelegramAPIHash      string
	TelegramBotToken     string
	TelegramAllowedChats []int64
	TMDBAPIKey           string
	ApexSecretKey        string

	// Tunable performance options
	MemoryCacheMB int
	DiskCacheMB   int
	PrefetchMB    int
	MaxStreams    int
	MediaClients  int

	// Path configurations
	DBPath          string
	DiskCachePath   string
	JellyfinMedia   string
	SessionFilePath string
	ServerPort      string
}

func Load() *Config {
	apiID, _ := strconv.Atoi(getEnv("TELEGRAM_API_ID", "0"))
	memMB, _ := strconv.Atoi(getEnv("APEX_MEMORY_CACHE_MB", "128"))
	diskMB, _ := strconv.Atoi(getEnv("APEX_DISK_CACHE_MB", "2048"))
	prefetchMB, _ := strconv.Atoi(getEnv("APEX_PREFETCH_MB", "16"))
	maxStreams, _ := strconv.Atoi(getEnv("APEX_MAX_STREAMS", "2"))
	mediaClients, _ := strconv.Atoi(getEnv("APEX_TELEGRAM_MEDIA_CLIENTS", "2"))

	allowedChatsRaw := getEnv("TELEGRAM_ALLOWED_CHAT_IDS", "")
	var allowedChats []int64
	if allowedChatsRaw != "" {
		for _, part := range strings.Split(allowedChatsRaw, ",") {
			part = strings.TrimSpace(part)
			if id, err := strconv.ParseInt(part, 10, 64); err == nil {
				allowedChats = append(allowedChats, id)
			}
		}
	}

	return &Config{
		TelegramAPIID:        apiID,
		TelegramAPIHash:      getEnv("TELEGRAM_API_HASH", ""),
		TelegramBotToken:     getEnv("TELEGRAM_BOT_TOKEN", ""),
		TelegramAllowedChats: allowedChats,
		TMDBAPIKey:           getEnv("TMDB_API_KEY", ""),
		ApexSecretKey:        getEnv("APEX_SECRET_KEY", "apex_default_secret_key_change_me"),

		MemoryCacheMB: memMB,
		DiskCacheMB:   diskMB,
		PrefetchMB:    prefetchMB,
		MaxStreams:    maxStreams,
		MediaClients:  mediaClients,

		DBPath:          getEnv("APEX_DB_PATH", "/tmp/apex-db/apex.db"),
		DiskCachePath:   getEnv("APEX_DISK_CACHE_PATH", "/tmp/apex-stream-cache"),
		JellyfinMedia:   getEnv("JELLYFIN_MEDIA_DIR", "/data/jellyfin/media"),
		SessionFilePath: getEnv("APEX_SESSION_FILE", "/data/apex/session/session.json"),
		ServerPort:      getEnv("APEX_CORE_PORT", "8084"),
	}
}

func getEnv(key, defaultVal string) string {
	if val := os.Getenv(key); val != "" {
		return val
	}
	return defaultVal
}
