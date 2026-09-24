package streamer

import (
	"context"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"sync"
	"testing"
	"time"

	"apex/internal/config"
	"apex/internal/db"
)

type mockFetcher struct {
	data []byte
}

func (m *mockFetcher) FetchChunk(ctx context.Context, item *db.MediaItem, offset int64, limit int) ([]byte, error) {
	if offset >= int64(len(m.data)) {
		return []byte{}, nil
	}
	end := offset + int64(limit)
	if end > int64(len(m.data)) {
		end = int64(len(m.data))
	}
	return m.data[offset:end], nil
}

func TestHMACToken(t *testing.T) {
	secret := "test_secret_key_32_bytes_long_12345"
	mediaID := "apx_test123"

	// 1. Valid token
	validToken := GenerateVLCToken(mediaID, time.Now().Add(1*time.Hour), secret)
	id, ok := ValidateVLCToken(validToken, secret)
	if !ok || id != mediaID {
		t.Fatalf("ValidateVLCToken failed for valid token: got %s, %v", id, ok)
	}

	// 2. Tampered token
	tampered := validToken + "bad"
	if _, ok := ValidateVLCToken(tampered, secret); ok {
		t.Errorf("ValidateVLCToken should fail for tampered token")
	}

	// 3. Expired token
	expiredToken := GenerateVLCToken(mediaID, time.Now().Add(-1*time.Minute), secret)
	if _, ok := ValidateVLCToken(expiredToken, secret); ok {
		t.Errorf("ValidateVLCToken should fail for expired token")
	}

	// 4. Invalid secret key
	if _, ok := ValidateVLCToken(validToken, "wrong_secret_key"); ok {
		t.Errorf("ValidateVLCToken should fail for wrong secret key")
	}
}

func TestStreamGatewayAuthAndRanges(t *testing.T) {
	secret := "super_secret_apex_key_32_bytes"
	tmpDB := t.TempDir() + "/test_apex.db"
	tmpCache := t.TempDir() + "/cache"

	database, err := db.Open(tmpDB)
	if err != nil {
		t.Fatalf("Failed to open test database: %v", err)
	}
	defer database.Close()

	// Seed test media item
	mediaID := "apx_testmedia"
	testPayload := make([]byte, 2*1024*1024) // 2 MB
	for i := range testPayload {
		testPayload[i] = byte(i % 256)
	}

	item := &db.MediaItem{
		ID:           mediaID,
		SourceChatID: 1001,
		MessageID:    42,
		FileID:       "test_fid",
		FileUniqueID: "test_fuid",
		FileRef:      []byte("valid_test_blob_ref"),
		AccessHash:   987654321,
		FileSize:     int64(len(testPayload)),
		MimeType:     "video/mp4",
		CleanTitle:   "Test Movie",
		MediaType:    "movie",
		StrmPath:     "/tmp/test.strm",
	}
	if err := database.SaveMediaItem(item); err != nil {
		t.Fatalf("Failed to save media item: %v", err)
	}

	cfg := &config.Config{
		ApexSecretKey: secret,
		MaxStreams:    2,
		PrefetchMB:    1,
	}

	cache := NewLRUCache(16, 64, tmpCache)
	fetcher := &mockFetcher{data: testPayload}
	gateway := NewGateway(cfg, database, cache, fetcher)

	// 1. External request without token should be REJECTED (401)
	reqExternalNoToken := httptest.NewRequest("GET", "/stream/"+mediaID, nil)
	reqExternalNoToken.RemoteAddr = "192.168.1.100:54321"
	reqExternalNoToken.Header.Set("X-Forwarded-For", "203.0.113.195")
	rr := httptest.NewRecorder()
	gateway.ServeHTTP(rr, reqExternalNoToken)
	if rr.Code != http.StatusUnauthorized {
		t.Errorf("External request without token should return 401, got %d", rr.Code)
	}

	// 2. External request with valid token should SUCCEED (200 or 206)
	token := GenerateVLCToken(mediaID, time.Now().Add(1*time.Hour), secret)
	reqExternalWithToken := httptest.NewRequest("GET", fmt.Sprintf("/stream/%s?token=%s", mediaID, token), nil)
	reqExternalWithToken.RemoteAddr = "192.168.1.100:54321"
	reqExternalWithToken.Header.Set("X-Forwarded-For", "203.0.113.195")
	rr = httptest.NewRecorder()
	gateway.ServeHTTP(rr, reqExternalWithToken)
	if rr.Code != http.StatusOK {
		t.Errorf("External request with valid token should return 200, got %d", rr.Code)
	}

	// 3. Internal request (loopback, no proxy header) should SUCCEED without token
	reqInternal := httptest.NewRequest("GET", "/stream/"+mediaID, nil)
	reqInternal.RemoteAddr = "127.0.0.1:45678"
	rr = httptest.NewRecorder()
	gateway.ServeHTTP(rr, reqInternal)
	if rr.Code != http.StatusOK {
		t.Errorf("Internal request should return 200, got %d", rr.Code)
	}

	// 4. RFC 7233 Range Request: bytes=0-1023
	reqRange := httptest.NewRequest("GET", "/stream/"+mediaID, nil)
	reqRange.RemoteAddr = "127.0.0.1:45678"
	reqRange.Header.Set("Range", "bytes=0-1023")
	rr = httptest.NewRecorder()
	gateway.ServeHTTP(rr, reqRange)
	if rr.Code != http.StatusPartialContent {
		t.Errorf("Range request should return 206, got %d", rr.Code)
	}
	if len(rr.Body.Bytes()) != 1024 {
		t.Errorf("Range 0-1023 body length = %d, want 1024", len(rr.Body.Bytes()))
	}
	if rr.Header().Get("Content-Range") != fmt.Sprintf("bytes 0-1023/%d", len(testPayload)) {
		t.Errorf("Content-Range header = %s, want bytes 0-1023/%d", rr.Header().Get("Content-Range"), len(testPayload))
	}

	// 5. Multi-range request rejection (416)
	reqMultiRange := httptest.NewRequest("GET", "/stream/"+mediaID, nil)
	reqMultiRange.RemoteAddr = "127.0.0.1:45678"
	reqMultiRange.Header.Set("Range", "bytes=0-50, 100-150")
	rr = httptest.NewRecorder()
	gateway.ServeHTTP(rr, reqMultiRange)
	if rr.Code != http.StatusRequestedRangeNotSatisfiable {
		t.Errorf("Multi-range request should return 416, got %d", rr.Code)
	}

	// 6. VLC endpoint with token
	reqVLC := httptest.NewRequest("GET", "/vlc/"+token+".mp4", nil)
	rr = httptest.NewRecorder()
	gateway.ServeHTTP(rr, reqVLC)
	if rr.Code != http.StatusOK {
		t.Errorf("VLC endpoint request should return 200, got %d", rr.Code)
	}
}


// blockingFetcher lets concurrent requests enter the Telegram-fetch boundary
// before releasing them, proving the HTTP layer does not reject legitimate
// parallel Range requests from Jellyfin/FFmpeg.
type blockingFetcher struct {
	data    []byte
	entered chan struct{}
	release chan struct{}
}

func (f *blockingFetcher) FetchChunk(ctx context.Context, item *db.MediaItem, offset int64, limit int) ([]byte, error) {
	select {
	case f.entered <- struct{}{}:
	case <-ctx.Done():
		return nil, ctx.Err()
	}
	select {
	case <-f.release:
	case <-ctx.Done():
		return nil, ctx.Err()
	}
	if offset >= int64(len(f.data)) {
		return []byte{}, nil
	}
	end := offset + int64(limit)
	if end > int64(len(f.data)) {
		end = int64(len(f.data))
	}
	return f.data[offset:end], nil
}

func TestPythonCompatibleConcurrentRanges(t *testing.T) {
	tmpDB := t.TempDir() + "/test_apex.db"
	tmpCache := t.TempDir() + "/cache"

	database, err := db.Open(tmpDB)
	if err != nil {
		t.Fatal(err)
	}
	defer database.Close()

	payload := make([]byte, 1024*1024)
	for i := range payload {
		payload[i] = byte(i % 251)
	}

	item := &db.MediaItem{
		ID: "apx_concurrent",
		SourceChatID: 1769298522,
		MessageID: 8,
		FileID: "test_fid",
		FileRef: []byte("ref"),
		AccessHash: 1,
		FileSize: int64(len(payload)),
		MimeType: "video/x-matroska",
		CleanTitle: "Concurrent Movie",
		MediaType: "movie",
		StrmPath: "/tmp/test.strm",
	}
	if err := database.SaveMediaItem(item); err != nil {
		t.Fatal(err)
	}

	fetcher := &blockingFetcher{
		data: payload,
		entered: make(chan struct{}, 3),
		release: make(chan struct{}),
	}
	gateway := NewGateway(
		&config.Config{ApexSecretKey: "test_secret_key_32_bytes_long_12345", MaxStreams: 1, PrefetchMB: 1},
		database,
		NewLRUCache(16, 64, tmpCache),
		fetcher,
	)

	results := make(chan int, 3)
	var wg sync.WaitGroup
	for i := 0; i < 3; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			req := httptest.NewRequest("GET", "/stream/1769298522/8/video.mp4", nil)
			req.RemoteAddr = "127.0.0.1:45678"
			req.Header.Set("Range", "bytes=0-1048575")
			rr := httptest.NewRecorder()
			gateway.ServeHTTP(rr, req)

			if rr.Header().Get("Content-Type") != "video/mp4" {
				t.Errorf("Content-Type = %q, want video/mp4", rr.Header().Get("Content-Type"))
			}
			if rr.Header().Get("Accept-Ranges") != "bytes" {
				t.Errorf("Accept-Ranges = %q, want bytes", rr.Header().Get("Accept-Ranges"))
			}
			if rr.Header().Get("Cache-Control") != "public, max-age=86400" {
				t.Errorf("Cache-Control = %q, want public, max-age=86400", rr.Header().Get("Cache-Control"))
			}
			if rr.Header().Get("Content-Disposition") == "" {
				t.Error("Content-Disposition missing")
			}
			if rr.Code != http.StatusPartialContent {
				t.Errorf("concurrent Range request returned %d, want 206", rr.Code)
			}
			results <- rr.Code
		}()
	}

	for i := 0; i < 3; i++ {
		select {
		case <-fetcher.entered:
		case <-time.After(2 * time.Second):
			t.Fatal("expected all three concurrent Range requests to reach the Telegram fetcher")
		}
	}
	close(fetcher.release)
	wg.Wait()
	close(results)

	for code := range results {
		if code != http.StatusPartialContent {
			t.Errorf("concurrent Range request returned %d, want 206", code)
		}
	}
}

func TestCacheAtomicWriteAndEviction(t *testing.T) {
	tmpDir, err := os.MkdirTemp("", "apex_cache_test_*")
	if err != nil {
		t.Fatalf("Failed to create temp dir: %v", err)
	}
	defer os.RemoveAll(tmpDir)

	cache := NewLRUCache(1, 4, tmpDir) // 1MB RAM, 4MB disk

	chunk1 := make([]byte, 512*1024)
	chunk1[0] = 0xAA
	chunk2 := make([]byte, 512*1024)
	chunk2[0] = 0xBB

	cache.Put("chunk1", chunk1)
	cache.Put("chunk2", chunk2)

	// Verify retrieval
	got1, ok1 := cache.Get("chunk1")
	if !ok1 || len(got1) != len(chunk1) || got1[0] != 0xAA {
		t.Errorf("Cache Get chunk1 failed: ok=%v", ok1)
	}

	got2, ok2 := cache.Get("chunk2")
	if !ok2 || len(got2) != len(chunk2) || got2[0] != 0xBB {
		t.Errorf("Cache Get chunk2 failed: ok=%v", ok2)
	}
}
