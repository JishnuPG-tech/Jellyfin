package jellyfin

import (
	"fmt"
	"net/http"
	"os"
	"path/filepath"
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
			Timeout: 5 * time.Second,
		},
	}
}

func (c *Client) RefreshLibrary() error {
	u := fmt.Sprintf("%s/Library/Refresh", c.baseURL)
	req, err := http.NewRequest(http.MethodPost, u, nil)
	if err != nil {
		return err
	}
	if c.apiKey != "" {
		req.Header.Set("X-Emby-Token", c.apiKey)
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
