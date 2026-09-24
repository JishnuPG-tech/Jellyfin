package streamer

import (
	"context"
	"fmt"
	"log"
	"net/http"
	"strconv"
	"strings"

	"apex/internal/db"
)

const ChunkSize = 1024 * 1024 // 1 MB aligned to Telegram MTProto boundaries

type TelegramChunkFetcher interface {
	FetchChunk(ctx context.Context, item *db.MediaItem, offset int64, limit int) ([]byte, error)
}

type Gateway struct {
	database *db.Database
	cache    *LRUCache
	fetcher  TelegramChunkFetcher
}

func NewGateway(database *db.Database, cache *LRUCache, fetcher TelegramChunkFetcher) *Gateway {
	return &Gateway{
		database: database,
		cache:    cache,
		fetcher:  fetcher,
	}
}

func (g *Gateway) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	// Path format: /stream/{apx_id}
	parts := strings.Split(strings.Trim(r.URL.Path, "/"), "/")
	if len(parts) < 2 {
		http.Error(w, "invalid stream path", http.StatusBadRequest)
		return
	}
	rawMediaID := parts[len(parts)-1]
	mediaID := rawMediaID
	if dotIdx := strings.LastIndex(rawMediaID, "."); dotIdx > 0 {
		mediaID = rawMediaID[:dotIdx]
	}

	item, err := g.database.GetMediaItem(mediaID)
	if err != nil {
		item, err = g.database.GetMediaItem(rawMediaID)
		if err != nil {
			log.Printf("[Streamer] Media item not found for requested ID: %s (raw: %s)", mediaID, rawMediaID)
			http.Error(w, "media item not found", http.StatusNotFound)
			return
		}
	}

	totalSize := item.FileSize
	rangeHeader := r.Header.Get("Range")

	contentType := item.MimeType
	if contentType == "" || contentType == "application/octet-stream" {
		contentType = "video/mp4"
	}

	w.Header().Set("Accept-Ranges", "bytes")
	w.Header().Set("Content-Type", contentType)

	start := int64(0)
	end := totalSize - 1

	if rangeHeader != "" {
		if strings.HasPrefix(rangeHeader, "bytes=") {
			ranges := strings.Split(strings.TrimPrefix(rangeHeader, "bytes="), "-")
			if s, err := strconv.ParseInt(ranges[0], 10, 64); err == nil {
				start = s
			}
			if len(ranges) > 1 && ranges[1] != "" {
				if e, err := strconv.ParseInt(ranges[1], 10, 64); err == nil {
					end = e
				}
			}
		}

		if start > end || start >= totalSize {
			w.Header().Set("Content-Range", fmt.Sprintf("bytes */%d", totalSize))
			http.Error(w, "Requested Range Not Satisfiable", http.StatusRequestedRangeNotSatisfiable)
			return
		}

		w.Header().Set("Content-Range", fmt.Sprintf("bytes %d-%d/%d", start, end, totalSize))
		w.Header().Set("Content-Length", strconv.FormatInt(end-start+1, 10))
		w.WriteHeader(http.StatusPartialContent)
	} else {
		w.Header().Set("Content-Length", strconv.FormatInt(totalSize, 10))
		w.WriteHeader(http.StatusOK)
	}

	// Stream chunks in pipelined 1 MB blocks
	currentOffset := start
	for currentOffset <= end {
		select {
		case <-r.Context().Done():
			log.Printf("[Streamer] Client disconnected or seeked, cancelling playback for %s", mediaID)
			return
		default:
		}

		chunkIndex := currentOffset / ChunkSize
		chunkBaseOffset := chunkIndex * ChunkSize
		cacheKey := fmt.Sprintf("%s:%d", mediaID, chunkIndex)

		data, hit := g.cache.Get(cacheKey)
		if !hit {
			// Fetch from Telegram MTProto
			var err error
			data, err = g.fetcher.FetchChunk(r.Context(), item, chunkBaseOffset, ChunkSize)
			if err != nil {
				log.Printf("[Streamer] Failed to fetch chunk from Telegram: %v", err)
				return
			}
			g.cache.Put(cacheKey, data)
		}

		// Calculate slice of the chunk to send
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
