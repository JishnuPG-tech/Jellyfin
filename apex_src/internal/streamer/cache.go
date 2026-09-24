package streamer

import (
	"container/list"
	"fmt"
	"os"
	"path/filepath"
	"sync"
)

type CacheItem struct {
	key      string
	data     []byte
	size     int64
	diskPath string
}

type LRUCache struct {
	mu          sync.Mutex
	maxMemory   int64
	maxDisk     int64
	currentMem  int64
	currentDisk int64
	diskDir     string
	items       map[string]*list.Element
	evictList   *list.List
}

func NewLRUCache(memoryMB, diskMB int, diskDir string) *LRUCache {
	_ = os.MkdirAll(diskDir, 0755)
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
	defer c.mu.Unlock()

	el, ok := c.items[key]
	if !ok {
		return nil, false
	}

	c.evictList.MoveToFront(el)
	item := el.Value.(*CacheItem)

	if len(item.data) > 0 {
		return item.data, true
	}

	// Read from disk cache if evicted from RAM
	if item.diskPath != "" {
		data, err := os.ReadFile(item.diskPath)
		if err == nil {
			return data, true
		}
	}

	return nil, false
}

func (c *LRUCache) Put(key string, data []byte) {
	c.mu.Lock()
	defer c.mu.Unlock()

	size := int64(len(data))
	diskPath := filepath.Join(c.diskDir, fmt.Sprintf("%x.chunk", key))
	_ = os.WriteFile(diskPath, data, 0644)

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
			oldItem.data = nil // Demote to disk only
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
}
