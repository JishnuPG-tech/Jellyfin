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

	// Background Library Auto-provisioning: ensure default Movies and Shows libraries
	go func() {
		time.Sleep(12 * time.Second) // wait for Jellyfin to start listening
		for i := 0; i < 5; i++ {
			select {
			case <-ctx.Done():
				return
			default:
				jfClient.EnsureDefaultLibraries()
				time.Sleep(30 * time.Second)
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

	// 5. Query TMDB Metadata
	var tmdbID int
	var plot string
	var rating float64
	var posterPath, backdropPath string
	var stillPath, epAirDate string
	var epTitle string

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

	// Series episode metadata
	if parsed.MediaType == "series" && tmdbID > 0 {
		if epMeta, err := tmdbClient.GetEpisodeDetails(tmdbID, parsed.Season, parsed.Episode); err == nil && epMeta != nil {
			epTitle = epMeta.Name
			if epMeta.Overview != "" {
				plot = epMeta.Overview
			}
			if epMeta.VoteAverage > 0 {
				rating = epMeta.VoteAverage
			}
			stillPath = epMeta.StillPath
			epAirDate = epMeta.AirDate
		}
	}
	if epTitle == "" {
		epTitle = fmt.Sprintf("Episode %d", parsed.Episode)
	}

	// 6. Build Virtual Filesystem Layout
	var relDir string
	var strmRelPath string
	var nfoPath string
	var nfoContent string

	if parsed.MediaType == "series" {
		showDir := filepath.Join(cfg.JellyfinMedia, "Shows", parsed.CleanTitle)
		seasonDir := filepath.Join(showDir, fmt.Sprintf("Season %02d", parsed.Season))
		_ = os.MkdirAll(seasonDir, 0755)

		relDir = filepath.Join("Shows", parsed.CleanTitle, fmt.Sprintf("Season %02d", parsed.Season))
		strmRelPath = filepath.Join(relDir, fmt.Sprintf("%s S%02dE%02d.strm", parsed.CleanTitle, parsed.Season, parsed.Episode))

		// Show NFO & Artwork
		showNfoPath := filepath.Join(showDir, "tvshow.nfo")
		if _, err := os.Stat(showNfoPath); os.IsNotExist(err) {
			_ = os.WriteFile(showNfoPath, []byte(metadata.GenerateShowNFO(parsed.CleanTitle, tmdbMeta.Overview, parsed.Year, rating, tmdbID)), 0644)
		}
		if posterPath != "" {
			_ = tmdbClient.DownloadImage(posterPath, filepath.Join(showDir, "poster.jpg"))
		}
		if backdropPath != "" {
			_ = tmdbClient.DownloadImage(backdropPath, filepath.Join(showDir, "backdrop.jpg"))
		}

		// Episode NFO & Still
		nfoPath = filepath.Join(seasonDir, fmt.Sprintf("%s S%02dE%02d.nfo", parsed.CleanTitle, parsed.Season, parsed.Episode))
		nfoContent = metadata.GenerateEpisodeNFO(epTitle, parsed.Season, parsed.Episode, plot, epAirDate, rating, tmdbID)
		if stillPath != "" {
			_ = tmdbClient.DownloadImage(stillPath, filepath.Join(seasonDir, fmt.Sprintf("%s S%02dE%02d-thumb.jpg", parsed.CleanTitle, parsed.Season, parsed.Episode)))
		}
	} else {
		if parsed.Year > 0 {
			relDir = filepath.Join("Movies", fmt.Sprintf("%s (%d)", parsed.CleanTitle, parsed.Year))
			strmRelPath = filepath.Join(relDir, fmt.Sprintf("%s (%d).strm", parsed.CleanTitle, parsed.Year))
		} else {
			relDir = filepath.Join("Movies", parsed.CleanTitle)
			strmRelPath = filepath.Join(relDir, fmt.Sprintf("%s.strm", parsed.CleanTitle))
		}
		movieDir := filepath.Join(cfg.JellyfinMedia, relDir)
		_ = os.MkdirAll(movieDir, 0755)

		// Movie NFO & Artwork
		nfoPath = filepath.Join(movieDir, "movie.nfo")
		nfoContent = metadata.GenerateMovieNFO(parsed.CleanTitle, plot, parsed.Year, rating, tmdbID)
		if posterPath != "" {
			_ = tmdbClient.DownloadImage(posterPath, filepath.Join(movieDir, "poster.jpg"))
		}
		if backdropPath != "" {
			_ = tmdbClient.DownloadImage(backdropPath, filepath.Join(movieDir, "backdrop.jpg"))
		}
	}

	// 7. Write .strm virtual file pointer
	fullStrmPath, err := jfWriter.WriteSTRM(strmRelPath, opaqueID)
	if err != nil {
		log.Printf("[Worker #%d] Error writing .strm: %v", workerID, err)
		job.Status = "failed"
		job.Error = err.Error()
		_ = database.SaveJob(job)
		return
	}

	if nfoPath != "" && nfoContent != "" {
		_ = os.WriteFile(nfoPath, []byte(nfoContent), 0644)
	}

	// 8. Persist Normalized Database Records
	if parsed.MediaType == "series" {
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
		_ = database.SaveSeries(seriesObj)

		epObj := &db.Episode{
			ID:            fmt.Sprintf("ep_%s", opaqueID),
			SeriesID:      seriesObj.ID,
			SeasonID:      fmt.Sprintf("%s_s%02d", seriesObj.ID, parsed.Season),
			SeasonNumber:  parsed.Season,
			EpisodeNumber: parsed.Episode,
			Title:         epTitle,
			Overview:      plot,
			AirDate:       epAirDate,
			Rating:        rating,
			StillPath:     stillPath,
			StrmPath:      fullStrmPath,
		}
		_ = database.SaveEpisode(epObj)
	} else {
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
		_ = database.SaveMovie(movieObj)
	}

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
		StrmPath:     fullStrmPath,
	}
	_ = database.SaveMediaItem(item)

	// 9. Media Capability Probe Analysis
	streamURL := fmt.Sprintf("http://127.0.0.1:%s/stream/%s", cfg.ServerPort, opaqueID)
	cap, probeErr := probe.Analyze(ctx, streamURL, opaqueID, filename)
	if probeErr != nil {
		log.Printf("[Worker #%d] Probe inspection note on %s: %v", workerID, opaqueID, probeErr)
	}
	if cap != nil {
		_ = database.SaveCapabilities(cap)
	}

	// 10. Mark Job Completed
	job.Status = "completed"
	_ = database.SaveJob(job)

	log.Printf("[Worker #%d] Successfully cataloged: %s (ID: %s, STRM: %s)",
		workerID, parsed.CleanTitle, opaqueID, fullStrmPath)

	// 11. Enqueue debounced Jellyfin refresh
	select {
	case refreshNotify <- struct{}{}:
	default:
	}
}
