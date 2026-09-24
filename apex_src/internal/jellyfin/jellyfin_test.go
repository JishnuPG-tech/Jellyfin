package jellyfin

import (
    "context"
    "encoding/json"
    "io"
    "net/http"
    "net/http/httptest"
    "testing"
)

func TestAddMediaPathUsesJSONMediaPathDto(t *testing.T) {
    srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
        if r.Method != http.MethodPost { t.Fatalf("method=%s", r.Method) }
        if r.URL.Path != "/Library/VirtualFolders/Paths" { t.Fatalf("path=%s", r.URL.Path) }
        if r.Header.Get("Content-Type") != "application/json" { t.Fatalf("content type=%q", r.Header.Get("Content-Type")) }
        if r.URL.Query().Get("refreshLibrary") != "false" { t.Fatalf("refreshLibrary=%q", r.URL.Query().Get("refreshLibrary")) }
        raw, err := io.ReadAll(r.Body); if err != nil { t.Fatal(err) }
        var body struct { Name string `json:"Name"`; Path string `json:"Path"` }
        if err := json.Unmarshal(raw, &body); err != nil { t.Fatalf("invalid JSON: %v", err) }
        if body.Name != "Movies" || body.Path != "/data/jellyfin/media/Movies" { t.Fatalf("unexpected body: %+v", body) }
        w.WriteHeader(http.StatusNoContent)
    }))
    defer srv.Close()
    c := NewClient(srv.URL, "test-key", "/data/jellyfin/media")
    if err := c.addMediaPath("Movies", "/data/jellyfin/media/Movies", false); err != nil { t.Fatal(err) }
}

func TestEnsureDefaultLibrariesRepairsStalePath(t *testing.T) {
    var added, removed bool
    srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
        switch {
        case r.Method == http.MethodGet && r.URL.Path == "/Library/VirtualFolders":
            _, _ = w.Write([]byte("[{\"Name\":\"Movies\",\"Locations\":[\"/data/jellyfin/data/data/playlists\"]},{\"Name\":\"Shows\",\"Locations\":[\"/data/jellyfin/media/Shows\"]}]"))
        case r.Method == http.MethodPost && r.URL.Path == "/Library/VirtualFolders/Paths":
            added = true; w.WriteHeader(http.StatusNoContent)
        case r.Method == http.MethodDelete && r.URL.Path == "/Library/VirtualFolders/Paths":
            removed = true; w.WriteHeader(http.StatusNoContent)
        default:
            t.Fatalf("unexpected request %s %s", r.Method, r.URL.String())
        }
    }))
    defer srv.Close()
    c := NewClient(srv.URL, "test-key", "/data/jellyfin/media")
    if err := c.EnsureDefaultLibraries(); err != nil { t.Fatal(err) }
    if !added { t.Fatal("canonical Movies path was not added") }
    if !removed { t.Fatal("stale Movies path was not removed") }
}

func TestVerifyIntegrationRequiresCanonicalPaths(t *testing.T) {
    srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
        switch r.URL.Path {
        case "/System/Info/Public":
            _, _ = w.Write([]byte("{}"))
        case "/Library/VirtualFolders":
            _, _ = w.Write([]byte("[{\"Name\":\"Movies\",\"Locations\":[\"/data/jellyfin/media/Movies\"]},{\"Name\":\"Shows\",\"Locations\":[\"/wrong/shows\"]}]"))
        default:
            t.Fatalf("unexpected request %s %s", r.Method, r.URL.String())
        }
    }))
    defer srv.Close()
    c := NewClient(srv.URL, "test-key", "/data/jellyfin/media")
    reachable, authValid, movies, shows, err := c.VerifyIntegration(context.Background())
    if err != nil { t.Fatal(err) }
    if !reachable || !authValid || !movies || shows { t.Fatalf("unexpected result: reachable=%v auth=%v movies=%v shows=%v", reachable, authValid, movies, shows) }
}
