package db

import (
	"database/sql"
)

func (d *Database) GetMediaItemByMessageID(chatID int64, messageID int) (*MediaItem, error) {
	query := `
	SELECT id, source_chat_id, message_id, file_id, file_unique_id, file_reference,
		   access_hash, file_size, mime_type, clean_title, media_type, year, season,
		   episode, tmdb_id, strm_path, created_at
	FROM media_items WHERE source_chat_id = ? AND message_id = ? LIMIT 1
	`
	row := d.conn.QueryRow(query, chatID, messageID)

	var item MediaItem
	err := row.Scan(
		&item.ID, &item.SourceChatID, &item.MessageID, &item.FileID, &item.FileUniqueID,
		&item.FileRef, &item.AccessHash, &item.FileSize, &item.MimeType, &item.CleanTitle,
		&item.MediaType, &item.Year, &item.Season, &item.Episode, &item.TMDBID,
		&item.StrmPath, &item.CreatedAt,
	)
	if err == sql.ErrNoRows {
		return nil, nil
	}
	return &item, err
}
