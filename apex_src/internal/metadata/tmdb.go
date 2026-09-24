package metadata

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"encoding/xml"
	"fmt"
	"io"
	"math"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"time"
)

type Client struct {
	apiKey     string
	cacheDir   string
	httpClient *http.Client
	mu         sync.RWMutex
	memCache   map[string][]byte
}

type TMDBResult struct {
	ID            int     `json:"id"`
	Title         string  `json:"title"`
	Name          string  `json:"name"`
	OriginalTitle string  `json:"original_title"`
	OriginalName  string  `json:"original_name"`
	Overview      string  `json:"overview"`
	PosterPath    string  `json:"poster_path"`
	BackdropPath  string  `json:"backdrop_path"`
	ReleaseDate   string  `json:"release_date"`
	FirstAirDate  string  `json:"first_air_date"`
	VoteAverage   float64 `json:"vote_average"`
	Popularity    float64 `json:"popularity"`
	Confidence    float64 `json:"confidence"` // Normalized between 0.0 and 1.0
}

type TMDBSearchResponse struct {
	Results []TMDBResult `json:"results"`
}

type TMDBEpisode struct {
	ID            int     `json:"id"`
	Name          string  `json:"name"`
	Overview      string  `json:"overview"`
	AirDate       string  `json:"air_date"`
	EpisodeNumber int     `json:"episode_number"`
	SeasonNumber  int     `json:"season_number"`
	StillPath     string  `json:"still_path"`
	VoteAverage   float64 `json:"vote_average"`
}

func NewClient(apiKey string, cacheDir ...string) *Client {
	cDir := ""
	if len(cacheDir) > 0 {
		cDir = cacheDir[0]
	}
	if cDir != "" {
		_ = os.MkdirAll(cDir, 0755)
		_ = os.MkdirAll(filepath.Join(cDir, "images"), 0755)
	}
	return &Client{
		apiKey:   apiKey,
		cacheDir: cDir,
		httpClient: &http.Client{
			Timeout: 10 * time.Second,
		},
		memCache: make(map[string][]byte),
	}
}

func (c *Client) getCache(key string) ([]byte, bool) {
	c.mu.RLock()
	val, ok := c.memCache[key]
	c.mu.RUnlock()
	if ok {
		return val, true
	}
	if c.cacheDir == "" {
		return nil, false
	}
	cacheFile := filepath.Join(c.cacheDir, key+".json")
	fi, err := os.Stat(cacheFile)
	if err != nil {
		return nil, false
	}
	// Expire disk cache older than 7 days
	if time.Since(fi.ModTime()) > 7*24*time.Hour {
		_ = os.Remove(cacheFile)
		return nil, false
	}
	data, err := os.ReadFile(cacheFile)
	if err == nil {
		c.mu.Lock()
		c.memCache[key] = data
		c.mu.Unlock()
		return data, true
	}
	return nil, false
}

func (c *Client) setCache(key string, data []byte) {
	c.mu.Lock()
	c.memCache[key] = data
	c.mu.Unlock()
	if c.cacheDir != "" {
		_ = os.MkdirAll(c.cacheDir, 0755)
		dest := filepath.Join(c.cacheDir, key+".json")
		tmp := fmt.Sprintf("%s.tmp.%d", dest, time.Now().UnixNano())
		if err := os.WriteFile(tmp, data, 0644); err == nil {
			_ = os.Rename(tmp, dest)
		}
	}
}

func cacheHash(val string) string {
	h := sha256.Sum256([]byte(val))
	return hex.EncodeToString(h[:12])
}

func (c *Client) Search(title string, year int, mediaType string) (*TMDBResult, error) {
	if c.apiKey == "" {
		return nil, fmt.Errorf("TMDB_API_KEY is not configured")
	}

	res, err := c.searchInternal(title, year, mediaType)
	if err != nil && year > 0 {
		// Fallback: search without year in case release year is off by 1
		if resRetry, errRetry := c.searchInternal(title, 0, mediaType); errRetry == nil {
			return resRetry, nil
		}
	}
	return res, err
}

func (c *Client) searchInternal(title string, year int, mediaType string) (*TMDBResult, error) {
	cacheKey := fmt.Sprintf("search_%s", cacheHash(fmt.Sprintf("%s_%d_%s", strings.ToLower(title), year, mediaType)))
	if cachedData, ok := c.getCache(cacheKey); ok {
		var cached TMDBResult
		if err := json.Unmarshal(cachedData, &cached); err == nil {
			return &cached, nil
		}
	}

	endpoint := "movie"
	if mediaType == "series" {
		endpoint = "tv"
	}

	u := fmt.Sprintf("https://api.themoviedb.org/3/search/%s?api_key=%s&query=%s",
		endpoint, c.apiKey, url.QueryEscape(title))
	if year > 0 {
		if mediaType == "series" {
			u += fmt.Sprintf("&first_air_date_year=%d", year)
		} else {
			u += fmt.Sprintf("&year=%d", year)
		}
	}

	resp, err := c.httpClient.Get(u)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("TMDB returned HTTP %d", resp.StatusCode)
	}

	var searchResp TMDBSearchResponse
	if err := json.NewDecoder(resp.Body).Decode(&searchResp); err != nil {
		return nil, err
	}

	if len(searchResp.Results) == 0 {
		return nil, fmt.Errorf("no metadata found for %s", title)
	}

	// Select best match based on normalized confidence scoring (0.0 to 1.0)
	bestIdx := 0
	highestScore := 0.0
	secondHighestScore := 0.0

	normQuery := strings.ToLower(strings.TrimSpace(title))

	for i, candidate := range searchResp.Results {
		score := 0.0
		candidateTitle := strings.ToLower(candidate.Title)
		if candidateTitle == "" {
			candidateTitle = strings.ToLower(candidate.Name)
		}

		// 1. Title match (max 0.50)
		if candidateTitle == normQuery {
			score += 0.50
		} else if strings.Contains(candidateTitle, normQuery) || strings.Contains(normQuery, candidateTitle) {
			score += 0.35
		} else {
			queryTokens := strings.Fields(normQuery)
			matches := 0
			for _, tok := range queryTokens {
				if strings.Contains(candidateTitle, tok) {
					matches++
				}
			}
			if len(queryTokens) > 0 {
				score += (float64(matches) / float64(len(queryTokens))) * 0.25
			}
		}

		// 2. Year match (max 0.30)
		candYear := 0
		if len(candidate.ReleaseDate) >= 4 {
			candYear, _ = strconv.Atoi(candidate.ReleaseDate[:4])
		} else if len(candidate.FirstAirDate) >= 4 {
			candYear, _ = strconv.Atoi(candidate.FirstAirDate[:4])
		}

		if year > 0 && candYear > 0 {
			if candYear == year {
				score += 0.30
			} else if candYear == year-1 || candYear == year+1 {
				score += 0.15
			}
		} else if year == 0 {
			score += 0.15
		}

		// 3. Popularity & Vote quality (max 0.20)
		score += math.Min(candidate.Popularity/50.0, 1.0) * 0.10
		if candidate.VoteAverage > 0 {
			score += (math.Min(candidate.VoteAverage, 10.0) / 10.0) * 0.10
		}

		if score > 1.0 {
			score = 1.0
		}

		if score > highestScore {
			secondHighestScore = highestScore
			highestScore = score
			bestIdx = i
		} else if score > secondHighestScore {
			secondHighestScore = score
		}
	}

	res := searchResp.Results[bestIdx]

	// Ambiguity Margin Check:
	// If second best is within 0.12 of top score and title isn't an exact match,
	// penalize confidence to prevent auto-accepting an ambiguous match.
	topTitle := strings.ToLower(res.Title)
	if topTitle == "" {
		topTitle = strings.ToLower(res.Name)
	}
	if len(searchResp.Results) > 1 && (highestScore-secondHighestScore) < 0.12 && topTitle != normQuery {
		highestScore = highestScore * 0.75
	}

	res.Confidence = highestScore

	if resBytes, err := json.Marshal(&res); err == nil {
		c.setCache(cacheKey, resBytes)
	}

	return &res, nil
}

func (c *Client) GetEpisodeDetails(seriesID int, seasonNumber int, episodeNumber int) (*TMDBEpisode, error) {
	if c.apiKey == "" {
		return nil, fmt.Errorf("TMDB_API_KEY is not configured")
	}

	cacheKey := fmt.Sprintf("ep_%d_%d_%d", seriesID, seasonNumber, episodeNumber)
	if cachedData, ok := c.getCache(cacheKey); ok {
		var ep TMDBEpisode
		if err := json.Unmarshal(cachedData, &ep); err == nil {
			return &ep, nil
		}
	}

	u := fmt.Sprintf("https://api.themoviedb.org/3/tv/%d/season/%d/episode/%d?api_key=%s",
		seriesID, seasonNumber, episodeNumber, c.apiKey)

	resp, err := c.httpClient.Get(u)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("TMDB episode returned HTTP %d", resp.StatusCode)
	}

	var ep TMDBEpisode
	if err := json.NewDecoder(resp.Body).Decode(&ep); err != nil {
		return nil, err
	}

	if epBytes, err := json.Marshal(&ep); err == nil {
		c.setCache(cacheKey, epBytes)
	}

	return &ep, nil
}

func (c *Client) GetMovieDetails(movieID int) (*DetailedMetadata, error) {
	if c.apiKey == "" || movieID <= 0 {
		return nil, fmt.Errorf("TMDB_API_KEY is not configured or invalid ID")
	}

	cacheKey := fmt.Sprintf("movie_details_%d", movieID)
	if cachedData, ok := c.getCache(cacheKey); ok {
		var meta DetailedMetadata
		if err := json.Unmarshal(cachedData, &meta); err == nil {
			return &meta, nil
		}
	}

	u := fmt.Sprintf("https://api.themoviedb.org/3/movie/%d?api_key=%s&append_to_response=credits",
		movieID, c.apiKey)

	resp, err := c.httpClient.Get(u)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("TMDB movie returned HTTP %d", resp.StatusCode)
	}

	var raw struct {
		Tagline string `json:"tagline"`
		IMDbID  string `json:"imdb_id"`
		Genres  []struct {
			Name string `json:"name"`
		} `json:"genres"`
		Credits struct {
			Cast []struct {
				Name      string `json:"name"`
				Character string `json:"character"`
			} `json:"cast"`
		} `json:"credits"`
	}

	if err := json.NewDecoder(resp.Body).Decode(&raw); err != nil {
		return nil, err
	}

	res := &DetailedMetadata{
		Tagline: raw.Tagline,
		IMDbID:  raw.IMDbID,
	}
	for _, g := range raw.Genres {
		if g.Name != "" {
			res.Genres = append(res.Genres, g.Name)
		}
	}
	for i, actor := range raw.Credits.Cast {
		if i >= 10 {
			break
		}
		res.Actors = append(res.Actors, Actor{
			Name: actor.Name,
			Role: actor.Character,
		})
	}

	if resBytes, err := json.Marshal(res); err == nil {
		c.setCache(cacheKey, resBytes)
	}

	return res, nil
}

func (c *Client) GetShowDetails(showID int) (*DetailedMetadata, error) {
	if c.apiKey == "" || showID <= 0 {
		return nil, fmt.Errorf("TMDB_API_KEY is not configured or invalid ID")
	}

	cacheKey := fmt.Sprintf("show_details_%d", showID)
	if cachedData, ok := c.getCache(cacheKey); ok {
		var meta DetailedMetadata
		if err := json.Unmarshal(cachedData, &meta); err == nil {
			return &meta, nil
		}
	}

	u := fmt.Sprintf("https://api.themoviedb.org/3/tv/%d?api_key=%s&append_to_response=credits,external_ids",
		showID, c.apiKey)

	resp, err := c.httpClient.Get(u)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("TMDB TV returned HTTP %d", resp.StatusCode)
	}

	var raw struct {
		Tagline     string `json:"tagline"`
		ExternalIDs struct {
			IMDbID string `json:"imdb_id"`
		} `json:"external_ids"`
		Genres []struct {
			Name string `json:"name"`
		} `json:"genres"`
		Credits struct {
			Cast []struct {
				Name      string `json:"name"`
				Character string `json:"character"`
			} `json:"cast"`
		} `json:"credits"`
	}

	if err := json.NewDecoder(resp.Body).Decode(&raw); err != nil {
		return nil, err
	}

	res := &DetailedMetadata{
		Tagline: raw.Tagline,
		IMDbID:  raw.ExternalIDs.IMDbID,
	}
	for _, g := range raw.Genres {
		if g.Name != "" {
			res.Genres = append(res.Genres, g.Name)
		}
	}
	for i, actor := range raw.Credits.Cast {
		if i >= 10 {
			break
		}
		res.Actors = append(res.Actors, Actor{
			Name: actor.Name,
			Role: actor.Character,
		})
	}

	if resBytes, err := json.Marshal(res); err == nil {
		c.setCache(cacheKey, resBytes)
	}

	return res, nil
}

func (c *Client) DownloadImage(imagePath, targetFile string) error {
	if imagePath == "" {
		return nil
	}
	// If destination already exists and is non-empty, avoid redundant download
	if fi, err := os.Stat(targetFile); err == nil && fi.Size() > 0 {
		return nil
	}

	// Check persistent image cache if configured
	var cachedPath string
	if c.cacheDir != "" {
		imgName := strings.TrimPrefix(imagePath, "/")
		imgName = strings.ReplaceAll(imgName, "/", "_")
		cachedPath = filepath.Join(c.cacheDir, "images", imgName)
		if fi, err := os.Stat(cachedPath); err == nil && fi.Size() > 0 {
			if err := copyFile(cachedPath, targetFile); err == nil {
				return nil
			}
		}
	}

	imageURL := fmt.Sprintf("https://image.tmdb.org/t/p/original%s", imagePath)

	resp, err := c.httpClient.Get(imageURL)
	if err != nil {
		return err
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		return fmt.Errorf("image fetch failed with HTTP %d", resp.StatusCode)
	}

	if err := os.MkdirAll(filepath.Dir(targetFile), 0755); err != nil {
		return err
	}

	tmpFile := fmt.Sprintf("%s.tmp.%d", targetFile, time.Now().UnixNano())
	out, err := os.OpenFile(tmpFile, os.O_WRONLY|os.O_CREATE|os.O_TRUNC, 0644)
	if err != nil {
		return err
	}

	_, copyErr := io.Copy(out, resp.Body)
	closeErr := out.Close()
	if copyErr != nil {
		_ = os.Remove(tmpFile)
		return copyErr
	}
	if closeErr != nil {
		_ = os.Remove(tmpFile)
		return closeErr
	}

	if err := os.Rename(tmpFile, targetFile); err != nil {
		_ = os.Remove(tmpFile)
		return err
	}

	if cachedPath != "" {
		_ = copyFile(targetFile, cachedPath)
	}

	return nil
}

func copyFile(src, dst string) error {
	in, err := os.Open(src)
	if err != nil {
		return err
	}
	defer in.Close()

	if err := os.MkdirAll(filepath.Dir(dst), 0755); err != nil {
		return err
	}
	tmpDst := fmt.Sprintf("%s.tmp.%d", dst, time.Now().UnixNano())
	out, err := os.OpenFile(tmpDst, os.O_WRONLY|os.O_CREATE|os.O_TRUNC, 0644)
	if err != nil {
		return err
	}

	_, copyErr := io.Copy(out, in)
	closeErr := out.Close()
	if copyErr != nil {
		_ = os.Remove(tmpDst)
		return copyErr
	}
	if closeErr != nil {
		_ = os.Remove(tmpDst)
		return closeErr
	}

	if err := os.Rename(tmpDst, dst); err != nil {
		_ = os.Remove(tmpDst)
		return err
	}
	return nil
}

// XML-escaped NFO Data Structures
type UniqueID struct {
	Type    string `xml:"type,attr"`
	Default string `xml:"default,attr,omitempty"`
	Value   string `xml:",chardata"`
}

type Actor struct {
	Name  string `xml:"name"`
	Role  string `xml:"role,omitempty"`
	Thumb string `xml:"thumb,omitempty"`
}

type DetailedMetadata struct {
	Tagline string   `json:"tagline"`
	IMDbID  string   `json:"imdb_id"`
	Genres  []string `json:"genres"`
	Actors  []Actor  `json:"actors"`
}

type MovieNFO struct {
	XMLName  xml.Name   `xml:"movie"`
	Title    string     `xml:"title"`
	Plot     string     `xml:"plot"`
	Tagline  string     `xml:"tagline,omitempty"`
	Year     int        `xml:"year,omitempty"`
	Rating   float64    `xml:"rating,omitempty"`
	Genres   []string   `xml:"genre,omitempty"`
	Actors   []Actor    `xml:"actor,omitempty"`
	UniqueID []UniqueID `xml:"uniqueid"`
	IMDbID   string     `xml:"imdbid,omitempty"`
	ID       string     `xml:"id,omitempty"`
}

type TVShowNFO struct {
	XMLName  xml.Name   `xml:"tvshow"`
	Title    string     `xml:"title"`
	Plot     string     `xml:"plot"`
	Tagline  string     `xml:"tagline,omitempty"`
	Year     int        `xml:"year,omitempty"`
	Rating   float64    `xml:"rating,omitempty"`
	Genres   []string   `xml:"genre,omitempty"`
	Actors   []Actor    `xml:"actor,omitempty"`
	UniqueID []UniqueID `xml:"uniqueid"`
	IMDbID   string     `xml:"imdbid,omitempty"`
	ID       string     `xml:"id,omitempty"`
}

type EpisodeDetailsNFO struct {
	XMLName          xml.Name   `xml:"episodedetails"`
	Title            string     `xml:"title"`
	Season           int        `xml:"season"`
	Episode          int        `xml:"episode"`
	EpisodeNumberEnd int        `xml:"episodenumberend,omitempty"`
	Plot             string     `xml:"plot"`
	Aired            string     `xml:"aired,omitempty"`
	Rating           float64    `xml:"rating,omitempty"`
	UniqueID         []UniqueID `xml:"uniqueid"`
	IMDbID           string     `xml:"imdbid,omitempty"`
	ID               string     `xml:"id,omitempty"`
}

func GenerateMovieNFO(title, plot string, year int, rating float64, tmdbID int) string {
	return GenerateRichMovieNFO(title, plot, year, rating, tmdbID, nil)
}

func GenerateRichMovieNFO(title, plot string, year int, rating float64, tmdbID int, details *DetailedMetadata) string {
	uids := []UniqueID{}
	if tmdbID > 0 {
		uids = append(uids, UniqueID{
			Type:    "tmdb",
			Default: "true",
			Value:   strconv.Itoa(tmdbID),
		})
	}
	var imdbID string
	var genres []string
	var actors []Actor
	var tagline string
	if details != nil {
		tagline = details.Tagline
		genres = details.Genres
		actors = details.Actors
		if details.IMDbID != "" {
			imdbID = details.IMDbID
			uids = append(uids, UniqueID{
				Type:  "imdb",
				Value: details.IMDbID,
			})
		}
	}
	nfo := MovieNFO{
		Title:    title,
		Plot:     plot,
		Tagline:  tagline,
		Year:     year,
		Rating:   rating,
		Genres:   genres,
		Actors:   actors,
		UniqueID: uids,
		IMDbID:   imdbID,
		ID:       imdbID,
	}
	data, err := xml.MarshalIndent(nfo, "", "    ")
	if err != nil {
		return ""
	}
	return "<?xml version=\"1.0\" encoding=\"UTF-8\" standalone=\"yes\"?>\n" + string(data)
}

func GenerateShowNFO(title, plot string, year int, rating float64, tmdbID int) string {
	return GenerateRichShowNFO(title, plot, year, rating, tmdbID, nil)
}

func GenerateRichShowNFO(title, plot string, year int, rating float64, tmdbID int, details *DetailedMetadata) string {
	uids := []UniqueID{}
	if tmdbID > 0 {
		uids = append(uids, UniqueID{
			Type:    "tmdb",
			Default: "true",
			Value:   strconv.Itoa(tmdbID),
		})
	}
	var imdbID string
	var genres []string
	var actors []Actor
	var tagline string
	if details != nil {
		tagline = details.Tagline
		genres = details.Genres
		actors = details.Actors
		if details.IMDbID != "" {
			imdbID = details.IMDbID
			uids = append(uids, UniqueID{
				Type:  "imdb",
				Value: details.IMDbID,
			})
		}
	}
	nfo := TVShowNFO{
		Title:    title,
		Plot:     plot,
		Tagline:  tagline,
		Year:     year,
		Rating:   rating,
		Genres:   genres,
		Actors:   actors,
		UniqueID: uids,
		IMDbID:   imdbID,
		ID:       imdbID,
	}
	data, err := xml.MarshalIndent(nfo, "", "    ")
	if err != nil {
		return ""
	}
	return "<?xml version=\"1.0\" encoding=\"UTF-8\" standalone=\"yes\"?>\n" + string(data)
}

func GenerateEpisodeNFO(title string, season, episode int, plot, airDate string, rating float64, tmdbEpisodeID int) string {
	return GenerateRichEpisodeNFO(title, season, episode, plot, airDate, rating, tmdbEpisodeID, 0, "")
}

func GenerateRichEpisodeNFO(title string, season, episode int, plot, airDate string, rating float64, tmdbEpisodeID int, seriesTMDBID int, imdbID string) string {
	uids := []UniqueID{}
	if tmdbEpisodeID > 0 {
		uids = append(uids, UniqueID{
			Type:    "tmdb",
			Default: "true",
			Value:   strconv.Itoa(tmdbEpisodeID),
		})
	}
	if seriesTMDBID > 0 {
		uids = append(uids, UniqueID{
			Type:  "tmdb_series",
			Value: strconv.Itoa(seriesTMDBID),
		})
	}
	if imdbID != "" {
		uids = append(uids, UniqueID{
			Type:  "imdb",
			Value: imdbID,
		})
	}
	nfo := EpisodeDetailsNFO{
		Title:    title,
		Season:   season,
		Episode:  episode,
		Plot:     plot,
		Aired:    airDate,
		Rating:   rating,
		UniqueID: uids,
		IMDbID:   imdbID,
		ID:       imdbID,
	}
	data, err := xml.MarshalIndent(nfo, "", "    ")
	if err != nil {
		return ""
	}
	return "<?xml version=\"1.0\" encoding=\"UTF-8\" standalone=\"yes\"?>\n" + string(data)
}

func GenerateMultiEpisodeNFO(episodes []EpisodeDetailsNFO) string {
	if len(episodes) == 0 {
		return ""
	}
	if len(episodes) == 1 {
		data, err := xml.MarshalIndent(episodes[0], "", "    ")
		if err != nil {
			return ""
		}
		return "<?xml version=\"1.0\" encoding=\"UTF-8\" standalone=\"yes\"?>\n" + string(data)
	}

	first := episodes[0]
	last := episodes[len(episodes)-1]

	var titleParts []string
	var plotParts []string
	var totalRating float64
	var ratingCount int

	for _, ep := range episodes {
		if ep.Title != "" {
			titleParts = append(titleParts, ep.Title)
		}
		if ep.Plot != "" {
			plotParts = append(plotParts, fmt.Sprintf("Episode %d: %s", ep.Episode, ep.Plot))
		}
		if ep.Rating > 0 {
			totalRating += ep.Rating
			ratingCount++
		}
	}

	avgRating := first.Rating
	if ratingCount > 0 {
		avgRating = totalRating / float64(ratingCount)
	}

	combinedTitle := strings.Join(titleParts, " / ")
	if combinedTitle == "" {
		combinedTitle = fmt.Sprintf("Episodes %d-%d", first.Episode, last.Episode)
	}
	combinedPlot := strings.Join(plotParts, "\n\n")

	composite := EpisodeDetailsNFO{
		Title:            combinedTitle,
		Season:           first.Season,
		Episode:          first.Episode,
		EpisodeNumberEnd: last.Episode,
		Plot:             combinedPlot,
		Aired:            first.Aired,
		Rating:           avgRating,
		UniqueID:         first.UniqueID,
		IMDbID:           first.IMDbID,
		ID:               first.ID,
	}

	data, err := xml.MarshalIndent(composite, "", "    ")
	if err != nil {
		return ""
	}
	return "<?xml version=\"1.0\" encoding=\"UTF-8\" standalone=\"yes\"?>\n" + string(data)
}
