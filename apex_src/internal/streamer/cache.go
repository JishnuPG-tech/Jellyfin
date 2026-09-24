package streamer

import (
	"container/list"
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"log"
	"os"
	"path/filepath"
	"strings"
	"sync"
)

type Call struct {
	wg  sync.WaitGroup
	val []byte
	err error
}

type Singleflight struct {
	mu sync.Mutex
	m  map[string]*Call
}

func (g *Singleflight) Do(key string, fn func() ([]byte, error)) ([]byte, error) {
	g.mu.Lock()
	if g.m == nil {
		g.m = make(map[string]*Call)
	}
	if c, ok := g.m[key]; ok {
		g.mu.Unlock()
		c.wg.Wait()
		return c.val, c.err
	}
	c := new(Call)
	c.wg.Add(1)
	g.m[key] = c
	g.mu.Unlock()

	c.val, c.err = fn()
	c.wg.Done()

	g.mu.Lock()
	delete(g.m, key)
	g.mu.Unlock()

	return c.val, c.err
}

type CacheItem struct {
	key      string
	data     []byte
	size     int64
	diskPath string
}

type LRUCache struct {
	mu          sync.RWMutex
	maxMemory   int64
	maxDisk     int64
	currentMem  int64
	currentDisk int64
	diskDir     string
	items       map[string]*list.Element
	evictList   *list.List
	flight      Singleflight
}

func NewLRUCache(memoryMB, diskMB int, diskDir string) *LRUCache {
	_ = os.MkdirAll(diskDir, 0755)

	// Startup corruption & temporary file cleanup
	if entries, err := os.ReadDir(diskDir); err == nil {
		for _, e := range entries {
			if e.IsDir() {
				continue
			}
			name := e.Name()
			fullPath := filepath.Join(diskDir, name)

			// Remove leftover temporary files from aborted writes
			if strings.Contains(name, ".tmp") {
				_ = os.Remove(fullPath)
				continue
			}

			// Check file integrity: remove 0-byte or corrupted chunks
			if info, err := e.Info(); err == nil {
				if info.Size() == 0 {
					_ = os.Remove(fullPath)
				}
			}
		}
	}

	return &LRUCache{
		maxMemory: int64(memoryMB) * 1024 * 1024,
		maxDisk:   int64(diskMB) * 1024 * 1024,
		diskDir:   diskDir,
		items:     make(map[string]*list.Element),
		evictList: list.New(),
	}
}

func (c *LRUCache) Get(key string) ([]byte, bool) {
	c.mu.Lock()
	el, ok := c.items[key]
	if !ok {
		c.mu.Unlock()
		return nil, false
	}

	c.evictList.MoveToFront(el)
	item := el.Value.(*CacheItem)

	// Hit in RAM
	if len(item.data) > 0 {
		data := item.data
		c.mu.Unlock()
		return data, true
	}

	diskPath := item.diskPath
	c.mu.Unlock()

	// Read from disk OUTSIDE mutex to avoid blocking concurrent readers
	if diskPath != "" {
		data, err := os.ReadFile(diskPath)
		if err == nil && len(data) > 0 {
			// Promote back to RAM if memory budget allows
			c.mu.Lock()
			if c.currentMem+int64(len(data)) <= c.maxMemory {
				item.data = data
				c.currentMem += int64(len(data))
			}
			c.mu.Unlock()
			return data, true
		}
	}

	return nil, false
}

func (c *LRUCache) Put(key string, data []byte) {
	size := int64(len(data))
	h := sha256.Sum256([]byte(key))
	diskFilename := fmt.Sprintf("%s.chunk", hex.EncodeToString(h[:16]))
	diskPath := filepath.Join(c.diskDir, diskFilename)

	c.mu.Lock()
	if el, ok := c.items[key]; ok {
		c.evictList.MoveToFront(el)
		item := el.Value.(*CacheItem)
		item.data = data
		c.mu.Unlock()
		go writeDiskAtomic(diskPath, data)
		return
	}

	item := &CacheItem{
		key:      key,
		data:     data,
		size:     size,
		diskPath: diskPath,
	}

	el := c.evictList.PushFront(item)
	c.items[key] = el
	c.currentMem += size
	c.currentDisk += size

	// Evict from RAM if exceeding memory threshold
	for c.currentMem > c.maxMemory && c.evictList.Len() > 0 {
		oldest := c.evictList.Back()
		if oldest == nil {
			break
		}
		oldItem := oldest.Value.(*CacheItem)
		if len(oldItem.data) > 0 {
			c.currentMem -= int64(len(oldItem.data))
			oldItem.data = nil // Keep on disk, evict from RAM
		}
	}

	// Evict from disk if exceeding disk limit
	for c.currentDisk > c.maxDisk && c.evictList.Len() > 0 {
		oldest := c.evictList.Back()
		if oldest == nil {
			break
		}
		oldItem := oldest.Value.(*CacheItem)
		if oldItem.diskPath != "" {
			_ = os.Remove(oldItem.diskPath)
			c.currentDisk -= oldItem.size
		}
		c.evictList.Remove(oldest)
		delete(c.items, oldItem.key)
	}
	c.mu.Unlock()

	// Persist to disk asynchronously using atomic write
	go writeDiskAtomic(diskPath, data)
}

func writeDiskAtomic(finalPath string, data []byte) {
	dir := filepath.Dir(finalPath)
	tmpFile, err := os.CreateTemp(dir, "chunk_*.tmp")
	if err != nil {
		log.Printf("[Cache] Warning: Failed to create temp cache file: %v", err)
		return
	}
	tmpName := tmpFile.Name()

	if _, err := tmpFile.Write(data); err != nil {
		_ = tmpFile.Close()
		_ = os.Remove(tmpName)
		log.Printf("[Cache] Warning: Failed to write cache data: %v", err)
		return
	}

	if err := tmpFile.Close(); err != nil {
		_ = os.Remove(tmpName)
		return
	}

	if err := os.Rename(tmpName, finalPath); err != nil {
		_ = os.Remove(tmpName)
		log.Printf("[Cache] Warning: Failed to commit cache chunk: %v", err)
	}
}

func (c *LRUCache) FetchCoalesced(key string, fetcher func() ([]byte, error)) ([]byte, error) {
	// 1. Try cache first
	if data, hit := c.Get(key); hit {
		return data, nil
	}

	// 2. Coalesce duplicate concurrent requests
	return c.flight.Do(key, func() ([]byte, error) {
		// Double check cache
		if data, hit := c.Get(key); hit {
			return data, nil
		}
		data, err := fetcher()
		if err != nil {
			return nil, err
		}
		c.Put(key, data)
		return data, nil
	})
}
