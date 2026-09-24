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
	"sync/atomic"
	"time"

	"apex/internal/config"
	"apex/internal/db"

	"github.com/gotd/td/telegram"
	"github.com/gotd/td/tg"
	"github.com/gotd/td/tgerr"
)

type IngestionTask struct {
	Doc     *tg.Document
	ChatID  int64
	MsgID   int
	Caption string
}

type TransferWorker struct {
	mu                  sync.RWMutex
	client              *telegram.Client
	idx                 int
	activeReqs          int64
	consecutiveErrors   int
	floodWaitUntil      time.Time
	circuitBreakerUntil time.Time
	healthy             bool
}

const (
	maxConsecutiveErrors   = 5
	circuitBreakerCooldown = 30 * time.Second
)

func (w *TransferWorker) SetHealthy(healthy bool) {
	w.mu.Lock()
	defer w.mu.Unlock()
	w.healthy = healthy
	if healthy {
		w.circuitBreakerUntil = time.Time{}
	}
}

func (w *TransferWorker) IsAvailable(now time.Time) bool {
	w.mu.RLock()
	defer w.mu.RUnlock()
	if !w.healthy {
		// Allow single trial probe if circuit breaker cooldown has expired
		if !w.circuitBreakerUntil.IsZero() && now.After(w.circuitBreakerUntil) {
			return true
		}
		return false
	}
	return !now.Before(w.floodWaitUntil)
}

func (w *TransferWorker) RecordSuccess() {
	w.mu.Lock()
	defer w.mu.Unlock()
	w.consecutiveErrors = 0
	w.healthy = true
	w.circuitBreakerUntil = time.Time{}
}

func (w *TransferWorker) RecordFloodWait(until time.Time) {
	w.mu.Lock()
	defer w.mu.Unlock()
	w.consecutiveErrors++
	w.floodWaitUntil = until
}

func (w *TransferWorker) RecordError() {
	w.mu.Lock()
	defer w.mu.Unlock()
	w.consecutiveErrors++
	if w.consecutiveErrors >= maxConsecutiveErrors {
		w.healthy = false
		w.circuitBreakerUntil = time.Now().Add(circuitBreakerCooldown)
		log.Printf("[Telegram] TransferWorker #%d entered circuit breaker (%d errors). Cooldown for %v.",
			w.idx, w.consecutiveErrors, circuitBreakerCooldown)
	}
}

type Manager struct {
	cfg                 *config.Config
	database            *db.Database
	updateClient        *telegram.Client
	transferPool        []*TransferWorker
	poolMu              sync.RWMutex
	rawAPI              *tg.Client
	channelAccessHashes sync.Map
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

func (m *Manager) SendReply(peer tg.InputPeerClass, text string) {
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

func (m *Manager) Start(ctx context.Context, jobQueue chan<- *IngestionTask) error {
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
				m.channelAccessHashes.Store(peer.ChannelID, c.AccessHash)
			}
		}

		// Text commands without media
		if msg.Media == nil {
			text := strings.TrimSpace(msg.Message)
			if text != "" {
				log.Printf("[Telegram] Received text message #%d from %s: %s", msg.ID, peerDesc, text)
				if strings.HasPrefix(text, "/start") || strings.HasPrefix(text, "/help") {
					m.SendReply(inputPeer, "🎬 Apex Media Platform\n\nSend or forward any movie or TV show video file here.\nIt will be queued, cataloged, and made available on Jellyfin instantly!")
				}
			}
			return nil
		}

		// Inspect media
		docMedia, ok := msg.Media.(*tg.MessageMediaDocument)
		if !ok {
			return nil
		}

		doc, ok := docMedia.Document.(*tg.Document)
		if !ok {
			return nil
		}

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
			log.Printf("[Telegram] Queuing video media: '%s' (size: %d MB) from %s msg #%d",
				filename, doc.Size/(1024*1024), peerDesc, msg.ID)

			m.SendReply(inputPeer, fmt.Sprintf("📥 Queued '%s' for ingestion...\nProcessing and adding to Jellyfin.", filename))

			task := &IngestionTask{
				Doc:     doc,
				ChatID:  chatID,
				MsgID:   msg.ID,
				Caption: caption,
			}
			select {
			case jobQueue <- task:
			default:
				log.Printf("[Telegram] Ingestion queue busy. Asynchronously queuing task for '%s'", filename)
				go func(t *IngestionTask) {
					jobQueue <- t
				}(task)
			}
		}
		return nil
	}

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
			log.Printf("[Telegram] Dedicated MTProto update & metadata connection established.")
			<-ctx.Done()
			return nil
		})
		if err != nil {
			log.Printf("[Telegram] Update connection error: %v", err)
		}
	}()

	// Initialize Adaptive Media Transfer Pool
	numClients := m.cfg.MediaClients
	if numClients <= 0 {
		numClients = 2
	}

	log.Printf("[Telegram] Initializing %d dedicated media transfer workers...", numClients)
	for i := 0; i < numClients; i++ {
		c := telegram.NewClient(m.cfg.TelegramAPIID, m.cfg.TelegramAPIHash, telegram.Options{})
		worker := &TransferWorker{
			client:  c,
			idx:     i + 1,
			healthy: true,
		}
		m.transferPool = append(m.transferPool, worker)

		go func(w *TransferWorker) {
			err := w.client.Run(ctx, func(ctx context.Context) error {
				_, authErr := w.client.Auth().Bot(ctx, m.cfg.TelegramBotToken)
				if authErr != nil {
					log.Printf("[Telegram] Worker #%d auth error: %v", w.idx, authErr)
					w.SetHealthy(false)
					return authErr
				}
				w.SetHealthy(true)
				log.Printf("[Telegram] Media transfer worker #%d active.", w.idx)
				<-ctx.Done()
				return nil
			})
			if err != nil {
				log.Printf("[Telegram] Media transfer worker #%d stopped: %v", w.idx, err)
			}
		}(worker)
	}

	return nil
}

// Adaptive health-aware worker selection
func (m *Manager) getBestWorker() *TransferWorker {
	m.poolMu.RLock()
	defer m.poolMu.RUnlock()

	now := time.Now()
	var best *TransferWorker
	minActive := int64(1<<62 - 1)

	for _, w := range m.transferPool {
		if !w.IsAvailable(now) {
			continue
		}

		active := atomic.LoadInt64(&w.activeReqs)
		if active < minActive {
			minActive = active
			best = w
		}
	}

	if best != nil {
		return best
	}
	// Fallback to first worker if all are busy/recovering
	if len(m.transferPool) > 0 {
		return m.transferPool[0]
	}
	return nil
}

func (m *Manager) FetchChunk(ctx context.Context, item *db.MediaItem, offset int64, limit int) ([]byte, error) {
	docID, _ := strconv.ParseInt(item.FileID, 10, 64)

	for attempt := 0; attempt < 3; attempt++ {
		worker := m.getBestWorker()
		if worker == nil {
			return nil, fmt.Errorf("no media transfer worker available")
		}

		atomic.AddInt64(&worker.activeReqs, 1)
		raw := worker.client.API()

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
		atomic.AddInt64(&worker.activeReqs, -1)

		if err == nil {
			worker.RecordSuccess()
			switch file := res.(type) {
			case *tg.UploadFile:
				return file.Bytes, nil
			case *tg.UploadFileCDNRedirect:
				log.Printf("[Telegram] Received CDN redirect for media %s (DC: %d). Requesting re-upload to master DC...", item.ID, file.DCID)
				_, reuploadErr := raw.UploadReuploadCDNFile(ctx, &tg.UploadReuploadCDNFileRequest{
					FileToken:    file.FileToken,
					RequestToken: file.FileToken,
				})
				if reuploadErr != nil {
					log.Printf("[Telegram] CDN re-upload failed: %v", reuploadErr)
					return nil, fmt.Errorf("CDN redirect received and re-upload failed: %w", reuploadErr)
				}
				time.Sleep(200 * time.Millisecond)
				continue
			}
			return nil, fmt.Errorf("unexpected file response type")
		}

		// Handle FLOOD_WAIT_X
		if d, ok := tgerr.AsFloodWait(err); ok {
			jitter := time.Duration(rand.Intn(500)) * time.Millisecond
			sleepDuration := d + jitter
			worker.RecordFloodWait(time.Now().Add(sleepDuration))
			log.Printf("[Telegram] Worker #%d FLOOD_WAIT %v. Backing off.", worker.idx, sleepDuration)
			select {
			case <-time.After(sleepDuration):
				continue
			case <-ctx.Done():
				return nil, ctx.Err()
			}
		} else {
			worker.RecordError()
		}

		// Handle FILE_REFERENCE_EXPIRED
		if strings.Contains(err.Error(), "FILE_REFERENCE_EXPIRED") || strings.Contains(err.Error(), "FILE_REFERENCE_INVALID") {
			log.Printf("[Telegram] File reference expired for media %s. Refreshing file reference...", item.ID)
			refreshed, refErr := m.RefreshFileReference(ctx, item)
			if refErr == nil && refreshed {
				continue
			}
		}

		return nil, err
	}
	return nil, fmt.Errorf("failed to fetch chunk after multiple retries")
}

func (m *Manager) RefreshFileReference(ctx context.Context, item *db.MediaItem) (bool, error) {
	if m.rawAPI == nil {
		return false, fmt.Errorf("raw API client not ready")
	}

	// 1. Try ChannelsGetMessages (for broadcast channels and supergroups)
	var channelHash int64
	if val, ok := m.channelAccessHashes.Load(item.SourceChatID); ok {
		channelHash = val.(int64)
	}
	channel := &tg.InputChannel{
		ChannelID:  item.SourceChatID,
		AccessHash: channelHash,
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
