package metadata

import (
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"time"
)

type Client struct {
	apiKey     string
	httpClient *http.Client
}

type TMDBResult struct {
	ID           int     `json:"id"`
	Title        string  `json:"title"`
	Name         string  `json:"name"`
	Overview     string  `json:"overview"`
	PosterPath   string  `json:"poster_path"`
	BackdropPath string  `json:"backdrop_path"`
	ReleaseDate  string  `json:"release_date"`
	FirstAirDate string  `json:"first_air_date"`
	VoteAverage  float64 `json:"vote_average"`
}

type TMDBSearchResponse struct {
	Results []TMDBResult `json:"results"`
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
		// Fallback: search without year in case the year in filename is slightly offset
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

	return &searchResp.Results[0], nil
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

func GenerateNFO(title, plot string, year int, rating float64, tmdbID int, mediaType string) string {
	if mediaType == "series" {
		return fmt.Sprintf(`<?xml version="1.0" encoding="UTF-8" standalone="yes" ?>
<tvshow>
    <title>%s</title>
    <plot>%s</plot>
    <year>%d</year>
    <rating>%.1f</rating>
    <uniqueid type="tmdb" default="true">%d</uniqueid>
</tvshow>`, title, plot, year, rating, tmdbID)
	}

	return fmt.Sprintf(`<?xml version="1.0" encoding="UTF-8" standalone="yes" ?>
<movie>
    <title>%s</title>
    <plot>%s</plot>
    <year>%d</year>
    <rating>%.1f</rating>
    <uniqueid type="tmdb" default="true">%d</uniqueid>
</movie>`, title, plot, year, rating, tmdbID)
}
