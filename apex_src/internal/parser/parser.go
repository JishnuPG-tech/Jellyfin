package parser

import (
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
)

type ParsedMedia struct {
	OriginalName string
	CleanTitle   string
	MediaType    string // "movie" or "series"
	Year         int
	Season       int
	Episode      int
	Resolution   string
}

var (
	// TV Season / Episode patterns: S01E02, s01e02, 1x02, Season 1 Episode 2
	reSeasonEp1 = regexp.MustCompile(`(?i)[._ -]S([0-9]{1,2})E([0-9]{1,3})`)
	reSeasonEp2 = regexp.MustCompile(`(?i)[._ -]([0-9]{1,2})x([0-9]{1,3})`)
	reSeasonEp3 = regexp.MustCompile(`(?i)Season[._ -]([0-9]{1,2})[._ -]Episode[._ -]([0-9]{1,3})`)
	
	// Movie Year pattern: (1990) to (2030) or .1990. to .2030.
	reYear = regexp.MustCompile(`(?i)[\[\(\._ -](19[5-9][0-9]|20[0-3][0-9])[\]\)\._ -]`)

	// Quality / Codec junk tags to remove from title
	reNoise = regexp.MustCompile(`(?i)[\[\(]?(1080p|720p|2160p|4k|uhd|bluray|blu-ray|webrip|web-dl|web|hdtv|x264|x265|hevc|aac|dts|remux)[\]\)]?`)
	reExtension = regexp.MustCompile(`\.(mp4|mkv|avi|mov|webm)$`)
)

func Parse(raw string) *ParsedMedia {
	res := &ParsedMedia{
		OriginalName: raw,
		MediaType:    "movie",
	}

	clean := filepath.Base(raw)
	clean = reExtension.ReplaceAllString(clean, "")

	// Check for TV Show markers
	if match := reSeasonEp1.FindStringSubmatch(clean); len(match) == 3 {
		res.MediaType = "series"
		res.Season, _ = strconv.Atoi(match[1])
		res.Episode, _ = strconv.Atoi(match[2])
		clean = clean[:reSeasonEp1.FindStringIndex(clean)[0]]
	} else if match := reSeasonEp2.FindStringSubmatch(clean); len(match) == 3 {
		res.MediaType = "series"
		res.Season, _ = strconv.Atoi(match[1])
		res.Episode, _ = strconv.Atoi(match[2])
		clean = clean[:reSeasonEp2.FindStringIndex(clean)[0]]
	} else if match := reSeasonEp3.FindStringSubmatch(clean); len(match) == 3 {
		res.MediaType = "series"
		res.Season, _ = strconv.Atoi(match[1])
		res.Episode, _ = strconv.Atoi(match[2])
		clean = clean[:reSeasonEp3.FindStringIndex(clean)[0]]
	}

	// Check for Year
	if match := reYear.FindStringSubmatch(clean); len(match) >= 2 {
		res.Year, _ = strconv.Atoi(match[1])
		idx := reYear.FindStringIndex(clean)
		if res.MediaType == "movie" {
			clean = clean[:idx[0]]
		}
	}

	// Remove quality tags and common noise
	clean = reNoise.ReplaceAllString(clean, "")
	clean = strings.ReplaceAll(clean, ".", " ")
	clean = strings.ReplaceAll(clean, "_", " ")
	clean = strings.ReplaceAll(clean, "-", " ")
	clean = strings.TrimSpace(clean)

	res.CleanTitle = clean
	return res
}
