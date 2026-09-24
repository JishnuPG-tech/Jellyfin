package jellyfin

import (
	"context"
	"encoding/json"
	"fmt"
	"log"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"time"
)

type Writer struct {
	baseDir string
}

func NewWriter(baseDir string) *Writer {
	return &Writer{baseDir: baseDir}
}

func (w *Writer) WriteSTRM(relPath, apxID string) (string, error) {
	fullPath := filepath.Join(w.baseDir, relPath)
	if err := os.MkdirAll(filepath.Dir(fullPath), 0755); err != nil {
		return "", err
	}

	streamURL := fmt.Sprintf("http://127.0.0.1:8084/stream/%s", apxID)
	err := os.WriteFile(fullPath, []byte(streamURL), 0644)
	if err != nil {
		return "", err
	}
	return fullPath, nil
}

type Client struct {
	mu         sync.RWMutex
	baseURL    string
	apiKey     string
	httpClient *http.Client
}

func NewClient(baseURL, apiKey string) *Client {
	return &Client{
		baseURL: baseURL,
		apiKey:  apiKey,
		httpClient: &http.Client{
			Timeout: 10 * time.Second,
		},
	}
}

func (c *Client) SetAPIKey(key string) {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.apiKey = key
}

func (c *Client) GetAPIKey() string {
	c.mu.RLock()
	defer c.mu.RUnlock()
	return c.apiKey
}

type VirtualFolder struct {
	Name      string   `json:"Name"`
	Locations []string `json:"Locations"`
}

// VerifyIntegration checks: (1) server reachability, (2) API key validity, (3) Movies library, (4) Shows library
func (c *Client) VerifyIntegration(ctx context.Context) (reachable bool, authValid bool, hasMovies bool, hasShows bool, err error) {
	// 1. Check reachability
	publicURL := fmt.Sprintf("%s/System/Info/Public", c.baseURL)
	reqPublic, err := http.NewRequestWithContext(ctx, http.MethodGet, publicURL, nil)
	if err != nil {
		return false, false, false, false, err
	}
	respPublic, err := c.httpClient.Do(reqPublic)
	if err != nil {
		return false, false, false, false, fmt.Errorf("jellyfin server unreachable: %w", err)
	}
	respPublic.Body.Close()
	reachable = true

	apiKey := c.GetAPIKey()
	if apiKey == "" {
		return reachable, false, false, false, fmt.Errorf("APEX_JELLYFIN_API_KEY is not set")
	}

	// 2. Check API key validity & virtual folders
	foldersURL := fmt.Sprintf("%s/Library/VirtualFolders", c.baseURL)
	reqFolders, err := http.NewRequestWithContext(ctx, http.MethodGet, foldersURL, nil)
	if err != nil {
		return reachable, false, false, false, err
	}
	reqFolders.Header.Set("X-Emby-Token", apiKey)
	reqFolders.Header.Set("Authorization", fmt.Sprintf("MediaBrowser Token=\"%s\"", apiKey))

	respFolders, err := c.httpClient.Do(reqFolders)
	if err != nil {
		return reachable, false, false, false, fmt.Errorf("error querying libraries: %w", err)
	}
	defer respFolders.Body.Close()

	if respFolders.StatusCode == http.StatusUnauthorized || respFolders.StatusCode == http.StatusForbidden {
		return reachable, false, false, false, fmt.Errorf("APEX_JELLYFIN_API_KEY is unauthorized or expired (HTTP %d)", respFolders.StatusCode)
	}
	if respFolders.StatusCode != http.StatusOK {
		return reachable, false, false, false, fmt.Errorf("unexpected status %d from library query", respFolders.StatusCode)
	}
	authValid = true

	var folders []VirtualFolder
	if err := json.NewDecoder(respFolders.Body).Decode(&folders); err != nil {
		return reachable, authValid, false, false, fmt.Errorf("error decoding libraries: %w", err)
	}

	for _, f := range folders {
		if strings.EqualFold(f.Name, "Movies") {
			hasMovies = true
		}
		if strings.EqualFold(f.Name, "Shows") || strings.EqualFold(f.Name, "TV Shows") || strings.EqualFold(f.Name, "Series") {
			hasShows = true
		}
	}

	return reachable, authValid, hasMovies, hasShows, nil
}

func (c *Client) EnsureDefaultLibraries() {
	apiKey := c.GetAPIKey()
	if apiKey == "" {
		log.Println("[Jellyfin] Notice: APEX_JELLYFIN_API_KEY is not configured. Library auto-provisioning skipped.")
		return
	}

	u := fmt.Sprintf("%s/Library/VirtualFolders", c.baseURL)
	req, err := http.NewRequest(http.MethodGet, u, nil)
	if err != nil {
		return
	}
	req.Header.Set("X-Emby-Token", apiKey)
	req.Header.Set("Authorization", fmt.Sprintf("MediaBrowser Token=\"%s\"", apiKey))

	resp, err := c.httpClient.Do(req)
	if err != nil {
		log.Printf("[Jellyfin] Failed to query virtual folders: %v", err)
		return
	}
	defer resp.Body.Close()

	if resp.StatusCode == http.StatusUnauthorized || resp.StatusCode == http.StatusForbidden {
		log.Printf("[Jellyfin] Warning: Configured APEX_JELLYFIN_API_KEY was rejected by Jellyfin (HTTP %d).", resp.StatusCode)
		return
	}
	if resp.StatusCode != http.StatusOK {
		log.Printf("[Jellyfin] VirtualFolders query returned HTTP %d", resp.StatusCode)
		return
	}

	var folders []VirtualFolder
	if err := json.NewDecoder(resp.Body).Decode(&folders); err != nil {
		return
	}

	hasMovies := false
	hasShows := false
	for _, f := range folders {
		if strings.EqualFold(f.Name, "Movies") {
			hasMovies = true
		}
		if strings.EqualFold(f.Name, "Shows") || strings.EqualFold(f.Name, "TV Shows") || strings.EqualFold(f.Name, "Series") {
			hasShows = true
		}
	}

	if !hasMovies {
		c.addVirtualFolder("Movies", "movies", "/data/jellyfin/media/Movies")
	}
	if !hasShows {
		c.addVirtualFolder("Shows", "tvshows", "/data/jellyfin/media/Shows")
	}
}

func (c *Client) addVirtualFolder(name, collectionType, path string) {
	apiKey := c.GetAPIKey()
	if apiKey == "" {
		return
	}

	u := fmt.Sprintf("%s/Library/VirtualFolders?name=%s&collectionType=%s&paths=%s&refreshLibrary=true",
		c.baseURL, url.QueryEscape(name), url.QueryEscape(collectionType), url.QueryEscape(path))

	req, err := http.NewRequest(http.MethodPost, u, nil)
	if err != nil {
		return
	}
	req.Header.Set("X-Emby-Token", apiKey)
	req.Header.Set("Authorization", fmt.Sprintf("MediaBrowser Token=\"%s\"", apiKey))

	resp, err := c.httpClient.Do(req)
	if err != nil {
		log.Printf("[Jellyfin] Error creating library '%s': %v", name, err)
		return
	}
	defer resp.Body.Close()

	if resp.StatusCode >= 200 && resp.StatusCode < 300 {
		log.Printf("[Jellyfin] Auto-created '%s' library pointing to %s (Status: %d)", name, path, resp.StatusCode)
	} else {
		log.Printf("[Jellyfin] Failed creating library '%s' (Status: %d)", name, resp.StatusCode)
	}
}

func (c *Client) RefreshLibrary() error {
	apiKey := c.GetAPIKey()
	if apiKey == "" {
		return fmt.Errorf("APEX_JELLYFIN_API_KEY is not configured")
	}

	// Ensure default libraries exist
	c.EnsureDefaultLibraries()

	u := fmt.Sprintf("%s/Library/Refresh", c.baseURL)
	req, err := http.NewRequest(http.MethodPost, u, nil)
	if err != nil {
		return err
	}
	req.Header.Set("X-Emby-Token", apiKey)
	req.Header.Set("Authorization", fmt.Sprintf("MediaBrowser Token=\"%s\"", apiKey))

	resp, err := c.httpClient.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()

	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return fmt.Errorf("jellyfin refresh returned HTTP %d", resp.StatusCode)
	}
	return nil
}
