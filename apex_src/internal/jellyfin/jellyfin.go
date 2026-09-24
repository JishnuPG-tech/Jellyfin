package jellyfin

import (
	"encoding/json"
	"fmt"
	"log"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"strings"
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

func (c *Client) EnsureDefaultLibraries() {
	if c.apiKey == "" {
		return
	}

	// 1. Fetch current virtual folders from Jellyfin
	u := fmt.Sprintf("%s/Library/VirtualFolders", c.baseURL)
	req, err := http.NewRequest(http.MethodGet, u, nil)
	if err != nil {
		return
	}
	req.Header.Set("X-Emby-Token", c.apiKey)
	req.Header.Set("Authorization", fmt.Sprintf("MediaBrowser Token=\"%s\"", c.apiKey))

	resp, err := c.httpClient.Do(req)
	if err != nil {
		return
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		return
	}

	type VirtualFolder struct {
		Name      string   `json:"Name"`
		Locations []string `json:"Locations"`
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
	u := fmt.Sprintf("%s/Library/VirtualFolders?name=%s&collectionType=%s&paths=%s&refreshLibrary=true",
		c.baseURL, url.QueryEscape(name), url.QueryEscape(collectionType), url.QueryEscape(path))

	req, err := http.NewRequest(http.MethodPost, u, nil)
	if err != nil {
		return
	}
	req.Header.Set("X-Emby-Token", c.apiKey)
	req.Header.Set("Authorization", fmt.Sprintf("MediaBrowser Token=\"%s\"", c.apiKey))

	resp, err := c.httpClient.Do(req)
	if err == nil {
		resp.Body.Close()
		log.Printf("[Jellyfin] Auto-created '%s' library pointing to %s (Status: %d)", name, path, resp.StatusCode)
	}
}

func (c *Client) RefreshLibrary() error {
	// Auto-provision Movies and Shows libraries if not already registered
	c.EnsureDefaultLibraries()

	u := fmt.Sprintf("%s/Library/Refresh", c.baseURL)
	req, err := http.NewRequest(http.MethodPost, u, nil)
	if err != nil {
		return err
	}
	if c.apiKey != "" {
		req.Header.Set("X-Emby-Token", c.apiKey)
		req.Header.Set("Authorization", fmt.Sprintf("MediaBrowser Token=\"%s\"", c.apiKey))
	}

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
