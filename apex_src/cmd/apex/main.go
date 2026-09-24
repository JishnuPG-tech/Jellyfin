package main

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"log"
	"net/http"
	"os"
	"os/signal"
	"path/filepath"
	"strconv"
	"strings"
	"syscall"
	"time"

	"apex/internal/config"
	"apex/internal/db"
	"apex/internal/jellyfin"
	"apex/internal/metadata"
	"apex/internal/parser"
	"apex/internal/streamer"
	"apex/internal/telegram"

	"github.com/gotd/td/tg"
)

func main() {
	log.Println("==================================================")
	log.Println(" 🚀 Starting Apex Core Daemon (Go / MTProto)      ")
	log.Println("==================================================")

	cfg := config.Load()

	// 1. Open live SQLite database
	database, err := db.Open(cfg.DBPath)
	if err != nil {
		log.Fatalf("[Apex] Fatal: Failed to initialize database: %v", err)
	}
	defer database.Close()
	log.Printf("[Apex] Database initialized at %s", cfg.DBPath)

	// 2. Initialize Two-Tier Cache & Streaming Gateway
	cache := streamer.NewLRUCache(cfg.MemoryCacheMB, cfg.DiskCacheMB, cfg.DiskCachePath)
	tgManager := telegram.NewManager(cfg, database)
	gateway := streamer.NewGateway(database, cache, tgManager)

	// 3. Initialize Metadata & Jellyfin Services
	tmdbClient := metadata.NewClient(cfg.TMDBAPIKey)
	jfWriter := jellyfin.NewWriter(cfg.JellyfinMedia)
	jfClient := jellyfin.NewClient(cfg.JellyfinURL, cfg.JellyfinAPIKey)

	// 4. Background Telegram Media Ingestion Handler
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	onNewMedia := func(ctx context.Context, doc *tg.Document, chatID int64, msgID int, caption string) {
		// Extract filename
		filename := fmt.Sprintf("media_%d.mp4", doc.ID)
		for _, attr := range doc.Attributes {
			if fileAttr, ok := attr.(*tg.DocumentAttributeFilename); ok {
				filename = fileAttr.FileName
				break
			}
		}

		// Security: Validate allowed chat IDs if configured
		if len(cfg.TelegramAllowedChats) > 0 {
			allowed := false
			for _, allowedID := range cfg.TelegramAllowedChats {
				if allowedID == chatID || allowedID == -chatID || allowedID == (-1000000000000 - chatID) {
					allowed = true
					break
				}
			}
			if !allowed {
				log.Printf("[Apex] Ignored media from unauthorized chat ID: %d (Allowed: %v)", chatID, cfg.TelegramAllowedChats)
				return
			}
		}

		log.Printf("[Apex] Ingesting media: %s (Chat: %d, Message: %d, Caption: %q)", filename, chatID, msgID, caption)

		// Parse metadata
		titleToParse := filename
		if strings.TrimSpace(caption) != "" {
			cleanFn := filepath.Base(filename)
			if strings.HasPrefix(cleanFn, "media_") || strings.HasPrefix(cleanFn, "video_") || strings.HasPrefix(cleanFn, "file_") || len(cleanFn) < 8 {
				titleToParse = caption
			}
		}

		parsed := parser.Parse(titleToParse)
		if parsed.CleanTitle == "" {
			parsed.CleanTitle = strings.TrimSuffix(filepath.Base(filename), filepath.Ext(filename))
			if parsed.CleanTitle == "" {
				parsed.CleanTitle = fmt.Sprintf("Media_%d", doc.ID)
			}
		}

		// Generate stable opaque ID: apx_<sha256[:12]>
		hasher := sha256.New()
		hasher.Write([]byte(fmt.Sprintf("%d:%d:%d", chatID, msgID, doc.ID)))
		opaqueID := fmt.Sprintf("apx_%s", hex.EncodeToString(hasher.Sum(nil))[:12])

		// Query TMDB
		var tmdbID int
		var plot string
		var rating float64
		var posterPath, backdropPath string

		tmdbMeta, err := tmdbClient.Search(parsed.CleanTitle, parsed.Year, parsed.MediaType)
		if err == nil && tmdbMeta != nil {
			tmdbID = tmdbMeta.ID
			plot = tmdbMeta.Overview
			rating = tmdbMeta.VoteAverage
			posterPath = tmdbMeta.PosterPath
			backdropPath = tmdbMeta.BackdropPath
			if parsed.Year == 0 {
				if parsed.MediaType == "series" && len(tmdbMeta.FirstAirDate) >= 4 {
					parsed.Year, _ = strconv.Atoi(tmdbMeta.FirstAirDate[:4])
				} else if len(tmdbMeta.ReleaseDate) >= 4 {
					parsed.Year, _ = strconv.Atoi(tmdbMeta.ReleaseDate[:4])
				}
			}
			canonicalTitle := tmdbMeta.Title
			if canonicalTitle == "" {
				canonicalTitle = tmdbMeta.Name
			}
			if canonicalTitle != "" {
				parsed.CleanTitle = canonicalTitle
			}
		}

		// Structure filesystem layout
		var relDir string
		var strmRelPath string

		if parsed.MediaType == "series" {
			relDir = filepath.Join("Shows", parsed.CleanTitle, fmt.Sprintf("Season %02d", parsed.Season))
			strmRelPath = filepath.Join(relDir, fmt.Sprintf("%s S%02dE%02d.strm", parsed.CleanTitle, parsed.Season, parsed.Episode))
		} else {
			if parsed.Year > 0 {
				relDir = filepath.Join("Movies", fmt.Sprintf("%s (%d)", parsed.CleanTitle, parsed.Year))
				strmRelPath = filepath.Join(relDir, fmt.Sprintf("%s (%d).strm", parsed.CleanTitle, parsed.Year))
			} else {
				relDir = filepath.Join("Movies", parsed.CleanTitle)
				strmRelPath = filepath.Join(relDir, fmt.Sprintf("%s.strm", parsed.CleanTitle))
			}
		}

		// Write .strm pointer
		fullStrmPath, err := jfWriter.WriteSTRM(strmRelPath, opaqueID)
		if err != nil {
			log.Printf("[Apex] Error writing .strm: %v", err)
			return
		}

		targetBase := filepath.Dir(fullStrmPath)

		// Download artwork if available
		if posterPath != "" {
			_ = tmdbClient.DownloadImage(posterPath, filepath.Join(targetBase, "poster.jpg"))
		}
		if backdropPath != "" {
			_ = tmdbClient.DownloadImage(backdropPath, filepath.Join(targetBase, "backdrop.jpg"))
		}

		// Write .nfo metadata
		nfoContent := metadata.GenerateNFO(parsed.CleanTitle, plot, parsed.Year, rating, tmdbID, parsed.MediaType)
		nfoPath := filepath.Join(targetBase, "movie.nfo")
		if parsed.MediaType == "series" {
			nfoPath = filepath.Join(targetBase, "tvshow.nfo")
		}
		_ = os.WriteFile(nfoPath, []byte(nfoContent), 0644)

		// Persist in SQLite
		item := &db.MediaItem{
			ID:           opaqueID,
			SourceChatID: chatID,
			MessageID:    msgID,
			FileID:       fmt.Sprintf("%d", doc.ID),
			FileUniqueID: fmt.Sprintf("%d", doc.ID),
			FileRef:      doc.FileReference,
			AccessHash:   doc.AccessHash,
			FileSize:     doc.Size,
			MimeType:     doc.MimeType,
			CleanTitle:   parsed.CleanTitle,
			MediaType:    parsed.MediaType,
			Year:         parsed.Year,
			Season:       parsed.Season,
			Episode:      parsed.Episode,
			TMDBID:       tmdbID,
			StrmPath:     fullStrmPath,
		}

		_ = database.SaveMediaItem(item)

		// Direct Play Capability Profile
		cap := &db.MediaCapability{
			MediaID:        opaqueID,
			Container:      filepath.Ext(filename),
			DirectPlaySafe: true,
		}
		_ = database.SaveCapabilities(cap)

		log.Printf("[Apex] Successfully cataloged: %s (ID: %s, STRM: %s)", parsed.CleanTitle, opaqueID, fullStrmPath)

		// Trigger Jellyfin Library Refresh
		if err := jfClient.RefreshLibrary(); err != nil {
			log.Printf("[Apex] Notice: Jellyfin library refresh request: %v", err)
		} else {
			log.Printf("[Apex] Jellyfin library refresh triggered successfully.")
		}
	}

	// 5. Start Telegram Connection Manager
	if err := tgManager.Start(ctx, onNewMedia); err != nil {
		log.Printf("[Apex] Warning starting Telegram: %v", err)
	}

	// 6. HTTP Router
	mux := http.NewServeMux()
	mux.Handle("/stream/", gateway)
	mux.Handle("/vlc/", gateway)

	mux.HandleFunc("/apex/health", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		fmt.Fprintf(w, `{"status":"ok","time":"%s"}`, time.Now().Format(time.RFC3339))
	})

	server := &http.Server{
		Addr:    ":" + cfg.ServerPort,
		Handler: mux,
	}

	go func() {
		log.Printf("[Apex] HTTP Gateway listening on :%s", cfg.ServerPort)
		if err := server.ListenAndServe(); err != nil && err != http.ErrServerClosed {
			log.Fatalf("[Apex] Server listen error: %v", err)
		}
	}()

	// 7. Signal Handling
	sigChan := make(chan os.Signal, 1)
	signal.Notify(sigChan, syscall.SIGINT, syscall.SIGTERM)
	<-sigChan

	log.Println("[Apex] Termination signal received. Shutting down...")
	cancel()

	shutdownCtx, shutdownCancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer shutdownCancel()
	_ = server.Shutdown(shutdownCtx)

	log.Println("[Apex] Shutdown complete.")
}
