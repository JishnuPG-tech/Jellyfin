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
	EndEpisode   int // for multi-episodes: S01E01-E03
	Resolution   string
	Confidence   float64 // 0.0 - 1.0
}

var (
	// Multi-episode: S01E01-E03 or S01E01-03 or S01E01E02
	reMultiEp = regexp.MustCompile(`(?i)[._ -]S([0-9]{1,2})E([0-9]{1,3})[-_ ]?E?([0-9]{1,3})`)

	// Standard TV Season / Episode patterns: S01E02, s01e02
	reSeasonEp1 = regexp.MustCompile(`(?i)[._ -]S([0-9]{1,2})E([0-9]{1,3})`)
	// 1x02
	reSeasonEp2 = regexp.MustCompile(`(?i)[._ -]([0-9]{1,2})x([0-9]{1,3})`)
	// Season 1 Episode 2
	reSeasonEp3 = regexp.MustCompile(`(?i)Season[._ -]([0-9]{1,2})[._ -]Episode[._ -]([0-9]{1,3})`)
	// EP01 or Episode 01 (default season 1)
	reEpOnly = regexp.MustCompile(`(?i)[._ -](?:EP|Episode)[._ -]?([0-9]{1,3})`)
	// Anime numbering: [Group] Title - 05 [1080p]
	reAnimeEp = regexp.MustCompile(`(?i)[._ -]- ([0-9]{1,3})[._ -]`)

	// Movie Year pattern: (1990) to (2030) or .1990. to .2030.
	reYear = regexp.MustCompile(`(?i)[\[\(\._ -](19[5-9][0-9]|20[0-3][0-9])[\]\)\._ -]`)

	// Quality / Codec tags
	reResolution = regexp.MustCompile(`(?i)(2160p|4k|1080p|720p|480p|576p|uhd)`)
	reNoise      = regexp.MustCompile(`(?i)[\[\(]?(2160p|4k|1080p|720p|480p|576p|uhd|bluray|blu-ray|webrip|web-dl|web|hdtv|x264|x265|hevc|av1|aac|dts|remux|h264|h265|proper|repack|internal|tinymkv|yts|yify)[\]\)]?`)
	reBrackets   = regexp.MustCompile(`^\[[^\]]+\]\s*`) // e.g., [SubGroup] at start
	reExtension  = regexp.MustCompile(`\.(mp4|mkv|avi|mov|webm|ts|m4v|flv|wmv)$`)
)

func Parse(raw string) *ParsedMedia {
	res := &ParsedMedia{
		OriginalName: raw,
		MediaType:    "movie",
		Confidence:   0.5,
	}

	clean := filepath.Base(raw)
	clean = reExtension.ReplaceAllString(clean, "")

	// Extract resolution if present
	if match := reResolution.FindStringSubmatch(clean); len(match) > 1 {
		res.Resolution = strings.ToUpper(match[1])
	}

	// Strip release group brackets at start, e.g. [SubsPlease] Title -> Title
	clean = reBrackets.ReplaceAllString(clean, "")

	// 1. Check for Multi-Episode: S01E01-E03
	if match := reMultiEp.FindStringSubmatch(clean); len(match) == 4 {
		res.MediaType = "series"
		res.Season, _ = strconv.Atoi(match[1])
		res.Episode, _ = strconv.Atoi(match[2])
		res.EndEpisode, _ = strconv.Atoi(match[3])
		clean = clean[:reMultiEp.FindStringIndex(clean)[0]]
		res.Confidence += 0.3
	} else if match := reSeasonEp1.FindStringSubmatch(clean); len(match) == 3 {
		res.MediaType = "series"
		res.Season, _ = strconv.Atoi(match[1])
		res.Episode, _ = strconv.Atoi(match[2])
		clean = clean[:reSeasonEp1.FindStringIndex(clean)[0]]
		res.Confidence += 0.3
	} else if match := reSeasonEp2.FindStringSubmatch(clean); len(match) == 3 {
		res.MediaType = "series"
		res.Season, _ = strconv.Atoi(match[1])
		res.Episode, _ = strconv.Atoi(match[2])
		clean = clean[:reSeasonEp2.FindStringIndex(clean)[0]]
		res.Confidence += 0.25
	} else if match := reSeasonEp3.FindStringSubmatch(clean); len(match) == 3 {
		res.MediaType = "series"
		res.Season, _ = strconv.Atoi(match[1])
		res.Episode, _ = strconv.Atoi(match[2])
		clean = clean[:reSeasonEp3.FindStringIndex(clean)[0]]
		res.Confidence += 0.3
	} else if match := reEpOnly.FindStringSubmatch(clean); len(match) == 2 {
		res.MediaType = "series"
		res.Season = 1
		res.Episode, _ = strconv.Atoi(match[1])
		clean = clean[:reEpOnly.FindStringIndex(clean)[0]]
		res.Confidence += 0.2
	} else if match := reAnimeEp.FindStringSubmatch(clean); len(match) == 2 {
		res.MediaType = "series"
		res.Season = 1
		res.Episode, _ = strconv.Atoi(match[1])
		clean = clean[:reAnimeEp.FindStringIndex(clean)[0]]
		res.Confidence += 0.2
	}

	// 2. Check for Year
	if match := reYear.FindStringSubmatch(clean); len(match) >= 2 {
		res.Year, _ = strconv.Atoi(match[1])
		idx := reYear.FindStringIndex(clean)
		if res.MediaType == "movie" {
			clean = clean[:idx[0]]
		}
		res.Confidence += 0.2
	}

	// 3. Remove noise, resolution, and codec tags
	clean = reNoise.ReplaceAllString(clean, "")
	clean = strings.ReplaceAll(clean, ".", " ")
	clean = strings.ReplaceAll(clean, "_", " ")
	clean = strings.ReplaceAll(clean, "-", " ")
	clean = strings.TrimSpace(clean)

	if clean != "" {
		res.CleanTitle = clean
	} else {
		res.CleanTitle = filepath.Base(raw)
	}

	if res.Confidence > 1.0 {
		res.Confidence = 1.0
	}

	return res
}
