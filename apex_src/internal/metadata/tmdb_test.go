package metadata

import (
	"encoding/xml"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestNFOEscaping(t *testing.T) {
	specialTitle := `Tom & Jerry: The "Fast" <Special> 'Movie'`
	specialPlot := `Tom & Jerry go to an "island" where 5 < 10 & 20 > 15.`

	// 1. Movie NFO
	movieXML := GenerateMovieNFO(specialTitle, specialPlot, 2021, 7.8, 12345)
	if !strings.Contains(movieXML, "&amp;") {
		t.Errorf("GenerateMovieNFO should escape '&', got: %s", movieXML)
	}
	if !strings.Contains(movieXML, "&lt;") {
		t.Errorf("GenerateMovieNFO should escape '<', got: %s", movieXML)
	}

	var parsedMovie MovieNFO
	if err := xml.Unmarshal([]byte(movieXML), &parsedMovie); err != nil {
		t.Fatalf("Failed to parse generated movie NFO: %v", err)
	}
	if parsedMovie.Title != specialTitle {
		t.Errorf("Parsed title = %q, want %q", parsedMovie.Title, specialTitle)
	}
	if parsedMovie.Plot != specialPlot {
		t.Errorf("Parsed plot = %q, want %q", parsedMovie.Plot, specialPlot)
	}

	// 2. Episode NFO
	epXML := GenerateEpisodeNFO(specialTitle, 2, 5, specialPlot, "2021-05-10", 8.2, 54321)
	var parsedEp EpisodeDetailsNFO
	if err := xml.Unmarshal([]byte(epXML), &parsedEp); err != nil {
		t.Fatalf("Failed to parse generated episode NFO: %v", err)
	}
	if parsedEp.Title != specialTitle {
		t.Errorf("Parsed episode title = %q, want %q", parsedEp.Title, specialTitle)
	}
	if parsedEp.Season != 2 || parsedEp.Episode != 5 {
		t.Errorf("Parsed S/E = %d/%d, want 2/5", parsedEp.Season, parsedEp.Episode)
	}
	if len(parsedEp.UniqueID) == 0 || parsedEp.UniqueID[0].Value != "54321" {
		t.Errorf("Parsed UniqueID TMDB = %+v, want 54321", parsedEp.UniqueID)
	}

	richEpXML := GenerateRichEpisodeNFO("Rich Ep", 1, 1, "Plot", "2021-01-01", 9.0, 1001, 2002, "tt9999999")
	var parsedRichEp EpisodeDetailsNFO
	if err := xml.Unmarshal([]byte(richEpXML), &parsedRichEp); err != nil {
		t.Fatalf("Failed to parse rich episode NFO: %v", err)
	}
	if parsedRichEp.IMDbID != "tt9999999" {
		t.Errorf("Parsed IMDbID = %q, want 'tt9999999'", parsedRichEp.IMDbID)
	}
	hasEpTMDB := false
	hasSeriesTMDB := false
	for _, uid := range parsedRichEp.UniqueID {
		if uid.Type == "tmdb" && uid.Value == "1001" {
			hasEpTMDB = true
		}
		if uid.Type == "tmdb_series" && uid.Value == "2002" {
			hasSeriesTMDB = true
		}
	}
	if !hasEpTMDB || !hasSeriesTMDB {
		t.Errorf("Missing expected unique IDs in rich episode NFO: %+v", parsedRichEp.UniqueID)
	}

	// 3. Rich Movie NFO (Genres, Cast, Tagline, IMDb ID)
	richDetails := &DetailedMetadata{
		Tagline: "Dream bigger.",
		IMDbID:  "tt1375666",
		Genres:  []string{"Action", "Sci-Fi"},
		Actors: []Actor{
			{Name: "Leonardo DiCaprio", Role: "Cobb"},
			{Name: "Joseph Gordon-Levitt", Role: "Arthur"},
		},
	}
	richXML := GenerateRichMovieNFO("Inception", "A thief who steals corporate secrets...", 2010, 8.8, 27205, richDetails)
	if !strings.Contains(richXML, "<genre>Action</genre>") {
		t.Errorf("Rich NFO should contain <genre>Action</genre>, got: %s", richXML)
	}
	if !strings.Contains(richXML, "<name>Leonardo DiCaprio</name>") {
		t.Errorf("Rich NFO should contain actor name, got: %s", richXML)
	}
	if !strings.Contains(richXML, "<tagline>Dream bigger.</tagline>") {
		t.Errorf("Rich NFO should contain tagline, got: %s", richXML)
	}
	if !strings.Contains(richXML, `<uniqueid type="imdb">tt1375666</uniqueid>`) {
		t.Errorf("Rich NFO should contain imdb uniqueid, got: %s", richXML)
	}
	if !strings.Contains(richXML, "<imdbid>tt1375666</imdbid>") {
		t.Errorf("Rich NFO should contain <imdbid>tt1375666</imdbid>, got: %s", richXML)
	}

	var parsedRich MovieNFO
	if err := xml.Unmarshal([]byte(richXML), &parsedRich); err != nil {
		t.Fatalf("Failed to parse rich movie NFO: %v", err)
	}
	if parsedRich.IMDbID != "tt1375666" {
		t.Errorf("Parsed IMDbID = %q, want 'tt1375666'", parsedRich.IMDbID)
	}

	// 4. Multi-Episode NFO (Valid Single-Root XML validation)
	multiNfo := GenerateMultiEpisodeNFO([]EpisodeDetailsNFO{
		{Title: "Episode 1", Season: 1, Episode: 1, Plot: "Pilot part 1", Rating: 8.0, Aired: "2024-01-01"},
		{Title: "Episode 2", Season: 1, Episode: 2, Plot: "Pilot part 2", Rating: 8.5, Aired: "2024-01-01"},
		{Title: "Episode 3", Season: 1, Episode: 3, Plot: "Pilot part 3", Rating: 9.0, Aired: "2024-01-01"},
	})

	var parsedMulti EpisodeDetailsNFO
	if err := xml.Unmarshal([]byte(multiNfo), &parsedMulti); err != nil {
		t.Fatalf("CRITICAL: Multi-episode NFO is not valid XML: %v\nContent:\n%s", err, multiNfo)
	}

	if parsedMulti.XMLName.Local != "episodedetails" {
		t.Errorf("Root tag = %q, want 'episodedetails'", parsedMulti.XMLName.Local)
	}
	if parsedMulti.Episode != 1 {
		t.Errorf("Start episode = %d, want 1", parsedMulti.Episode)
	}
	if parsedMulti.EpisodeNumberEnd != 3 {
		t.Errorf("End episode = %d, want 3", parsedMulti.EpisodeNumberEnd)
	}
	if parsedMulti.Title != "Episode 1 / Episode 2 / Episode 3" {
		t.Errorf("Composite title = %q, want 'Episode 1 / Episode 2 / Episode 3'", parsedMulti.Title)
	}
	if !strings.Contains(parsedMulti.Plot, "Episode 1: Pilot part 1") || !strings.Contains(parsedMulti.Plot, "Episode 3: Pilot part 3") {
		t.Errorf("Composite plot missing episode descriptions: %q", parsedMulti.Plot)
	}
}

func TestTMDBCache(t *testing.T) {
	tempDir, err := os.MkdirTemp("", "tmdb_cache_test_*")
	if err != nil {
		t.Fatalf("failed to create temp dir: %v", err)
	}
	defer os.RemoveAll(tempDir)

	client := NewClient("test_key", tempDir)

	// Test cache set/get
	testKey := "test_entry"
	testPayload := []byte(`{"id": 999, "title": "Cached Movie"}`)

	client.setCache(testKey, testPayload)

	// Verify memory cache hit
	cached, ok := client.getCache(testKey)
	if !ok || string(cached) != string(testPayload) {
		t.Fatalf("expected memory cache hit, got ok=%v, val=%s", ok, string(cached))
	}

	// Create a second client with same dir to test disk cache persistence
	client2 := NewClient("test_key", tempDir)
	diskCached, ok2 := client2.getCache(testKey)
	if !ok2 || string(diskCached) != string(testPayload) {
		t.Fatalf("expected disk cache hit from second client, got ok=%v, val=%s", ok2, string(diskCached))
	}

	// Verify image file skipping
	dummyImg := filepath.Join(tempDir, "poster.jpg")
	if err := os.WriteFile(dummyImg, []byte("fake image data"), 0644); err != nil {
		t.Fatalf("failed to create dummy image: %v", err)
	}
	// Calling DownloadImage on existing image should return nil without making HTTP call
	if err := client.DownloadImage("/fake.jpg", dummyImg); err != nil {
		t.Errorf("DownloadImage on existing image returned error: %v", err)
	}
}
