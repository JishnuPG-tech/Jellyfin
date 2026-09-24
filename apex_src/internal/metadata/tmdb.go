package metadata

import (
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
	"time"
)

type Client struct {
	apiKey     string
	httpClient *http.Client
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

func NewClient(apiKey string) *Client {
	return &Client{
		apiKey: apiKey,
		httpClient: &http.Client{
			Timeout: 10 * time.Second,
		},
	}
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
			highestScore = score
			bestIdx = i
		}
	}

	res := searchResp.Results[bestIdx]
	res.Confidence = highestScore
	return &res, nil
}

func (c *Client) GetEpisodeDetails(seriesID int, seasonNumber int, episodeNumber int) (*TMDBEpisode, error) {
	if c.apiKey == "" {
		return nil, fmt.Errorf("TMDB_API_KEY is not configured")
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

	return &ep, nil
}

func (c *Client) DownloadImage(imagePath, targetFile string) error {
	if imagePath == "" {
		return nil
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

	out, err := os.Create(targetFile)
	if err != nil {
		return err
	}
	defer out.Close()

	_, err = io.Copy(out, resp.Body)
	return err
}

// XML-escaped NFO Data Structures
type UniqueID struct {
	Type    string `xml:"type,attr"`
	Default string `xml:"default,attr"`
	Value   int    `xml:",chardata"`
}

type MovieNFO struct {
	XMLName  xml.Name `xml:"movie"`
	Title    string   `xml:"title"`
	Plot     string   `xml:"plot"`
	Year     int      `xml:"year,omitempty"`
	Rating   float64  `xml:"rating,omitempty"`
	UniqueID UniqueID `xml:"uniqueid"`
}

type TVShowNFO struct {
	XMLName  xml.Name `xml:"tvshow"`
	Title    string   `xml:"title"`
	Plot     string   `xml:"plot"`
	Year     int      `xml:"year,omitempty"`
	Rating   float64  `xml:"rating,omitempty"`
	UniqueID UniqueID `xml:"uniqueid"`
}

type EpisodeDetailsNFO struct {
	XMLName  xml.Name `xml:"episodedetails"`
	Title    string   `xml:"title"`
	Season   int      `xml:"season"`
	Episode  int      `xml:"episode"`
	Plot     string   `xml:"plot"`
	Aired    string   `xml:"aired,omitempty"`
	Rating   float64  `xml:"rating,omitempty"`
	UniqueID UniqueID `xml:"uniqueid"`
}

func GenerateMovieNFO(title, plot string, year int, rating float64, tmdbID int) string {
	nfo := MovieNFO{
		Title:  title,
		Plot:   plot,
		Year:   year,
		Rating: rating,
		UniqueID: UniqueID{
			Type:    "tmdb",
			Default: "true",
			Value:   tmdbID,
		},
	}
	data, err := xml.MarshalIndent(nfo, "", "    ")
	if err != nil {
		return ""
	}
	return "<?xml version=\"1.0\" encoding=\"UTF-8\" standalone=\"yes\"?>\n" + string(data)
}

func GenerateShowNFO(title, plot string, year int, rating float64, tmdbID int) string {
	nfo := TVShowNFO{
		Title:  title,
		Plot:   plot,
		Year:   year,
		Rating: rating,
		UniqueID: UniqueID{
			Type:    "tmdb",
			Default: "true",
			Value:   tmdbID,
		},
	}
	data, err := xml.MarshalIndent(nfo, "", "    ")
	if err != nil {
		return ""
	}
	return "<?xml version=\"1.0\" encoding=\"UTF-8\" standalone=\"yes\"?>\n" + string(data)
}

func GenerateEpisodeNFO(title string, season, episode int, plot, airDate string, rating float64, tmdbID int) string {
	nfo := EpisodeDetailsNFO{
		Title:   title,
		Season:  season,
		Episode: episode,
		Plot:    plot,
		Aired:   airDate,
		Rating:  rating,
		UniqueID: UniqueID{
			Type:    "tmdb",
			Default: "true",
			Value:   tmdbID,
		},
	}
	data, err := xml.MarshalIndent(nfo, "", "    ")
	if err != nil {
		return ""
	}
	return "<?xml version=\"1.0\" encoding=\"UTF-8\" standalone=\"yes\"?>\n" + string(data)
}
