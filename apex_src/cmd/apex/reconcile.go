package main

import (
	"database/sql"
	"log"
	"os"
	"path/filepath"
	"strconv"
	"strings"

	"apex/internal/db"
)

func reconcileSTRMFiles(mediaRoot string, database *db.Database) {
	log.Printf("[Apex] Starting reconciliation of STRM files in %s", mediaRoot)
	deletedCount := 0
	migratedCount := 0

	err := filepath.Walk(mediaRoot, func(path string, info os.FileInfo, err error) error {
		if err != nil {
			return err
		}
		if !info.IsDir() && strings.HasSuffix(info.Name(), ".strm") {
			content, err := os.ReadFile(path)
			if err != nil {
				return nil
			}
			urlStr := strings.TrimSpace(string(content))
			parts := strings.Split(urlStr, "/stream/")
			if len(parts) == 2 {
				target := parts[1]
				subParts := strings.Split(target, "/")
				
				var item *db.MediaItem
				var dbErr error
				var isOldFormat bool

				if len(subParts) >= 3 {
					// New format: {chatID}/{msgID}/filename
					chatID, _ := strconv.ParseInt(subParts[0], 10, 64)
					msgID, _ := strconv.Atoi(subParts[1])
					item, dbErr = database.GetMediaItemByMessageID(chatID, msgID)
				} else {
					// Old format: apx_...
					isOldFormat = true
					item, dbErr = database.GetMediaItem(target)
				}

				if dbErr == sql.ErrNoRows || (dbErr == nil && item == nil) {
					log.Printf("[Apex] Cleanup: Removing orphaned STRM file for nonexistent media %s: %s", target, path)
					os.Remove(path)
					dir := filepath.Dir(path)
					if dir != mediaRoot && dir != filepath.Join(mediaRoot, "Movies") && dir != filepath.Join(mediaRoot, "Shows") {
						_ = os.Remove(dir)
					}
					deletedCount++
				} else if isOldFormat && item != nil && dbErr == nil {
					// Migrate old format to new format
					newURL := "http://127.0.0.1:8084/stream/" + strconv.FormatInt(item.SourceChatID, 10) + "/" + strconv.Itoa(item.MessageID) + "/video.mp4"
					_ = os.WriteFile(path, []byte(newURL), 0644)
					migratedCount++
				}
			}
		}
		return nil
	})

	if err != nil {
		log.Printf("[Apex] Error during STRM reconciliation: %v", err)
	} else if deletedCount > 0 || migratedCount > 0 {
		log.Printf("[Apex] STRM reconciliation complete. Removed %d orphaned files, Migrated %d files.", deletedCount, migratedCount)
	}
}
