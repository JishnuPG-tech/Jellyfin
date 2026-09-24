package streamer

import (
	"context"
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"log"
	"math/rand"
	"net/http"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"time"

	"apex/internal/config"
	"apex/internal/db"
)

const ChunkSize = 1024 * 1024 // 1 MB aligned to Telegram MTProto boundaries

type TelegramChunkFetcher interface {
	FetchChunk(ctx context.Context, item *db.MediaItem, offset int64, limit int) ([]byte, error)
}

type Gateway struct {
	cfg       *config.Config
	database  *db.Database
	cache     *LRUCache
	fetcher   TelegramChunkFetcher
	semaphore chan struct{}
	activeMu  sync.Mutex
	activeMap map[string]context.CancelFunc
}

func NewGateway(cfg *config.Config, database *db.Database, cache *LRUCache, fetcher TelegramChunkFetcher) *Gateway {
	maxStreams := cfg.MaxStreams
	if maxStreams <= 0 {
		maxStreams = 2
	}

	return &Gateway{
		cfg:       cfg,
		database:  database,
		cache:     cache,
		fetcher:   fetcher,
		semaphore: make(chan struct{}, maxStreams),
		activeMap: make(map[string]context.CancelFunc),
	}
}

// GenerateVLCToken generates a signed HMAC token: <mediaID>.<expiry_unix>.<hmac_hex>
func GenerateVLCToken(mediaID string, expiry time.Time, secretKey string) string {
	exp := expiry.Unix()
	msg := fmt.Sprintf("%s:%d", mediaID, exp)
	mac := hmac.New(sha256.New, []byte(secretKey))
	mac.Write([]byte(msg))
	sig := hex.EncodeToString(mac.Sum(nil))
	return fmt.Sprintf("%s.%d.%s", mediaID, exp, sig)
}

// ValidateVLCToken verifies HMAC signature and expiration
func ValidateVLCToken(token, secretKey string) (string, bool) {
	if secretKey == "" {
		return "", false
	}
	parts := strings.Split(token, ".")
	if len(parts) != 3 {
		return "", false
	}
	mediaID := parts[0]
	expUnix, err := strconv.ParseInt(parts[1], 10, 64)
	if err != nil || time.Now().Unix() > expUnix {
		return "", false
	}

	msg := fmt.Sprintf("%s:%d", mediaID, expUnix)
	mac := hmac.New(sha256.New, []byte(secretKey))
	mac.Write([]byte(msg))
	expectedSig := hex.EncodeToString(mac.Sum(nil))

	if !hmac.Equal([]byte(parts[2]), []byte(expectedSig)) {
		return "", false
	}
	return mediaID, true
}

func (g *Gateway) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	path := strings.Trim(r.URL.Path, "/")
	parts := strings.Split(path, "/")
	if len(parts) < 2 {
		http.Error(w, "invalid stream path", http.StatusBadRequest)
		return
	}

	prefix := parts[0]
	target := parts[len(parts)-1]

	var mediaID string

	// 1. Authorization check
	if prefix == "vlc" {
		// Strip extension if present: token.mp4 -> token
		cleanToken := target
		if dotIdx := strings.LastIndex(cleanToken, "."); dotIdx > 0 {
			cleanToken = cleanToken[:dotIdx]
		}

		validID, ok := ValidateVLCToken(cleanToken, g.cfg.ApexSecretKey)
		if !ok {
			http.Error(w, "invalid or expired stream token", http.StatusUnauthorized)
			return
		}
		mediaID = validID
	} else {
		// Standard /stream/{id}
		rawMediaID := target
		if dotIdx := strings.LastIndex(rawMediaID, "."); dotIdx > 0 {
			mediaID = rawMediaID[:dotIdx]
		} else {
			mediaID = rawMediaID
		}

		// Distinguish internal requests from Jellyfin server vs external ingress
		clientIP := r.RemoteAddr
		isLoopback := strings.HasPrefix(clientIP, "127.0.0.1") ||
			strings.HasPrefix(clientIP, "[::1]") ||
			strings.HasPrefix(clientIP, "::1") ||
			strings.HasPrefix(clientIP, "localhost")

		hasProxyHeaders := r.Header.Get("X-Forwarded-For") != "" ||
			r.Header.Get("X-Real-IP") != ""

		isInternal := isLoopback && !hasProxyHeaders

		if !isInternal {
			// External request: MUST provide a valid HMAC token
			token := r.URL.Query().Get("token")
			if token == "" {
				authHeader := r.Header.Get("Authorization")
				if strings.HasPrefix(authHeader, "Bearer ") {
					token = strings.TrimPrefix(authHeader, "Bearer ")
				}
			}

			if token == "" {
				http.Error(w, "missing stream authentication token", http.StatusUnauthorized)
				return
			}

			validID, ok := ValidateVLCToken(token, g.cfg.ApexSecretKey)
			if !ok || validID != mediaID {
				http.Error(w, "invalid or expired stream token", http.StatusUnauthorized)
				return
			}
		}
	}

	// 2. Lookup media record
	var item *db.MediaItem
	var err error

	if prefix == "stream" && len(parts) >= 3 {
		chatID, err1 := strconv.ParseInt(parts[1], 10, 64)
		msgID, err2 := strconv.Atoi(parts[2])
		if err1 == nil && err2 == nil {
			item, err = g.database.GetMediaItemByMessageID(chatID, msgID)
			if item != nil {
				mediaID = item.ID // use internal ID for caching and session management
			}
		}
	}

	if item == nil {
		item, err = g.database.GetMediaItem(mediaID)
	}

	if err != nil || item == nil {
		http.Error(w, "media item not found", http.StatusNotFound)
		return
	}

	// 3. Acquire concurrent stream semaphore
	select {
	case g.semaphore <- struct{}{}:
		defer func() { <-g.semaphore }()
	default:
		w.Header().Set("Retry-After", "5")
		http.Error(w, "maximum concurrent streams reached", http.StatusServiceUnavailable)
		return
	}

	totalSize := item.FileSize
	rangeHeader := r.Header.Get("Range")

	contentType := item.MimeType
	if contentType == "" || contentType == "application/octet-stream" {
		contentType = "video/mp4"
	}

	w.Header().Set("Accept-Ranges", "bytes")
	w.Header().Set("Content-Type", contentType)
	w.Header().Set("Content-Disposition", fmt.Sprintf("inline; filename=\"%s\"", item.CleanTitle+".mp4"))
	w.Header().Set("Connection", "keep-alive")
	w.Header().Set("Access-Control-Allow-Origin", "*")

	// 4. RFC 7233 HTTP Range parsing
	start := int64(0)
	end := totalSize - 1

	if rangeHeader != "" {
		// Reject multi-range requests
		if strings.Contains(rangeHeader, ",") {
			w.Header().Set("Content-Range", fmt.Sprintf("bytes */%d", totalSize))
			http.Error(w, "multi-range requests not supported", http.StatusRequestedRangeNotSatisfiable)
			return
		}

		if !strings.HasPrefix(rangeHeader, "bytes=") {
			w.Header().Set("Content-Range", fmt.Sprintf("bytes */%d", totalSize))
			http.Error(w, "invalid range header", http.StatusRequestedRangeNotSatisfiable)
			return
		}

		spec := strings.TrimPrefix(rangeHeader, "bytes=")
		rangeParts := strings.Split(spec, "-")

		if len(rangeParts) != 2 {
			w.Header().Set("Content-Range", fmt.Sprintf("bytes */%d", totalSize))
			http.Error(w, "malformed range specifier", http.StatusRequestedRangeNotSatisfiable)
			return
		}

		if rangeParts[0] == "" {
			// Suffix range: bytes=-500 (last 500 bytes)
			suffix, err := strconv.ParseInt(rangeParts[1], 10, 64)
			if err != nil || suffix <= 0 {
				w.Header().Set("Content-Range", fmt.Sprintf("bytes */%d", totalSize))
				http.Error(w, "invalid suffix range", http.StatusRequestedRangeNotSatisfiable)
				return
			}
			if suffix > totalSize {
				suffix = totalSize
			}
			start = totalSize - suffix
			end = totalSize - 1
		} else {
			// bytes=500- or bytes=500-1000
			s, err := strconv.ParseInt(rangeParts[0], 10, 64)
			if err != nil || s < 0 {
				w.Header().Set("Content-Range", fmt.Sprintf("bytes */%d", totalSize))
				http.Error(w, "invalid start range", http.StatusRequestedRangeNotSatisfiable)
				return
			}
			start = s

			if rangeParts[1] != "" {
				e, err := strconv.ParseInt(rangeParts[1], 10, 64)
				if err != nil || e < start {
					w.Header().Set("Content-Range", fmt.Sprintf("bytes */%d", totalSize))
					http.Error(w, "invalid end range", http.StatusRequestedRangeNotSatisfiable)
					return
				}
				if e < totalSize {
					end = e
				}
			}
		}

		if start > end || start >= totalSize {
			w.Header().Set("Content-Range", fmt.Sprintf("bytes */%d", totalSize))
			http.Error(w, "requested range not satisfiable", http.StatusRequestedRangeNotSatisfiable)
			return
		}

		w.Header().Set("Content-Range", fmt.Sprintf("bytes %d-%d/%d", start, end, totalSize))
		w.Header().Set("Content-Length", strconv.FormatInt(end-start+1, 10))
		log.Printf("[Streamer] %s request for %s (Range: bytes %d-%d/%d, Size: %d)", r.Method, mediaID, start, end, totalSize, end-start+1)
		w.WriteHeader(http.StatusPartialContent)
	} else {
		w.Header().Set("Content-Length", strconv.FormatInt(totalSize, 10))
		log.Printf("[Streamer] %s request for %s (Full file, Size: %d)", r.Method, mediaID, totalSize)
		w.WriteHeader(http.StatusOK)
	}

	if r.Method == http.MethodHead {
		return
	}

	// 5. Unique stream session setup
	streamCtx, streamCancel := context.WithCancel(r.Context())
	defer streamCancel()

	sessionID := fmt.Sprintf("%s:%d:%d", mediaID, time.Now().UnixNano(), rand.Int63())

	g.activeMu.Lock()
	g.activeMap[sessionID] = streamCancel
	g.activeMu.Unlock()

	defer func() {
		g.activeMu.Lock()
		delete(g.activeMap, sessionID)
		g.activeMu.Unlock()
	}()

	prefetchWindow := g.cfg.PrefetchMB
	if prefetchWindow <= 0 {
		prefetchWindow = 16
	}

	// 6. Dedicated bounded prefetch worker per stream session
	prefetchCh := make(chan int64, 1)

	go func() {
		for {
			select {
			case <-streamCtx.Done():
				return
			case anchorChunk, ok := <-prefetchCh:
				if !ok {
					return
				}
				for i := 1; i <= prefetchWindow; i++ {
					select {
					case <-streamCtx.Done():
						return
					default:
					}

					nextChunk := anchorChunk + int64(i)
					nextBaseOffset := nextChunk * ChunkSize
					if nextBaseOffset >= totalSize {
						break
					}
					nextKey := fmt.Sprintf("%s:%d", mediaID, nextChunk)

					// Skip if already in cache
					if _, hit := g.cache.Get(nextKey); hit {
						continue
					}

					// Fetch subsequent chunk in background
					_, _ = g.cache.FetchCoalesced(nextKey, func() ([]byte, error) {
						return g.fetcher.FetchChunk(streamCtx, item, nextBaseOffset, ChunkSize)
					})
				}
			}
		}
	}()

	// 7. Stream chunk delivery with request coalescing & bounded prefetch
	currentOffset := start
	for currentOffset <= end {
		select {
		case <-r.Context().Done():
			log.Printf("[Streamer] Client disconnected or seeked for %s at offset %d", mediaID, currentOffset)
			return
		default:
		}

		chunkIndex := currentOffset / ChunkSize
		chunkBaseOffset := chunkIndex * ChunkSize
		cacheKey := fmt.Sprintf("%s:%d", mediaID, chunkIndex)

		// Fetch current chunk via coalescing cache
		fetchStart := time.Now()
		data, err := g.cache.FetchCoalesced(cacheKey, func() ([]byte, error) {
			return g.fetcher.FetchChunk(streamCtx, item, chunkBaseOffset, ChunkSize)
		})
		if err != nil {
			log.Printf("[Streamer] Failed to retrieve chunk %d for %s (took %v): %v", chunkIndex, mediaID, time.Since(fetchStart), err)
			return
		}
		// log.Printf("[Streamer] Served chunk %d for %s (fetched in %v)", chunkIndex, mediaID, time.Since(fetchStart))

		// Trigger bounded prefetch worker for upcoming chunks
		select {
		case prefetchCh <- chunkIndex:
		default:
		}

		// Slice requested byte range from the 1 MB block
		sliceStart := currentOffset - chunkBaseOffset
		sliceEnd := int64(len(data))
		remainingNeeded := (end - currentOffset) + 1

		if sliceStart+remainingNeeded < sliceEnd {
			sliceEnd = sliceStart + remainingNeeded
		}

		if sliceStart < int64(len(data)) {
			n, err := w.Write(data[sliceStart:sliceEnd])
			if err != nil {
				return
			}
			if flusher, ok := w.(http.Flusher); ok {
				flusher.Flush()
			}
			currentOffset += int64(n)
		} else {
			break
		}
	}
}

func (g *Gateway) GetDirectStreamURL(mediaID string) string {
	ext := filepath.Ext(mediaID)
	token := GenerateVLCToken(mediaID, time.Now().Add(24*time.Hour), g.cfg.ApexSecretKey)
	return fmt.Sprintf("/vlc/%s%s", token, ext)
}
