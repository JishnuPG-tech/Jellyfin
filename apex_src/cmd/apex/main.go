package main

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
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
	gateway := streamer.NewGateway(cfg, database, cache, tgManager)

	// 3. Initialize Metadata & Jellyfin Services
	apiKeyFile := filepath.Join(filepath.Dir(cfg.SessionFilePath), "jellyfin_api_key.txt")
	if cfg.JellyfinAPIKey == "" {
		if data, err := os.ReadFile(apiKeyFile); err == nil {
			savedKey := strings.TrimSpace(string(data))
			if savedKey != "" {
				log.Println("[Apex] Loaded persisted APEX_JELLYFIN_API_KEY from storage.")
				cfg.JellyfinAPIKey = savedKey
			}
		}
	}

	tmdbClient := metadata.NewClient(cfg.TMDBAPIKey, cfg.MetadataCacheDir)
	jfWriter := jellyfin.NewWriter(cfg.JellyfinMedia)
	jfClient := jellyfin.NewClient(cfg.JellyfinURL, cfg.JellyfinAPIKey)

	// Ingestion task queue (non-blocking decouple from Telegram event loop)
	jobQueue := make(chan *telegram.IngestionTask, 500)

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
					if jfClient.GetAPIKey() == "" {
						log.Printf("[Jellyfin Health] Jellyfin is up. APEX_JELLYFIN_API_KEY is not configured yet. Complete wizard and set key via /apex/api-key.")
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
		checkCtx, checkCancel := context.WithTimeout(r.Context(), 3*time.Second)
		defer checkCancel()

		reachable, authValid, hasMovies, hasShows, _ := jfClient.VerifyIntegration(checkCtx)
		status := map[string]interface{}{
			"status": "ok",
			"time":   time.Now().Format(time.RFC3339),
			"jellyfin": map[string]interface{}{
				"reachable":      reachable,
				"authenticated":  authValid,
				"movies_library": hasMovies,
				"shows_library":  hasShows,
			},
		}
		_ = json.NewEncoder(w).Encode(status)
	})

	// Bootstrap API key endpoint
	mux.HandleFunc("/apex/api-key", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			http.Error(w, "Method Not Allowed. Use POST with JSON: {\"api_key\":\"...\"}", http.StatusMethodNotAllowed)
			return
		}

		// Authenticate request using APEX_SECRET_KEY via headers ONLY
		authHeader := r.Header.Get("Authorization")
		secretHeader := r.Header.Get("X-Apex-Secret")

		authenticated := false
		if secretHeader != "" && secretHeader == cfg.ApexSecretKey {
			authenticated = true
		} else if strings.HasPrefix(authHeader, "Bearer ") && strings.TrimPrefix(authHeader, "Bearer ") == cfg.ApexSecretKey {
			authenticated = true
		}

		if !authenticated {
			http.Error(w, `{"error":"unauthorized: valid APEX_SECRET_KEY required in X-Apex-Secret or Authorization header"}`, http.StatusUnauthorized)
			return
		}

		var payload struct {
			APIKey string `json:"api_key"`
		}
		if err := json.NewDecoder(r.Body).Decode(&payload); err != nil || strings.TrimSpace(payload.APIKey) == "" {
			http.Error(w, `{"error":"JSON body with 'api_key' is required"}`, http.StatusBadRequest)
			return
		}

		key := strings.TrimSpace(payload.APIKey)
		testClient := jellyfin.NewClient(cfg.JellyfinURL, key)
		checkCtx, checkCancel := context.WithTimeout(r.Context(), 5*time.Second)
		defer checkCancel()

		reachable, authValid, hasMovies, hasShows, err := testClient.VerifyIntegration(checkCtx)
		if !reachable {
			w.WriteHeader(http.StatusBadGateway)
			fmt.Fprintf(w, `{"error":"jellyfin server unreachable: %v"}`, err)
			return
		}
		if !authValid {
			w.WriteHeader(http.StatusUnauthorized)
			fmt.Fprintf(w, `{"error":"API key rejected by Jellyfin: %v"}`, err)
			return
		}

		// Save and apply key
		jfClient.SetAPIKey(key)
		_ = os.WriteFile(apiKeyFile, []byte(key), 0600)
		jfClient.EnsureDefaultLibraries()

		w.Header().Set("Content-Type", "application/json")
		fmt.Fprintf(w, `{"status":"ok","message":"API key verified and saved","movies_library":%t,"shows_library":%t}`, hasMovies, hasShows)
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

	// 3. Duplicate Detection via Universal Telegram doc.ID
	docIDStr := fmt.Sprintf("%d", doc.ID)
	if existing, err := database.GetMediaItemByFileID(docIDStr); err == nil && existing != nil {
		log.Printf("[Worker #%d] Media already cataloged as '%s' (ID: %s, STRM: %s). Updating file reference...",
			workerID, existing.CleanTitle, existing.ID, existing.StrmPath)
		_ = database.UpdateFileReference(existing.ID, doc.FileReference, doc.AccessHash)

		// Verify STRM still exists; recreate if missing
		if _, err := os.Stat(existing.StrmPath); os.IsNotExist(err) {
			log.Printf("[Worker #%d] Missing STRM file detected for '%s'. Recreating...", workerID, existing.ID)
			streamURL := fmt.Sprintf("http://127.0.0.1:8084/stream/%s", existing.ID)
			_ = os.MkdirAll(filepath.Dir(existing.StrmPath), 0755)
			_ = os.WriteFile(existing.StrmPath, []byte(streamURL), 0644)
		}

		// Enqueue Jellyfin refresh to ensure it's picked up
		select {
		case refreshNotify <- struct{}{}:
		default:
		}
		return
	}

	// 4. Generate stable opaque ID: apx_<sha256[:12]>
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

	// 5. Parse Title / Season / Episode
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

	// 6. Fetch Rich TMDB Details (Cast, Genres, IMDb ID, Tagline)
	var detailedMeta *metadata.DetailedMetadata
	if tmdbID > 0 {
		if parsed.MediaType == "series" {
			detailedMeta, _ = tmdbClient.GetShowDetails(tmdbID)
		} else {
			detailedMeta, _ = tmdbClient.GetMovieDetails(tmdbID)
		}
	}

	// 7. Build Virtual Filesystem Layout & NFO Metadata
	var primaryStrmPath string

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

		// Show NFO & Artwork (preserving existing artwork if already present)
		showNfoPath := filepath.Join(showDir, "tvshow.nfo")
		if _, err := os.Stat(showNfoPath); os.IsNotExist(err) {
			_ = os.WriteFile(showNfoPath, []byte(metadata.GenerateRichShowNFO(parsed.CleanTitle, plot, parsed.Year, rating, tmdbID, detailedMeta)), 0644)
		}
		if posterPath != "" {
			posterDest := filepath.Join(showDir, "poster.jpg")
			if _, err := os.Stat(posterDest); os.IsNotExist(err) {
				_ = tmdbClient.DownloadImage(posterPath, posterDest)
			}
		}
		if backdropPath != "" {
			backdropDest := filepath.Join(showDir, "backdrop.jpg")
			if _, err := os.Stat(backdropDest); os.IsNotExist(err) {
				_ = tmdbClient.DownloadImage(backdropPath, backdropDest)
			}
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
			log.Printf("[Worker #%d] Warning: failed to save series %s: %v", workerID, seriesObj.ID, err)
		}

		// Persist Season row
		seasonObj := &db.Season{
			ID:           fmt.Sprintf("%s_s%02d", seriesObj.ID, parsed.Season),
			SeriesID:     seriesObj.ID,
			SeasonNumber: parsed.Season,
			Title:        fmt.Sprintf("Season %02d", parsed.Season),
		}
		if err := database.SaveSeason(seasonObj); err != nil {
			log.Printf("[Worker #%d] Warning: failed to save season %s: %v", workerID, seasonObj.ID, err)
		}

		if endEp > startEp {
			// Multi-episode file: Jellyfin native multi-episode convention "Show S01E01-E03.strm"
			baseTitle := fmt.Sprintf("%s S%02dE%02d-E%02d", parsed.CleanTitle, parsed.Season, startEp, endEp)
			strmFileName, epStrmPath, err := jfWriter.WriteVersionedSTRM(relDir, baseTitle, parsed.Resolution, opaqueID)
			if err != nil {
				log.Printf("[Worker #%d] Error writing multi-ep .strm: %v", workerID, err)
				job.Status = "failed"
				job.Error = err.Error()
				_ = database.SaveJob(job)
				return
			}
			primaryStrmPath = epStrmPath

			var epNFOs []metadata.EpisodeDetailsNFO
			for epNum := startEp; epNum <= endEp; epNum++ {
				epTitle := fmt.Sprintf("Episode %d", epNum)
				epPlot := plot
				epRating := rating
				epStillPath := ""
				epAirDate := ""
				epTMDBID := 0

				if tmdbID > 0 {
					if epMeta, err := tmdbClient.GetEpisodeDetails(tmdbID, parsed.Season, epNum); err == nil && epMeta != nil {
						if epMeta.ID > 0 {
							epTMDBID = epMeta.ID
						}
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

				epUIDs := []metadata.UniqueID{}
				if epTMDBID > 0 {
					epUIDs = append(epUIDs, metadata.UniqueID{Type: "tmdb", Default: "true", Value: strconv.Itoa(epTMDBID)})
				}
				if tmdbID > 0 {
					epUIDs = append(epUIDs, metadata.UniqueID{Type: "tmdb_series", Value: strconv.Itoa(tmdbID)})
				}

				epNFOs = append(epNFOs, metadata.EpisodeDetailsNFO{
					Title:    epTitle,
					Season:   parsed.Season,
					Episode:  epNum,
					Plot:     epPlot,
					Aired:    epAirDate,
					Rating:   epRating,
					UniqueID: epUIDs,
				})

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
					TMDBEpisodeID: epTMDBID,
					Title:         epTitle,
					Overview:      epPlot,
					AirDate:       epAirDate,
					Rating:        epRating,
					StillPath:     epStillPath,
					StrmPath:      epStrmPath,
				}
				if err := database.SaveEpisode(epObj); err != nil {
					log.Printf("[Worker #%d] Warning: failed to save episode %s: %v", workerID, epObj.ID, err)
				}

				// Record media version
				isDefault := (strmFileName == baseTitle+".strm")
				ver := &db.MediaVersion{
					ID:          fmt.Sprintf("ver_%s_%d", opaqueID, epNum),
					MediaItemID: opaqueID,
					MediaType:   "episode",
					ParentID:    epObj.ID,
					Edition:     parsed.Resolution,
					FileSize:    doc.Size,
					StrmPath:    epStrmPath,
					IsDefault:   isDefault,
				}
				_ = database.SaveMediaVersion(ver)
			}

			// Write multi-episode NFO matching the STRM file
			multiNfoContent := metadata.GenerateMultiEpisodeNFO(epNFOs)
			multiNfoPath := filepath.Join(seasonDir, fmt.Sprintf("%s.nfo", strings.TrimSuffix(strmFileName, ".strm")))
			if _, err := os.Stat(multiNfoPath); os.IsNotExist(err) {
				if err := os.WriteFile(multiNfoPath, []byte(multiNfoContent), 0644); err != nil {
					log.Printf("[Worker #%d] Warning: failed to write multi-episode NFO: %v", workerID, err)
				}
			}

		} else {
			// Single episode
			baseTitle := fmt.Sprintf("%s S%02dE%02d", parsed.CleanTitle, parsed.Season, startEp)
			strmFileName, epStrmPath, err := jfWriter.WriteVersionedSTRM(relDir, baseTitle, parsed.Resolution, opaqueID)
			if err != nil {
				log.Printf("[Worker #%d] Error writing episode .strm: %v", workerID, err)
				job.Status = "failed"
				job.Error = err.Error()
				_ = database.SaveJob(job)
				return
			}
			primaryStrmPath = epStrmPath

			epTitle := fmt.Sprintf("Episode %d", startEp)
			epPlot := plot
			epRating := rating
			epStillPath := ""
			epAirDate := ""
			epTMDBID := 0

			if tmdbID > 0 {
				if epMeta, err := tmdbClient.GetEpisodeDetails(tmdbID, parsed.Season, startEp); err == nil && epMeta != nil {
					if epMeta.ID > 0 {
						epTMDBID = epMeta.ID
					}
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

			epNfoPath := filepath.Join(seasonDir, fmt.Sprintf("%s.nfo", strings.TrimSuffix(strmFileName, ".strm")))
			if _, err := os.Stat(epNfoPath); os.IsNotExist(err) {
				epNfoContent := metadata.GenerateRichEpisodeNFO(epTitle, parsed.Season, startEp, epPlot, epAirDate, epRating, epTMDBID, tmdbID, "")
				if err := os.WriteFile(epNfoPath, []byte(epNfoContent), 0644); err != nil {
					log.Printf("[Worker #%d] Warning: failed to write episode NFO: %v", workerID, err)
				}
			}
			if epStillPath != "" {
				thumbDest := filepath.Join(seasonDir, fmt.Sprintf("%s S%02dE%02d-thumb.jpg", parsed.CleanTitle, parsed.Season, startEp))
				if _, err := os.Stat(thumbDest); os.IsNotExist(err) {
					_ = tmdbClient.DownloadImage(epStillPath, thumbDest)
				}
			}

			epObj := &db.Episode{
				ID:            fmt.Sprintf("ep_%s_%d", opaqueID, startEp),
				SeriesID:      seriesObj.ID,
				SeasonID:      seasonObj.ID,
				SeasonNumber:  parsed.Season,
				EpisodeNumber: startEp,
				TMDBEpisodeID: epTMDBID,
				Title:         epTitle,
				Overview:      epPlot,
				AirDate:       epAirDate,
				Rating:        epRating,
				StillPath:     epStillPath,
				StrmPath:      epStrmPath,
			}
			if err := database.SaveEpisode(epObj); err != nil {
				log.Printf("[Worker #%d] Warning: failed to save episode %s: %v", workerID, epObj.ID, err)
			}

			// Record media version
			isDefault := (strmFileName == baseTitle+".strm")
			ver := &db.MediaVersion{
				ID:          fmt.Sprintf("ver_%s", opaqueID),
				MediaItemID: opaqueID,
				MediaType:   "episode",
				ParentID:    epObj.ID,
				Edition:     parsed.Resolution,
				FileSize:    doc.Size,
				StrmPath:    epStrmPath,
				IsDefault:   isDefault,
			}
			_ = database.SaveMediaVersion(ver)
		}
	} else {
		// Movie
		baseName := parsed.CleanTitle
		if parsed.Year > 0 {
			baseName = fmt.Sprintf("%s (%d)", parsed.CleanTitle, parsed.Year)
		}
		relDir := filepath.Join("Movies", baseName)
		movieDir := filepath.Join(cfg.JellyfinMedia, relDir)
		_ = os.MkdirAll(movieDir, 0755)

		strmFileName, fullStrmPath, err := jfWriter.WriteVersionedSTRM(relDir, baseName, parsed.Resolution, opaqueID)
		if err != nil {
			log.Printf("[Worker #%d] Error writing .strm: %v", workerID, err)
			job.Status = "failed"
			job.Error = err.Error()
			_ = database.SaveJob(job)
			return
		}
		primaryStrmPath = fullStrmPath

		// Movie NFO & Artwork (preserving existing metadata if already present)
		nfoPath := filepath.Join(movieDir, "movie.nfo")
		if _, err := os.Stat(nfoPath); os.IsNotExist(err) {
			nfoContent := metadata.GenerateRichMovieNFO(parsed.CleanTitle, plot, parsed.Year, rating, tmdbID, detailedMeta)
			if err := os.WriteFile(nfoPath, []byte(nfoContent), 0644); err != nil {
				log.Printf("[Worker #%d] Warning: failed to write movie NFO: %v", workerID, err)
			}
		}

		if posterPath != "" {
			posterDest := filepath.Join(movieDir, "poster.jpg")
			if _, err := os.Stat(posterDest); os.IsNotExist(err) {
				_ = tmdbClient.DownloadImage(posterPath, posterDest)
			}
		}
		if backdropPath != "" {
			backdropDest := filepath.Join(movieDir, "backdrop.jpg")
			if _, err := os.Stat(backdropDest); os.IsNotExist(err) {
				_ = tmdbClient.DownloadImage(backdropPath, backdropDest)
			}
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
			log.Printf("[Worker #%d] Warning: failed to save movie %s: %v", workerID, movieObj.ID, err)
		}

		// Record media version
		isDefault := (strmFileName == baseName+".strm")
		ver := &db.MediaVersion{
			ID:          fmt.Sprintf("ver_%s", opaqueID),
			MediaItemID: opaqueID,
			MediaType:   "movie",
			ParentID:    movieObj.ID,
			Edition:     parsed.Resolution,
			FileSize:    doc.Size,
			StrmPath:    fullStrmPath,
			IsDefault:   isDefault,
		}
		_ = database.SaveMediaVersion(ver)
	}

	// 8. Persist MediaItem lookup index with actual verified primary .strm path
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
		StrmPath:     primaryStrmPath,
	}
	if err := database.SaveMediaItem(item); err != nil {
		log.Printf("[Worker #%d] FATAL: Error saving media item %s to database: %v", workerID, opaqueID, err)
		job.Status = "failed"
		job.Error = fmt.Sprintf("database error: %v", err)
		_ = database.SaveJob(job)
		return
	}

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
		workerID, parsed.CleanTitle, opaqueID, primaryStrmPath)

	// 11. Enqueue debounced Jellyfin refresh
	select {
	case refreshNotify <- struct{}{}:
	default:
	}
}
