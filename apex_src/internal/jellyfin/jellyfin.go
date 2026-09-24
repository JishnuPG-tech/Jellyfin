package jellyfin

import (
    "bytes"
    "context"
    "encoding/json"
    "fmt"
    "io"
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

// ClaimAndWriteSTRM atomically creates a new STRM file using O_CREATE|O_EXCL.
// If the target file already exists, it returns os.ErrExist to allow race-free collision handling.
func (w *Writer) ClaimAndWriteSTRM(relPath, apxID string) (string, error) {
	fullPath := filepath.Join(w.baseDir, relPath)
	if err := os.MkdirAll(filepath.Dir(fullPath), 0755); err != nil {
		return "", err
	}

	f, err := os.OpenFile(fullPath, os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0644)
	if err != nil {
		return "", err
	}
	defer f.Close()

	streamURL := fmt.Sprintf("http://127.0.0.1:8084/stream/%s", apxID)
	if _, err := f.WriteString(streamURL); err != nil {
		return "", err
	}
	return fullPath, nil
}

// WriteVersionedSTRM atomically claims a unique STRM filename in relDir following Jellyfin conventions:
// 1. Primary: "<base>.strm"
// 2. Secondary: "<base> - <edition>.strm"
// 3. Collision: "<base> - <edition> [<opaqueID>].strm"
func (w *Writer) WriteVersionedSTRM(relDir, baseTitle, edition, opaqueID string) (fileName string, fullPath string, err error) {
	tag := strings.TrimSpace(edition)
	if tag == "" {
		tag = opaqueID
	}

	candidates := []string{
		baseTitle + ".strm",
		fmt.Sprintf("%s - %s.strm", baseTitle, tag),
		fmt.Sprintf("%s - %s [%s].strm", baseTitle, tag, opaqueID),
	}

	for _, name := range candidates {
		relPath := filepath.Join(relDir, name)
		fp, err := w.ClaimAndWriteSTRM(relPath, opaqueID)
		if err == nil {
			return name, fp, nil
		}
		if os.IsExist(err) {
			continue
		}
		return "", "", err
	}

	// Microsecond fallback to guarantee collision immunity under extreme worker concurrency
	fallback := fmt.Sprintf("%s - %s [%s_%d].strm", baseTitle, tag, opaqueID, time.Now().UnixNano()%100000)
	relPath := filepath.Join(relDir, fallback)
	fp, err := w.ClaimAndWriteSTRM(relPath, opaqueID)
	return fallback, fp, err
}

type Client struct {
    mu sync.RWMutex
    baseURL string
    apiKey string
    mediaRoot string
    httpClient *http.Client
}

func NewClient(baseURL, apiKey string, mediaRoot ...string) *Client {
    root := "/data/jellyfin/media"
    if len(mediaRoot) > 0 && strings.TrimSpace(mediaRoot[0]) != "" { root = strings.TrimSpace(mediaRoot[0]) }
    return &Client{baseURL: strings.TrimRight(baseURL, "/"), apiKey: apiKey, mediaRoot: filepath.Clean(root), httpClient: &http.Client{Timeout: 10 * time.Second}}
}

func (c *Client) SetAPIKey(key string) { c.mu.Lock(); defer c.mu.Unlock(); c.apiKey = key }
func (c *Client) GetAPIKey() string { c.mu.RLock(); defer c.mu.RUnlock(); return c.apiKey }

type VirtualFolder struct {
    Name string `json:"Name"`
    Locations []string `json:"Locations"`
}

func normalizeLibraryPath(path string) string {
    cleaned := strings.TrimSpace(path)
    if cleaned == "" { return "" }
    cleaned = filepath.Clean(cleaned)
    if cleaned == "." { return "" }
    return cleaned
}

func containsLibraryPath(locations []string, wanted string) bool {
    wanted = normalizeLibraryPath(wanted)
    if wanted == "" { return false }
    for _, location := range locations { if normalizeLibraryPath(location) == wanted { return true } }
    return false
}

func (c *Client) desiredLibraryPaths() (string,string) {
    root := normalizeLibraryPath(c.mediaRoot)
    return filepath.Join(root,"Movies"), filepath.Join(root,"Shows")
}

func (c *Client) getVirtualFolders() ([]VirtualFolder,error) {
    key := c.GetAPIKey()
    if key == "" { return nil,fmt.Errorf("APEX_JELLYFIN_API_KEY is not configured") }
    req,err:=http.NewRequest(http.MethodGet,fmt.Sprintf("%s/Library/VirtualFolders",c.baseURL),nil)
    if err!=nil{return nil,err}
    req.Header.Set("X-Emby-Token",key); req.Header.Set("Authorization",fmt.Sprintf("MediaBrowser Token=\"%s\"",key)); req.Header.Set("Accept","application/json")
    resp,err:=c.httpClient.Do(req)
    if err!=nil{return nil,fmt.Errorf("querying Jellyfin virtual folders: %w",err)}
    defer resp.Body.Close()
    if resp.StatusCode==http.StatusUnauthorized||resp.StatusCode==http.StatusForbidden{return nil,fmt.Errorf("APEX_JELLYFIN_API_KEY was rejected by Jellyfin (HTTP %d)",resp.StatusCode)}
    if resp.StatusCode!=http.StatusOK{return nil,fmt.Errorf("Jellyfin virtual-folder query returned HTTP %d",resp.StatusCode)}
    var folders []VirtualFolder
    if err:=json.NewDecoder(resp.Body).Decode(&folders);err!=nil{return nil,fmt.Errorf("decoding Jellyfin virtual folders: %w",err)}
    return folders,nil
}

func (c *Client) VerifyIntegration(ctx context.Context) (reachable bool, authValid bool, hasMovies bool, hasShows bool, err error) {
    req,err:=http.NewRequestWithContext(ctx,http.MethodGet,fmt.Sprintf("%s/System/Info/Public",c.baseURL),nil)
    if err!=nil{return false,false,false,false,err}
    resp,err:=c.httpClient.Do(req)
    if err!=nil{return false,false,false,false,fmt.Errorf("jellyfin server unreachable: %w",err)}
    resp.Body.Close(); reachable=true
    if c.GetAPIKey()==""{return reachable,false,false,false,fmt.Errorf("APEX_JELLYFIN_API_KEY is not set")}
    folders,err:=c.getVirtualFolders(); if err!=nil{return reachable,false,false,false,err}; authValid=true
    moviePath,showPath:=c.desiredLibraryPaths()
    for _,f:=range folders {
        if strings.EqualFold(f.Name,"Movies")&&containsLibraryPath(f.Locations,moviePath){hasMovies=true}
        if (strings.EqualFold(f.Name,"Shows")||strings.EqualFold(f.Name,"TV Shows")||strings.EqualFold(f.Name,"Series"))&&containsLibraryPath(f.Locations,showPath){hasShows=true}
    }
    return reachable,authValid,hasMovies,hasShows,nil
}

func (c *Client) EnsureDefaultLibraries() error {
    folders,err:=c.getVirtualFolders();if err!=nil{return err}
    moviePath,showPath:=c.desiredLibraryPaths()
    if err:=c.ensureLibrary(folders,[]string{"Movies"},"Movies","movies",moviePath);err!=nil{return err}
    folders,err=c.getVirtualFolders();if err!=nil{return err}
    return c.ensureLibrary(folders,[]string{"Shows","TV Shows","Series"},"Shows","tvshows",showPath)
}

func (c *Client) ensureLibrary(folders []VirtualFolder,aliases []string,canonicalName,collectionType,canonicalPath string) error {
    var existing *VirtualFolder
    for i:=range folders {
        for _,alias:=range aliases {if strings.EqualFold(folders[i].Name,alias){existing=&folders[i];break}}
        if existing!=nil{break}
    }
    if existing==nil{return c.addVirtualFolder(canonicalName,collectionType,canonicalPath)}
    if !containsLibraryPath(existing.Locations,canonicalPath){
        if err:=c.addMediaPath(existing.Name,canonicalPath,false);err!=nil{return fmt.Errorf("repair library %q by adding %q: %w",existing.Name,canonicalPath,err)}
    }
    canonical:=normalizeLibraryPath(canonicalPath)
    for _,old:=range existing.Locations {
        if normalized:=normalizeLibraryPath(old);normalized!=""&&normalized!=canonical {
            if err:=c.removeMediaPath(existing.Name,old,false);err!=nil{return fmt.Errorf("repair library %q by removing stale path %q: %w",existing.Name,old,err)}
        }
    }
    log.Printf("[Jellyfin] Library %q is configured with canonical path %s.",existing.Name,canonicalPath)
    return nil
}

func readResponseBody(resp *http.Response) string {b,err:=io.ReadAll(resp.Body);if err!=nil{return ""};return strings.TrimSpace(string(b))}
func formatResponseBody(body string) string {body=strings.TrimSpace(body);if body==""{return ""};if len(body)>300{body=body[:300]+"..."};return ": "+body}

func (c *Client) addVirtualFolder(name,collectionType,path string) error {
    key:=c.GetAPIKey();if key==""{return fmt.Errorf("APEX_JELLYFIN_API_KEY is not configured")}
    u:=fmt.Sprintf("%s/Library/VirtualFolders?name=%s&collectionType=%s&paths=%s&refreshLibrary=false",c.baseURL,url.QueryEscape(name),url.QueryEscape(collectionType),url.QueryEscape(path))
    req,err:=http.NewRequest(http.MethodPost,u,nil);if err!=nil{return err}
    req.Header.Set("X-Emby-Token",key);req.Header.Set("Authorization",fmt.Sprintf("MediaBrowser Token=\"%s\"",key));req.Header.Set("Accept","application/json")
    resp,err:=c.httpClient.Do(req);if err!=nil{return fmt.Errorf("creating library %q: %w",name,err)}
    body:=readResponseBody(resp);status:=resp.StatusCode;resp.Body.Close()
    if status>=200&&status<300{log.Printf("[Jellyfin] Auto-created %q library pointing to %s (HTTP %d).",name,path,status);return nil}
    return fmt.Errorf("creating library %q returned HTTP %d%s",name,status,formatResponseBody(body))
}

func (c *Client) addMediaPath(name,path string,refreshLibrary bool) error {
    key:=c.GetAPIKey();if key==""{return fmt.Errorf("APEX_JELLYFIN_API_KEY is not configured")}
    endpoint:=fmt.Sprintf("%s/Library/VirtualFolders/Paths?refreshLibrary=%t",c.baseURL,refreshLibrary)
    payload:=struct{Name string `json:"Name"`;Path string `json:"Path"`}{Name:name,Path:path}
    raw,err:=json.Marshal(payload);if err!=nil{return fmt.Errorf("encoding media path request: %w",err)}
    req,err:=http.NewRequest(http.MethodPost,endpoint,bytes.NewReader(raw));if err!=nil{return err}
    req.Header.Set("Content-Type","application/json");req.Header.Set("Accept","application/json");req.Header.Set("X-Emby-Token",key);req.Header.Set("Authorization",fmt.Sprintf("MediaBrowser Token=\"%s\"",key))
    resp,err:=c.httpClient.Do(req);if err!=nil{return fmt.Errorf("adding path %s to library %q: %w",path,name,err)}
    body:=readResponseBody(resp);status:=resp.StatusCode;resp.Body.Close()
    if status>=200&&status<300{log.Printf("[Jellyfin] Added canonical path %s to library %q (HTTP %d).",path,name,status);return nil}
    return fmt.Errorf("adding path %s to library %q returned HTTP %d%s",path,name,status,formatResponseBody(body))
}

func (c *Client) removeMediaPath(name,path string,refreshLibrary bool) error {
    key:=c.GetAPIKey();if key==""{return fmt.Errorf("APEX_JELLYFIN_API_KEY is not configured")}
    endpoint:=fmt.Sprintf("%s/Library/VirtualFolders/Paths?name=%s&path=%s&refreshLibrary=%t",c.baseURL,url.QueryEscape(name),url.QueryEscape(path),refreshLibrary)
    req,err:=http.NewRequest(http.MethodDelete,endpoint,nil);if err!=nil{return err}
    req.Header.Set("X-Emby-Token",key);req.Header.Set("Authorization",fmt.Sprintf("MediaBrowser Token=\"%s\"",key));req.Header.Set("Accept","application/json")
    resp,err:=c.httpClient.Do(req);if err!=nil{return fmt.Errorf("removing path %s from library %q: %w",path,name,err)}
    body:=readResponseBody(resp);status:=resp.StatusCode;resp.Body.Close()
    if (status>=200&&status<300)||status==http.StatusNotFound{log.Printf("[Jellyfin] Removed stale path %s from library %q (HTTP %d).",path,name,status);return nil}
    return fmt.Errorf("removing path %s from library %q returned HTTP %d%s",path,name,status,formatResponseBody(body))
}

func (c *Client) RefreshLibrary() error {
    key:=c.GetAPIKey();if key==""{return fmt.Errorf("APEX_JELLYFIN_API_KEY is not configured")}
    if err:=c.EnsureDefaultLibraries();err!=nil{return err}
    req,err:=http.NewRequest(http.MethodPost,fmt.Sprintf("%s/Library/Refresh",c.baseURL),nil);if err!=nil{return err}
    req.Header.Set("X-Emby-Token",key);req.Header.Set("Authorization",fmt.Sprintf("MediaBrowser Token=\"%s\"",key));req.Header.Set("Accept","application/json")
    resp,err:=c.httpClient.Do(req);if err!=nil{return fmt.Errorf("requesting Jellyfin library refresh: %w",err)}
    body:=readResponseBody(resp);status:=resp.StatusCode;resp.Body.Close()
    if status<200||status>=300{return fmt.Errorf("jellyfin refresh returned HTTP %d%s",status,formatResponseBody(body))};return nil
}
