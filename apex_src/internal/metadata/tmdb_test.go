package metadata

import (
	"encoding/xml"
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
}
