package parser

import (
	"testing"
)

func TestParserMovie(t *testing.T) {
	tests := []struct {
		input     string
		wantTitle string
		wantYear  int
		wantType  string
	}{
		{
			input:     "Inception.2010.1080p.BluRay.x264.mp4",
			wantTitle: "Inception",
			wantYear:  2010,
			wantType:  "movie",
		},
		{
			input:     "The Matrix (1999) [2160p] [UHD] [Remux].mkv",
			wantTitle: "The Matrix",
			wantYear:  1999,
			wantType:  "movie",
		},
		{
			input:     "Interstellar.2014.IMAX.720p.BRRip.mkv",
			wantTitle: "Interstellar",
			wantYear:  2014,
			wantType:  "movie",
		},
	}

	for _, tt := range tests {
		res := Parse(tt.input)
		if res.MediaType != tt.wantType {
			t.Errorf("Parse(%q).MediaType = %v, want %v", tt.input, res.MediaType, tt.wantType)
		}
		if res.CleanTitle != tt.wantTitle {
			t.Errorf("Parse(%q).CleanTitle = %v, want %v", tt.input, res.CleanTitle, tt.wantTitle)
		}
		if res.Year != tt.wantYear {
			t.Errorf("Parse(%q).Year = %v, want %v", tt.input, res.Year, tt.wantYear)
		}
	}
}

func TestParserSeries(t *testing.T) {
	tests := []struct {
		input       string
		wantTitle   string
		wantSeason  int
		wantEpisode int
		wantEndEp   int
	}{
		{
			input:       "Breaking.Bad.S01E05.720p.HDTV.x264.mkv",
			wantTitle:   "Breaking Bad",
			wantSeason:  1,
			wantEpisode: 5,
			wantEndEp:   0,
		},
		{
			input:       "Stranger.Things.S04E01-E03.1080p.NF.WEB-DL.mkv",
			wantTitle:   "Stranger Things",
			wantSeason:  4,
			wantEpisode: 1,
			wantEndEp:   3,
		},
		{
			input:       "The.Office.US.2x04.HDTV.mp4",
			wantTitle:   "The Office US",
			wantSeason:  2,
			wantEpisode: 4,
			wantEndEp:   0,
		},
		{
			input:       "[SubsPlease] Sousou no Frieren - 05 (1080p) [12345678].mkv",
			wantTitle:   "Sousou no Frieren",
			wantSeason:  1,
			wantEpisode: 5,
			wantEndEp:   0,
		},
	}

	for _, tt := range tests {
		res := Parse(tt.input)
		if res.MediaType != "series" {
			t.Errorf("Parse(%q).MediaType = %v, want series", tt.input, res.MediaType)
		}
		if res.CleanTitle != tt.wantTitle {
			t.Errorf("Parse(%q).CleanTitle = %q, want %q", tt.input, res.CleanTitle, tt.wantTitle)
		}
		if res.Season != tt.wantSeason {
			t.Errorf("Parse(%q).Season = %d, want %d", tt.input, res.Season, tt.wantSeason)
		}
		if res.Episode != tt.wantEpisode {
			t.Errorf("Parse(%q).Episode = %d, want %d", tt.input, res.Episode, tt.wantEpisode)
		}
		if res.EndEpisode != tt.wantEndEp {
			t.Errorf("Parse(%q).EndEpisode = %d, want %d", tt.input, res.EndEpisode, tt.wantEndEp)
		}
	}
}
