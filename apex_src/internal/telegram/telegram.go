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

func (m *Manager) Start(ctx context.Context, onNewMedia func(ctx context.Context, doc *tg.Document, chatID int64, msgID int)) error {
	if m.cfg.TelegramAPIID == 0 || m.cfg.TelegramAPIHash == "" || m.cfg.TelegramBotToken == "" {
		log.Printf("[Telegram] Warning: Telegram credentials not fully configured. Ingestion disabled.")
		return nil
	}

	sessionDir := filepath.Dir(m.cfg.SessionFilePath)
	_ = os.MkdirAll(sessionDir, 0700)

	dispatcher := tg.NewUpdateDispatcher()
	dispatcher.OnNewChannelMessage(func(ctx context.Context, e tg.Entities, u *tg.UpdateNewChannelMessage) error {
		msg, ok := u.Message.(*tg.Message)
		if !ok || msg.Media == nil {
			return nil
		}

		docMedia, ok := msg.Media.(*tg.MessageMediaDocument)
		if !ok {
			return nil
		}

		doc, ok := docMedia.Document.(*tg.Document)
		if !ok {
			return nil
		}

		peerChannel, ok := msg.PeerID.(*tg.PeerChannel)
		if !ok {
			return nil
		}

		onNewMedia(ctx, doc, peerChannel.ChannelID, msg.ID)
		return nil
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
			_ = client.Run(ctx, func(ctx context.Context) error {
				_, _ = client.Auth().Bot(ctx, m.cfg.TelegramBotToken)
				log.Printf("[Telegram] Media transfer connection #%d active.", idx+1)
				<-ctx.Done()
				return nil
			})
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
			log.Printf("[Telegram] File reference expired for media %s. Refreshing from channel...", item.ID)
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

	channel := &tg.InputChannel{
		ChannelID:  item.SourceChatID,
		AccessHash: 0,
	}

	messages, err := m.rawAPI.ChannelsGetMessages(ctx, &tg.ChannelsGetMessagesRequest{
		Channel: channel,
		ID:      []tg.InputMessageClass{&tg.InputMessageID{ID: item.MessageID}},
	})
	if err != nil {
		return false, err
	}

	chanMsgs, ok := messages.(*tg.MessagesChannelMessages)
	if !ok || len(chanMsgs.Messages) == 0 {
		return false, fmt.Errorf("message not found")
	}

	msg, ok := chanMsgs.Messages[0].(*tg.Message)
	if !ok || msg.Media == nil {
		return false, fmt.Errorf("invalid message media")
	}

	docMedia, ok := msg.Media.(*tg.MessageMediaDocument)
	if !ok {
		return false, fmt.Errorf("no document in message")
	}

	doc, ok := docMedia.Document.(*tg.Document)
	if !ok {
		return false, fmt.Errorf("document cast failed")
	}

	item.FileRef = doc.FileReference
	item.AccessHash = doc.AccessHash

	err = m.database.UpdateFileReference(item.ID, doc.FileReference, doc.AccessHash)
	return err == nil, err
}
