package db

import (
	"database/sql"
	"fmt"
	"os"
	"path/filepath"
	"time"

	_ "modernc.org/sqlite"
)

type Database struct {
	conn *sql.DB
}

type TelegramSource struct {
	ID            string
	ChatID        int64
	Title         string
	Category      string
	Enabled       bool
	LastMessageID int
	CreatedAt     time.Time
	UpdatedAt     time.Time
}

type Movie struct {
	ID            string
	Title         string
	OriginalTitle string
	Year          int
	TMDBID        int
	Overview      string
	Rating        float64
	PosterPath    string
	BackdropPath  string
	StrmPath      string
	CreatedAt     time.Time
}

type Series struct {
	ID            string
	Title         string
	OriginalTitle string
	Year          int
	TMDBID        int
	Overview      string
	Rating        float64
	PosterPath    string
	BackdropPath  string
	CreatedAt     time.Time
}

type Season struct {
	ID           string
	SeriesID     string
	SeasonNumber int
	Title        string
	Overview     string
	PosterPath   string
}

type Episode struct {
	ID            string
	SeriesID      string
	SeasonID      string
	SeasonNumber  int
	EpisodeNumber int
	Title         string
	Overview      string
	AirDate       string
	Rating        float64
	StillPath     string
	StrmPath      string
	CreatedAt     time.Time
}

type MediaItem struct {
	ID           string
	SourceChatID int64
	MessageID    int
	FileID       string
	FileUniqueID string
	FileRef      []byte
	AccessHash   int64
	FileSize     int64
	MimeType     string
	CleanTitle   string
	MediaType    string // "movie" or "series"
	Year         int
	Season       int
	Episode      int
	TMDBID       int
	StrmPath     string
	CreatedAt    time.Time
}

type MediaCapability struct {
	MediaID         string
	Container       string
	VideoCodec      string
	VideoProfile    string
	Width           int
	Height          int
	FPS             float64
	BitDepth        int
	HDR             string
	AudioCodec      string
	AudioChannels   int
	SubtitleTypes   string
	Bitrate         int
	DirectPlaySafe  bool
	TranscodeReason string
}

type ProcessingJob struct {
	ID        string
	ChatID    int64
	MessageID int
	Filename  string
	Caption   string
	Status    string // "queued", "processing", "completed", "failed"
	Error     string
	CreatedAt time.Time
	UpdatedAt time.Time
}

func Open(dbPath string) (*Database, error) {
	if err := os.MkdirAll(filepath.Dir(dbPath), 0755); err != nil {
		return nil, fmt.Errorf("failed to create db directory: %w", err)
	}

	conn, err := sql.Open("sqlite", dbPath+"?_pragma=journal_mode(WAL)&_pragma=synchronous(NORMAL)&_pragma=foreign_keys(1)")
	if err != nil {
		return nil, fmt.Errorf("failed to open sqlite database: %w", err)
	}

	d := &Database{conn: conn}
	if err := d.migrate(); err != nil {
		_ = conn.Close()
		return nil, fmt.Errorf("failed to migrate database: %w", err)
	}

	return d, nil
}

func (d *Database) Close() error {
	return d.conn.Close()
}

func (d *Database) migrate() error {
	query := `
	CREATE TABLE IF NOT EXISTS telegram_sources (
		id TEXT PRIMARY KEY,
		chat_id INTEGER UNIQUE NOT NULL,
		title TEXT NOT NULL,
		category TEXT NOT NULL,
		enabled BOOLEAN NOT NULL DEFAULT 1,
		last_message_id INTEGER NOT NULL DEFAULT 0,
		created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
		updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
	);

	CREATE TABLE IF NOT EXISTS movies (
		id TEXT PRIMARY KEY,
		title TEXT NOT NULL,
		original_title TEXT,
		year INTEGER,
		tmdb_id INTEGER,
		overview TEXT,
		rating REAL,
		poster_path TEXT,
		backdrop_path TEXT,
		strm_path TEXT NOT NULL,
		created_at DATETIME DEFAULT CURRENT_TIMESTAMP
	);

	CREATE TABLE IF NOT EXISTS series (
		id TEXT PRIMARY KEY,
		title TEXT NOT NULL,
		original_title TEXT,
		year INTEGER,
		tmdb_id INTEGER,
		overview TEXT,
		rating REAL,
		poster_path TEXT,
		backdrop_path TEXT,
		created_at DATETIME DEFAULT CURRENT_TIMESTAMP
	);

	CREATE TABLE IF NOT EXISTS seasons (
		id TEXT PRIMARY KEY,
		series_id TEXT NOT NULL,
		season_number INTEGER NOT NULL,
		title TEXT,
		overview TEXT,
		poster_path TEXT,
		FOREIGN KEY (series_id) REFERENCES series(id) ON DELETE CASCADE,
		UNIQUE(series_id, season_number)
	);

	CREATE TABLE IF NOT EXISTS episodes (
		id TEXT PRIMARY KEY,
		series_id TEXT NOT NULL,
		season_id TEXT NOT NULL,
		season_number INTEGER NOT NULL,
		episode_number INTEGER NOT NULL,
		title TEXT,
		overview TEXT,
		air_date TEXT,
		rating REAL,
		still_path TEXT,
		strm_path TEXT NOT NULL,
		created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
		FOREIGN KEY (series_id) REFERENCES series(id) ON DELETE CASCADE,
		FOREIGN KEY (season_id) REFERENCES seasons(id) ON DELETE CASCADE,
		UNIQUE(series_id, season_number, episode_number)
	);

	CREATE TABLE IF NOT EXISTS media_items (
		id TEXT PRIMARY KEY,
		source_chat_id INTEGER NOT NULL,
		message_id INTEGER NOT NULL,
		file_id TEXT NOT NULL,
		file_unique_id TEXT NOT NULL,
		file_reference BLOB NOT NULL,
		access_hash INTEGER NOT NULL,
		file_size INTEGER NOT NULL,
		mime_type TEXT NOT NULL,
		clean_title TEXT NOT NULL,
		media_type TEXT NOT NULL,
		year INTEGER,
		season INTEGER,
		episode INTEGER,
		tmdb_id INTEGER,
		strm_path TEXT NOT NULL,
		created_at DATETIME DEFAULT CURRENT_TIMESTAMP
	);

	CREATE INDEX IF NOT EXISTS idx_media_items_unique ON media_items(source_chat_id, message_id);

	CREATE TABLE IF NOT EXISTS media_capabilities (
		media_id TEXT PRIMARY KEY,
		container TEXT,
		video_codec TEXT,
		video_profile TEXT,
		width INTEGER,
		height INTEGER,
		fps REAL,
		bit_depth INTEGER,
		hdr TEXT,
		audio_codec TEXT,
		audio_channels INTEGER,
		subtitle_types TEXT,
		bitrate INTEGER,
		direct_play_safe BOOLEAN NOT NULL DEFAULT 1,
		transcode_reason TEXT,
		FOREIGN KEY (media_id) REFERENCES media_items(id) ON DELETE CASCADE
	);

	CREATE TABLE IF NOT EXISTS processing_jobs (
		id TEXT PRIMARY KEY,
		chat_id INTEGER NOT NULL,
		message_id INTEGER NOT NULL,
		filename TEXT NOT NULL,
		caption TEXT,
		status TEXT NOT NULL,
		error TEXT,
		created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
		updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
	);
	`
	_, err := d.conn.Exec(query)
	return err
}

func (d *Database) SaveMovie(m *Movie) error {
	query := `
	INSERT INTO movies (id, title, original_title, year, tmdb_id, overview, rating, poster_path, backdrop_path, strm_path)
	VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
	ON CONFLICT(id) DO UPDATE SET
		title=excluded.title,
		overview=excluded.overview,
		rating=excluded.rating,
		poster_path=excluded.poster_path,
		backdrop_path=excluded.backdrop_path;
	`
	_, err := d.conn.Exec(query, m.ID, m.Title, m.OriginalTitle, m.Year, m.TMDBID, m.Overview, m.Rating, m.PosterPath, m.BackdropPath, m.StrmPath)
	return err
}

func (d *Database) SaveSeries(s *Series) error {
	query := `
	INSERT INTO series (id, title, original_title, year, tmdb_id, overview, rating, poster_path, backdrop_path)
	VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
	ON CONFLICT(id) DO UPDATE SET
		title=excluded.title,
		overview=excluded.overview,
		rating=excluded.rating,
		poster_path=excluded.poster_path,
		backdrop_path=excluded.backdrop_path;
	`
	_, err := d.conn.Exec(query, s.ID, s.Title, s.OriginalTitle, s.Year, s.TMDBID, s.Overview, s.Rating, s.PosterPath, s.BackdropPath)
	return err
}

func (d *Database) SaveEpisode(ep *Episode) error {
	query := `
	INSERT INTO episodes (id, series_id, season_id, season_number, episode_number, title, overview, air_date, rating, still_path, strm_path)
	VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
	ON CONFLICT(series_id, season_number, episode_number) DO UPDATE SET
		title=excluded.title,
		overview=excluded.overview,
		rating=excluded.rating,
		still_path=excluded.still_path,
		strm_path=excluded.strm_path;
	`
	_, err := d.conn.Exec(query, ep.ID, ep.SeriesID, ep.SeasonID, ep.SeasonNumber, ep.EpisodeNumber, ep.Title, ep.Overview, ep.AirDate, ep.Rating, ep.StillPath, ep.StrmPath)
	return err
}

func (d *Database) SaveMediaItem(item *MediaItem) error {
	query := `
	INSERT INTO media_items (
		id, source_chat_id, message_id, file_id, file_unique_id, file_reference,
		access_hash, file_size, mime_type, clean_title, media_type, year, season,
		episode, tmdb_id, strm_path
	) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
	ON CONFLICT(id) DO UPDATE SET
		file_reference=excluded.file_reference,
		access_hash=excluded.access_hash,
		file_size=excluded.file_size;
	`
	_, err := d.conn.Exec(query,
		item.ID, item.SourceChatID, item.MessageID, item.FileID, item.FileUniqueID,
		item.FileRef, item.AccessHash, item.FileSize, item.MimeType, item.CleanTitle,
		item.MediaType, item.Year, item.Season, item.Episode, item.TMDBID, item.StrmPath,
	)
	return err
}

func (d *Database) GetMediaItem(id string) (*MediaItem, error) {
	query := `
	SELECT id, source_chat_id, message_id, file_id, file_unique_id, file_reference,
		   access_hash, file_size, mime_type, clean_title, media_type, year, season,
		   episode, tmdb_id, strm_path, created_at
	FROM media_items WHERE id = ?
	`
	row := d.conn.QueryRow(query, id)

	var item MediaItem
	err := row.Scan(
		&item.ID, &item.SourceChatID, &item.MessageID, &item.FileID, &item.FileUniqueID,
		&item.FileRef, &item.AccessHash, &item.FileSize, &item.MimeType, &item.CleanTitle,
		&item.MediaType, &item.Year, &item.Season, &item.Episode, &item.TMDBID,
		&item.StrmPath, &item.CreatedAt,
	)
	if err != nil {
		return nil, err
	}
	return &item, nil
}

func (d *Database) UpdateFileReference(id string, newRef []byte, accessHash int64) error {
	query := `UPDATE media_items SET file_reference = ?, access_hash = ? WHERE id = ?`
	_, err := d.conn.Exec(query, newRef, accessHash, id)
	return err
}

func (d *Database) SaveCapabilities(cap *MediaCapability) error {
	query := `
	INSERT INTO media_capabilities (
		media_id, container, video_codec, video_profile, width, height, fps,
		bit_depth, hdr, audio_codec, audio_channels, subtitle_types, bitrate,
		direct_play_safe, transcode_reason
	) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
	ON CONFLICT(media_id) DO UPDATE SET
		container=excluded.container,
		video_codec=excluded.video_codec,
		video_profile=excluded.video_profile,
		width=excluded.width,
		height=excluded.height,
		fps=excluded.fps,
		bit_depth=excluded.bit_depth,
		hdr=excluded.hdr,
		audio_codec=excluded.audio_codec,
		audio_channels=excluded.audio_channels,
		subtitle_types=excluded.subtitle_types,
		bitrate=excluded.bitrate,
		direct_play_safe=excluded.direct_play_safe,
		transcode_reason=excluded.transcode_reason;
	`
	_, err := d.conn.Exec(query,
		cap.MediaID, cap.Container, cap.VideoCodec, cap.VideoProfile, cap.Width, cap.Height,
		cap.FPS, cap.BitDepth, cap.HDR, cap.AudioCodec, cap.AudioChannels, cap.SubtitleTypes,
		cap.Bitrate, cap.DirectPlaySafe, cap.TranscodeReason,
	)
	return err
}

func (d *Database) SaveJob(job *ProcessingJob) error {
	query := `
	INSERT INTO processing_jobs (id, chat_id, message_id, filename, caption, status, error, updated_at)
	VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
	ON CONFLICT(id) DO UPDATE SET
		status=excluded.status,
		error=excluded.error,
		updated_at=CURRENT_TIMESTAMP;
	`
	_, err := d.conn.Exec(query, job.ID, job.ChatID, job.MessageID, job.Filename, job.Caption, job.Status, job.Error)
	return err
}
