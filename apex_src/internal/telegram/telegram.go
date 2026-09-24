package telegram

import (
	"context"
	"fmt"
	"log"
	"math/rand"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"time"

	"apex/internal/config"
	"apex/internal/db"

	"github.com/gotd/td/telegram"
	"github.com/gotd/td/tg"
	"github.com/gotd/td/tgerr"
)

type Manager struct {
	cfg          *config.Config
	database     *db.Database
	updateClient *telegram.Client
	transferPool []*telegram.Client
	poolIndex    int
	poolMu       sync.Mutex
	rawAPI       *tg.Client
}

func NewManager(cfg *config.Config, database *db.Database) *Manager {
	return &Manager{
		cfg:      cfg,
		database: database,
	}
}

func isVideoExtension(filename string) bool {
	ext := strings.ToLower(filepath.Ext(filename))
	switch ext {
	case ".mp4", ".mkv", ".avi", ".mov", ".webm", ".ts", ".m4v", ".flv", ".wmv", ".iso", ".mpg", ".mpeg":
		return true
	}
	return false
}

func (m *Manager) sendReply(peer tg.InputPeerClass, text string) {
	if peer == nil || m.rawAPI == nil {
		return
	}
	go func() {
		ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		_, err := m.rawAPI.MessagesSendMessage(ctx, &tg.MessagesSendMessageRequest{
			Peer:     peer,
			Message:  text,
			RandomID: rand.Int63(),
		})
		if err != nil {
			log.Printf("[Telegram] Note: Reply send skipped: %v", err)
		}
	}()
}

func (m *Manager) Start(ctx context.Context, onNewMedia func(ctx context.Context, doc *tg.Document, chatID int64, msgID int, caption string)) error {
	if m.cfg.TelegramAPIID == 0 || m.cfg.TelegramAPIHash == "" || m.cfg.TelegramBotToken == "" {
		log.Printf("[Telegram] Warning: Telegram credentials not fully configured. Ingestion disabled.")
		return nil
	}

	sessionDir := filepath.Dir(m.cfg.SessionFilePath)
	_ = os.MkdirAll(sessionDir, 0700)

	dispatcher := tg.NewUpdateDispatcher()

	processMessage := func(ctx context.Context, e tg.Entities, msgClass tg.MessageClass) error {
		msg, ok := msgClass.(*tg.Message)
		if !ok {
			return nil
		}

		var chatID int64
		var inputPeer tg.InputPeerClass
		peerDesc := "unknown"

		switch peer := msg.PeerID.(type) {
		case *tg.PeerUser:
			chatID = peer.UserID
			peerDesc = fmt.Sprintf("User(%d)", peer.UserID)
			if u, exists := e.Users[peer.UserID]; exists {
				inputPeer = &tg.InputPeerUser{UserID: peer.UserID, AccessHash: u.AccessHash}
			}
		case *tg.PeerChat:
			chatID = peer.ChatID
			peerDesc = fmt.Sprintf("Chat(%d)", peer.ChatID)
			inputPeer = &tg.InputPeerChat{ChatID: peer.ChatID}
		case *tg.PeerChannel:
			chatID = peer.ChannelID
			peerDesc = fmt.Sprintf("Channel(%d)", peer.ChannelID)
			if c, exists := e.Channels[peer.ChannelID]; exists {
				inputPeer = &tg.InputPeerChannel{ChannelID: peer.ChannelID, AccessHash: c.AccessHash}
			}
		}

		// Check if message is purely text without media
		if msg.Media == nil {
			text := strings.TrimSpace(msg.Message)
			if text != "" {
				log.Printf("[Telegram] Received text message #%d from %s: %s", msg.ID, peerDesc, text)
				if strings.HasPrefix(text, "/start") || strings.HasPrefix(text, "/help") {
					m.sendReply(inputPeer, "🎬 Apex Cloud Platform\n\nSend or forward any movie or TV series video file here.\nIt will be cataloged instantly and made available to stream on Jellyfin!")
				}
			}
			return nil
		}

		// Inspect media for documents or videos
		docMedia, ok := msg.Media.(*tg.MessageMediaDocument)
		if !ok {
			log.Printf("[Telegram] Message #%d from %s has non-document media (%T)", msg.ID, peerDesc, msg.Media)
			return nil
		}

		doc, ok := docMedia.Document.(*tg.Document)
		if !ok {
			log.Printf("[Telegram] Message #%d from %s has empty document", msg.ID, peerDesc)
			return nil
		}

		// Extract filename & video flag
		var filename string
		var isVideo bool
		for _, attr := range doc.Attributes {
			switch a := attr.(type) {
			case *tg.DocumentAttributeFilename:
				filename = a.FileName
			case *tg.DocumentAttributeVideo:
				isVideo = true
			}
		}

		caption := strings.TrimSpace(msg.Message)
		if filename == "" {
			if caption != "" {
				filename = caption
				if !strings.Contains(filename, ".") {
					filename += ".mp4"
				}
			} else {
				filename = fmt.Sprintf("media_%d.mp4", doc.ID)
			}
		}

		if strings.HasPrefix(doc.MimeType, "video/") || isVideo || isVideoExtension(filename) {
			log.Printf("[Telegram] Ingesting video media: '%s' (size: %d MB, mime: %s) from %s msg #%d",
				filename, doc.Size/(1024*1024), doc.MimeType, peerDesc, msg.ID)

			m.sendReply(inputPeer, fmt.Sprintf("📥 Ingesting '%s'...\nAdding to your Jellyfin library.", filename))

			onNewMedia(ctx, doc, chatID, msg.ID, caption)
		} else {
			log.Printf("[Telegram] Ignored non-video document: '%s' (mime: %s) from %s msg #%d",
				filename, doc.MimeType, peerDesc, msg.ID)
		}

		return nil
	}

	// Register update handlers for all message arrival vectors
	dispatcher.OnNewMessage(func(ctx context.Context, e tg.Entities, u *tg.UpdateNewMessage) error {
		return processMessage(ctx, e, u.Message)
	})
	dispatcher.OnNewChannelMessage(func(ctx context.Context, e tg.Entities, u *tg.UpdateNewChannelMessage) error {
		return processMessage(ctx, e, u.Message)
	})
	dispatcher.OnEditMessage(func(ctx context.Context, e tg.Entities, u *tg.UpdateEditMessage) error {
		return processMessage(ctx, e, u.Message)
	})
	dispatcher.OnEditChannelMessage(func(ctx context.Context, e tg.Entities, u *tg.UpdateEditChannelMessage) error {
		return processMessage(ctx, e, u.Message)
	})

	updateOpts := telegram.Options{
		SessionStorage: &telegram.FileSessionStorage{
			Path: m.cfg.SessionFilePath,
		},
		UpdateHandler: dispatcher,
	}

	m.updateClient = telegram.NewClient(m.cfg.TelegramAPIID, m.cfg.TelegramAPIHash, updateOpts)

	// Start update client in background
	go func() {
		err := m.updateClient.Run(ctx, func(ctx context.Context) error {
			status, err := m.updateClient.Auth().Status(ctx)
			if err != nil {
				return err
			}
			if !status.Authorized {
				if _, err := m.updateClient.Auth().Bot(ctx, m.cfg.TelegramBotToken); err != nil {
					return fmt.Errorf("bot auth failed: %w", err)
				}
			}

			m.rawAPI = m.updateClient.API()
			log.Printf("[Telegram] Persistent MTProto update connection established.")
			<-ctx.Done()
			return nil
		})
		if err != nil {
			log.Printf("[Telegram] Update connection error: %v", err)
		}
	}()

	// Initialize Media Transfer Pool (Default: 2 clients)
	numClients := m.cfg.MediaClients
	if numClients <= 0 {
		numClients = 2
	}

	log.Printf("[Telegram] Initializing %d dedicated media transfer connections...", numClients)
	for i := 0; i < numClients; i++ {
		c := telegram.NewClient(m.cfg.TelegramAPIID, m.cfg.TelegramAPIHash, telegram.Options{})
		m.transferPool = append(m.transferPool, c)
		clientIdx := i
		go func(client *telegram.Client, idx int) {
			err := client.Run(ctx, func(ctx context.Context) error {
				_, authErr := client.Auth().Bot(ctx, m.cfg.TelegramBotToken)
				if authErr != nil {
					log.Printf("[Telegram] Media transfer connection #%d auth error: %v", idx+1, authErr)
					return authErr
				}
				log.Printf("[Telegram] Media transfer connection #%d active.", idx+1)
				<-ctx.Done()
				return nil
			})
			if err != nil {
				log.Printf("[Telegram] Media transfer connection #%d error: %v", idx+1, err)
			}
		}(c, clientIdx)
	}

	return nil
}

func (m *Manager) getTransferClient() *telegram.Client {
	m.poolMu.Lock()
	defer m.poolMu.Unlock()
	if len(m.transferPool) == 0 {
		return m.updateClient
	}
	c := m.transferPool[m.poolIndex]
	m.poolIndex = (m.poolIndex + 1) % len(m.transferPool)
	return c
}

func (m *Manager) FetchChunk(ctx context.Context, item *db.MediaItem, offset int64, limit int) ([]byte, error) {
	docID, _ := strconv.ParseInt(item.FileID, 10, 64)

	for attempt := 0; attempt < 3; attempt++ {
		client := m.getTransferClient()
		raw := client.API()

		location := &tg.InputDocumentFileLocation{
			ID:            docID,
			AccessHash:    item.AccessHash,
			FileReference: item.FileRef,
		}

		res, err := raw.UploadGetFile(ctx, &tg.UploadGetFileRequest{
			Location: location,
			Offset:   offset,
			Limit:    limit,
		})

		if err == nil {
			switch file := res.(type) {
			case *tg.UploadFile:
				return file.Bytes, nil
			case *tg.UploadFileCDNRedirect:
				return nil, fmt.Errorf("CDN redirect not supported")
			}
			return nil, fmt.Errorf("unexpected file response type")
		}

		// Handle FLOOD_WAIT_X
		if d, ok := tgerr.AsFloodWait(err); ok {
			jitter := time.Duration(rand.Intn(500)) * time.Millisecond
			sleepDuration := d + jitter
			log.Printf("[Telegram] FLOOD_WAIT received. Sleeping for %v", sleepDuration)
			select {
			case <-time.After(sleepDuration):
				continue
			case <-ctx.Done():
				return nil, ctx.Err()
			}
		}

		// Handle FILE_REFERENCE_EXPIRED
		if strings.Contains(err.Error(), "FILE_REFERENCE_EXPIRED") || strings.Contains(err.Error(), "FILE_REFERENCE_INVALID") {
			log.Printf("[Telegram] File reference expired for media %s. Refreshing file reference...", item.ID)
			refreshed, refErr := m.refreshFileReference(ctx, item)
			if refErr == nil && refreshed {
				continue
			}
		}

		return nil, err
	}
	return nil, fmt.Errorf("failed to fetch chunk after multiple retries")
}

func (m *Manager) refreshFileReference(ctx context.Context, item *db.MediaItem) (bool, error) {
	if m.rawAPI == nil {
		return false, fmt.Errorf("raw API client not ready")
	}

	// 1. Try ChannelsGetMessages (for broadcast channels and supergroups)
	channel := &tg.InputChannel{
		ChannelID:  item.SourceChatID,
		AccessHash: 0,
	}

	messages, err := m.rawAPI.ChannelsGetMessages(ctx, &tg.ChannelsGetMessagesRequest{
		Channel: channel,
		ID:      []tg.InputMessageClass{&tg.InputMessageID{ID: item.MessageID}},
	})
	if err == nil {
		if chanMsgs, ok := messages.(*tg.MessagesChannelMessages); ok && len(chanMsgs.Messages) > 0 {
			if msg, ok := chanMsgs.Messages[0].(*tg.Message); ok && msg.Media != nil {
				if docMedia, ok := msg.Media.(*tg.MessageMediaDocument); ok {
					if doc, ok := docMedia.Document.(*tg.Document); ok {
						item.FileRef = doc.FileReference
						item.AccessHash = doc.AccessHash
						_ = m.database.UpdateFileReference(item.ID, doc.FileReference, doc.AccessHash)
						return true, nil
					}
				}
			}
		}
	}

	// 2. Try MessagesGetMessages (for private bot chats and basic groups)
	userMsgs, err2 := m.rawAPI.MessagesGetMessages(ctx, []tg.InputMessageClass{&tg.InputMessageID{ID: item.MessageID}})
	if err2 == nil {
		var msgList []tg.MessageClass
		switch ms := userMsgs.(type) {
		case *tg.MessagesMessages:
			msgList = ms.Messages
		case *tg.MessagesMessagesSlice:
			msgList = ms.Messages
		case *tg.MessagesChannelMessages:
			msgList = ms.Messages
		}
		if len(msgList) > 0 {
			if msg, ok := msgList[0].(*tg.Message); ok && msg.Media != nil {
				if docMedia, ok := msg.Media.(*tg.MessageMediaDocument); ok {
					if doc, ok := docMedia.Document.(*tg.Document); ok {
						item.FileRef = doc.FileReference
						item.AccessHash = doc.AccessHash
						_ = m.database.UpdateFileReference(item.ID, doc.FileReference, doc.AccessHash)
						return true, nil
					}
				}
			}
		}
	}

	return false, fmt.Errorf("could not refresh file reference: channel err: %v, direct err: %v", err, err2)
}
