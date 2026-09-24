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
	"apex/internal/probe"
	"apex/internal/streamer"
	"apex/internal/telegram"

	"github.com/gotd/td/tg"
)

func main() {
	log.Println("==================================================")
	log.Println(" 🚀 Starting Apex Core Daemon (Go / MTProto)      ")
	log.Println("==================================================")

	cfg := config.Load()

	// 1. Open SQLite database
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

	// Ingestion task queue (non-blocking decouple from Telegram event loop)
	jobQueue := make(chan *telegram.IngestionTask, 100)

	// Debounced Jellyfin Library Refresh Channel
	refreshNotify := make(chan struct{}, 20)

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	// Start Debounced Jellyfin Refresh Worker
	go func() {
		var debounceTimer *time.Timer
		debounceDuration := 8 * time.Second

		for {
			select {
			case <-ctx.Done():
				return
			case <-refreshNotify:
				if debounceTimer != nil {
					debounceTimer.Stop()
				}
				debounceTimer = time.AfterFunc(debounceDuration, func() {
					log.Println("[Apex] Debounce window elapsed. Requesting Jellyfin library scan...")
					if err := jfClient.RefreshLibrary(); err != nil {
						log.Printf("[Apex] Jellyfin library refresh notice: %v", err)
					} else {
						log.Println("[Apex] Jellyfin library refresh triggered successfully.")
					}
				})
			}
		}
	}()

	// Background Jellyfin Integration Health Monitor
	go func() {
		time.Sleep(8 * time.Second) // wait for Jellyfin service to initialize
		ticker := time.NewTicker(30 * time.Second)
		defer ticker.Stop()

		for {
			select {
			case <-ctx.Done():
				return
			case <-ticker.C:
				checkCtx, checkCancel := context.WithTimeout(ctx, 6*time.Second)
				reachable, authValid, hasMovies, hasShows, err := jfClient.VerifyIntegration(checkCtx)
				checkCancel()

				if !reachable {
					log.Printf("[Jellyfin Health] Service at %s not yet responding: %v", cfg.JellyfinURL, err)
				} else if !authValid {
					if cfg.JellyfinAPIKey == "" {
						log.Printf("[Jellyfin Health] Jellyfin is up. APEX_JELLYFIN_API_KEY is not configured yet. Complete wizard and set key.")
					} else {
						log.Printf("[Jellyfin Health] Warning: APEX_JELLYFIN_API_KEY was rejected by Jellyfin: %v", err)
					}
				} else {
					if !hasMovies || !hasShows {
						log.Printf("[Jellyfin Health] Provisioning missing libraries (Movies: %t, Shows: %t)...", hasMovies, hasShows)
						jfClient.EnsureDefaultLibraries()
					}
				}
			}
		}
	}()

	// Start Ingestion Worker Pool (2 concurrent workers)
	numWorkers := 2
	for w := 1; w <= numWorkers; w++ {
		go func(workerID int) {
			for {
				select {
				case <-ctx.Done():
					return
				case task, ok := <-jobQueue:
					if !ok {
						return
					}
					processIngestionTask(ctx, workerID, task, cfg, database, tmdbClient, jfWriter, refreshNotify)
				}
			}
		}(w)
	}

	// 4. Start Telegram Connection Manager
	if err := tgManager.Start(ctx, jobQueue); err != nil {
		log.Printf("[Apex] Warning starting Telegram: %v", err)
	}

	// 5. HTTP Router
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

	// 6. Signal Handling
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

func processIngestionTask(
	ctx context.Context,
	workerID int,
	task *telegram.IngestionTask,
	cfg *config.Config,
	database *db.Database,
	tmdbClient *metadata.Client,
	jfWriter *jellyfin.Writer,
	refreshNotify chan<- struct{},
) {
	doc := task.Doc

	// 1. Extract filename from document attributes
	filename := fmt.Sprintf("media_%d.mp4", doc.ID)
	for _, attr := range doc.Attributes {
		if fileAttr, ok := attr.(*tg.DocumentAttributeFilename); ok {
			filename = fileAttr.FileName
			break
		}
	}

	// 2. Validate allowed chat IDs if configured
	if len(cfg.TelegramAllowedChats) > 0 {
		allowed := false
		for _, allowedID := range cfg.TelegramAllowedChats {
			if allowedID == task.ChatID || allowedID == -task.ChatID || allowedID == (-1000000000000 - task.ChatID) {
				allowed = true
				break
			}
		}
		if !allowed {
			log.Printf("[Worker #%d] Ignored media from unauthorized chat ID: %d", workerID, task.ChatID)
			return
		}
	}

	// 3. Generate stable opaque ID: apx_<sha256[:12]>
	hasher := sha256.New()
	hasher.Write([]byte(fmt.Sprintf("%d:%d:%d", task.ChatID, task.MsgID, doc.ID)))
	opaqueID := fmt.Sprintf("apx_%s", hex.EncodeToString(hasher.Sum(nil))[:12])

	// Record job status in DB
	job := &db.ProcessingJob{
		ID:        opaqueID,
		ChatID:    task.ChatID,
		MessageID: task.MsgID,
		Filename:  filename,
		Caption:   task.Caption,
		Status:    "processing",
	}
	_ = database.SaveJob(job)

	log.Printf("[Worker #%d] Processing media: %s (ID: %s, Size: %d MB)",
		workerID, filename, opaqueID, doc.Size/(1024*1024))

	// 4. Parse Title / Season / Episode
	titleToParse := filename
	if strings.TrimSpace(task.Caption) != "" {
		cleanFn := filepath.Base(filename)
		if strings.HasPrefix(cleanFn, "media_") || strings.HasPrefix(cleanFn, "video_") || strings.HasPrefix(cleanFn, "file_") || len(cleanFn) < 8 {
			titleToParse = task.Caption
		}
	}

	parsed := parser.Parse(titleToParse)
	if parsed.CleanTitle == "" {
		parsed.CleanTitle = strings.TrimSuffix(filepath.Base(filename), filepath.Ext(filename))
		if parsed.CleanTitle == "" {
			parsed.CleanTitle = fmt.Sprintf("Media_%d", doc.ID)
		}
	}

	// 5. Query TMDB Metadata with Normalized Confidence Scoring
	var tmdbID int
	var plot string
	var rating float64
	var posterPath, backdropPath string

	tmdbMeta, err := tmdbClient.Search(parsed.CleanTitle, parsed.Year, parsed.MediaType)
	if err == nil && tmdbMeta != nil {
		if tmdbMeta.Confidence >= 0.85 {
			log.Printf("[Worker #%d] TMDB auto-accepted (confidence: %.2f) for %s", workerID, tmdbMeta.Confidence, parsed.CleanTitle)
			tmdbID = tmdbMeta.ID
			plot = tmdbMeta.Overview
			rating = tmdbMeta.VoteAverage
			posterPath = tmdbMeta.PosterPath
			backdropPath = tmdbMeta.BackdropPath
			if canonical := tmdbMeta.Title; canonical != "" {
				parsed.CleanTitle = canonical
			} else if tmdbMeta.Name != "" {
				parsed.CleanTitle = tmdbMeta.Name
			}
		} else if tmdbMeta.Confidence >= 0.70 {
			log.Printf("[Worker #%d] TMDB accepted with verification (confidence: %.2f) for %s", workerID, tmdbMeta.Confidence, parsed.CleanTitle)
			tmdbID = tmdbMeta.ID
			plot = tmdbMeta.Overview
			rating = tmdbMeta.VoteAverage
			posterPath = tmdbMeta.PosterPath
			backdropPath = tmdbMeta.BackdropPath
			if canonical := tmdbMeta.Title; canonical != "" {
				parsed.CleanTitle = canonical
			} else if tmdbMeta.Name != "" {
				parsed.CleanTitle = tmdbMeta.Name
			}
		} else {
			log.Printf("[Worker #%d] TMDB low confidence (%.2f < 0.70) for %s. Keeping original parsed title.", workerID, tmdbMeta.Confidence, parsed.CleanTitle)
		}
	}

	// 6. Build Virtual Filesystem Layout & NFO Metadata
	if parsed.MediaType == "series" {
		showDir := filepath.Join(cfg.JellyfinMedia, "Shows", parsed.CleanTitle)
		seasonDir := filepath.Join(showDir, fmt.Sprintf("Season %02d", parsed.Season))
		_ = os.MkdirAll(seasonDir, 0755)

		relDir := filepath.Join("Shows", parsed.CleanTitle, fmt.Sprintf("Season %02d", parsed.Season))

		startEp := parsed.Episode
		endEp := parsed.EndEpisode
		if endEp < startEp {
			endEp = startEp
		}

		var strmRelPath string
		if endEp > startEp {
			strmRelPath = filepath.Join(relDir, fmt.Sprintf("%s S%02dE%02d-E%02d.strm", parsed.CleanTitle, parsed.Season, startEp, endEp))
		} else {
			strmRelPath = filepath.Join(relDir, fmt.Sprintf("%s S%02dE%02d.strm", parsed.CleanTitle, parsed.Season, startEp))
		}

		fullStrmPath, err := jfWriter.WriteSTRM(strmRelPath, opaqueID)
		if err != nil {
			log.Printf("[Worker #%d] Error writing .strm: %v", workerID, err)
			job.Status = "failed"
			job.Error = err.Error()
			_ = database.SaveJob(job)
			return
		}

		// Show NFO & Artwork
		showNfoPath := filepath.Join(showDir, "tvshow.nfo")
		if _, err := os.Stat(showNfoPath); os.IsNotExist(err) {
			_ = os.WriteFile(showNfoPath, []byte(metadata.GenerateShowNFO(parsed.CleanTitle, plot, parsed.Year, rating, tmdbID)), 0644)
		}
		if posterPath != "" {
			_ = tmdbClient.DownloadImage(posterPath, filepath.Join(showDir, "poster.jpg"))
		}
		if backdropPath != "" {
			_ = tmdbClient.DownloadImage(backdropPath, filepath.Join(showDir, "backdrop.jpg"))
		}

		// Persist Series row
		seriesIDStr := fmt.Sprintf("series_%d", tmdbID)
		if tmdbID == 0 {
			seriesIDStr = fmt.Sprintf("series_%s", opaqueID)
		}
		seriesObj := &db.Series{
			ID:           seriesIDStr,
			Title:        parsed.CleanTitle,
			Year:         parsed.Year,
			TMDBID:       tmdbID,
			Overview:     plot,
			Rating:       rating,
			PosterPath:   posterPath,
			BackdropPath: backdropPath,
		}
		if err := database.SaveSeries(seriesObj); err != nil {
			log.Printf("[Worker #%d] Error saving series: %v", workerID, err)
		}

		// Persist Season row (FOREIGN KEY requirement for episodes)
		seasonObj := &db.Season{
			ID:           fmt.Sprintf("%s_s%02d", seriesObj.ID, parsed.Season),
			SeriesID:     seriesObj.ID,
			SeasonNumber: parsed.Season,
			Title:        fmt.Sprintf("Season %02d", parsed.Season),
		}
		if err := database.SaveSeason(seasonObj); err != nil {
			log.Printf("[Worker #%d] Error saving season: %v", workerID, err)
		}

		// Process each episode in the range (multi-episode or single)
		for epNum := startEp; epNum <= endEp; epNum++ {
			epTitle := fmt.Sprintf("Episode %d", epNum)
			epPlot := plot
			epRating := rating
			epStillPath := ""
			epAirDate := ""

			if tmdbID > 0 {
				if epMeta, err := tmdbClient.GetEpisodeDetails(tmdbID, parsed.Season, epNum); err == nil && epMeta != nil {
					if epMeta.Name != "" {
						epTitle = epMeta.Name
					}
					if epMeta.Overview != "" {
						epPlot = epMeta.Overview
					}
					if epMeta.VoteAverage > 0 {
						epRating = epMeta.VoteAverage
					}
					epStillPath = epMeta.StillPath
					epAirDate = epMeta.AirDate
				}
			}

			// Episode NFO & Still
			epNfoContent := metadata.GenerateEpisodeNFO(epTitle, parsed.Season, epNum, epPlot, epAirDate, epRating, tmdbID)
			epNfoPath := filepath.Join(seasonDir, fmt.Sprintf("%s S%02dE%02d.nfo", parsed.CleanTitle, parsed.Season, epNum))
			_ = os.WriteFile(epNfoPath, []byte(epNfoContent), 0644)

			if epStillPath != "" {
				_ = tmdbClient.DownloadImage(epStillPath, filepath.Join(seasonDir, fmt.Sprintf("%s S%02dE%02d-thumb.jpg", parsed.CleanTitle, parsed.Season, epNum)))
			}

			// Persist Episode row
			epObj := &db.Episode{
				ID:            fmt.Sprintf("ep_%s_%d", opaqueID, epNum),
				SeriesID:      seriesObj.ID,
				SeasonID:      seasonObj.ID,
				SeasonNumber:  parsed.Season,
				EpisodeNumber: epNum,
				Title:         epTitle,
				Overview:      epPlot,
				AirDate:       epAirDate,
				Rating:        epRating,
				StillPath:     epStillPath,
				StrmPath:      fullStrmPath,
			}
			if err := database.SaveEpisode(epObj); err != nil {
				log.Printf("[Worker #%d] Error saving episode %d: %v", workerID, epNum, err)
			}
		}
	} else {
		// Movie
		var relDir string
		var strmRelPath string
		if parsed.Year > 0 {
			relDir = filepath.Join("Movies", fmt.Sprintf("%s (%d)", parsed.CleanTitle, parsed.Year))
			strmRelPath = filepath.Join(relDir, fmt.Sprintf("%s (%d).strm", parsed.CleanTitle, parsed.Year))
		} else {
			relDir = filepath.Join("Movies", parsed.CleanTitle)
			strmRelPath = filepath.Join(relDir, fmt.Sprintf("%s.strm", parsed.CleanTitle))
		}
		movieDir := filepath.Join(cfg.JellyfinMedia, relDir)
		_ = os.MkdirAll(movieDir, 0755)

		fullStrmPath, err := jfWriter.WriteSTRM(strmRelPath, opaqueID)
		if err != nil {
			log.Printf("[Worker #%d] Error writing .strm: %v", workerID, err)
			job.Status = "failed"
			job.Error = err.Error()
			_ = database.SaveJob(job)
			return
		}

		// Movie NFO & Artwork
		nfoPath := filepath.Join(movieDir, "movie.nfo")
		nfoContent := metadata.GenerateMovieNFO(parsed.CleanTitle, plot, parsed.Year, rating, tmdbID)
		_ = os.WriteFile(nfoPath, []byte(nfoContent), 0644)

		if posterPath != "" {
			_ = tmdbClient.DownloadImage(posterPath, filepath.Join(movieDir, "poster.jpg"))
		}
		if backdropPath != "" {
			_ = tmdbClient.DownloadImage(backdropPath, filepath.Join(movieDir, "backdrop.jpg"))
		}

		movieObj := &db.Movie{
			ID:           fmt.Sprintf("movie_%s", opaqueID),
			Title:        parsed.CleanTitle,
			Year:         parsed.Year,
			TMDBID:       tmdbID,
			Overview:     plot,
			Rating:       rating,
			PosterPath:   posterPath,
			BackdropPath: backdropPath,
			StrmPath:     fullStrmPath,
		}
		if err := database.SaveMovie(movieObj); err != nil {
			log.Printf("[Worker #%d] Error saving movie: %v", workerID, err)
		}
	}

	// 7. Persist MediaItem lookup index
	strmPath := filepath.Join(cfg.JellyfinMedia, "Movies", fmt.Sprintf("%s.strm", parsed.CleanTitle))
	item := &db.MediaItem{
		ID:           opaqueID,
		SourceChatID: task.ChatID,
		MessageID:    task.MsgID,
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
		StrmPath:     strmPath,
	}
	_ = database.SaveMediaItem(item)

	// 8. Media Capability Probe Analysis
	streamURL := fmt.Sprintf("http://127.0.0.1:%s/stream/%s", cfg.ServerPort, opaqueID)
	cap, probeErr := probe.Analyze(ctx, streamURL, opaqueID, filename)
	if probeErr != nil {
		log.Printf("[Worker #%d] Probe inspection note on %s: %v", workerID, opaqueID, probeErr)
	}
	if cap != nil {
		_ = database.SaveCapabilities(cap)
	}

	// 9. Mark Job Completed
	job.Status = "completed"
	_ = database.SaveJob(job)

	log.Printf("[Worker #%d] Successfully cataloged: %s (ID: %s)", workerID, parsed.CleanTitle, opaqueID)

	// 10. Enqueue debounced Jellyfin refresh
	select {
	case refreshNotify <- struct{}{}:
	default:
	}
}
