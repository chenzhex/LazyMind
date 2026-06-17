package tree

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/lazymind/scan_control_plane/internal/sourceengine/connector"
)

const (
	targetSearchCacheTTL        = 10 * time.Minute
	targetSearchCacheMaxObjects = 5000
	targetSearchCachePageDelay  = time.Second

	targetSearchCacheStatusMissing  = "missing"
	targetSearchCacheStatusBuilding = "building"
	targetSearchCacheStatusComplete = "complete"
	targetSearchCacheStatusFailed   = "failed"
)

type targetSearchCache struct {
	mu       sync.Mutex
	entries  map[string]*targetSearchCacheEntry
	store    targetSearchCacheStore
	ttl      time.Duration
	maxItems int
	delay    time.Duration
}

type targetSearchCacheEntry struct {
	nodes     []TreeNode
	building  bool
	complete  bool
	truncated bool
	lastError string
	expiresAt time.Time
}

type targetSearchCacheSnapshot struct {
	nodes     []TreeNode
	status    string
	building  bool
	complete  bool
	truncated bool
	lastError string
}

type targetSearchCacheStore interface {
	Get(ctx context.Context, key string) (targetSearchCacheSnapshot, bool, error)
	Set(ctx context.Context, key string, snapshot targetSearchCacheSnapshot, ttl time.Duration) error
	TryLock(ctx context.Context, key string, ttl time.Duration) (bool, error)
}

func newTargetSearchCache() *targetSearchCache {
	return &targetSearchCache{
		entries:  map[string]*targetSearchCacheEntry{},
		ttl:      targetSearchCacheTTL,
		maxItems: targetSearchCacheMaxObjects,
		delay:    targetSearchCachePageDelay,
	}
}

func (c *targetSearchCache) snapshot(ctx context.Context, req TargetTreeSearchRequest) targetSearchCacheSnapshot {
	if c == nil {
		return targetSearchCacheSnapshot{}
	}
	key := targetSearchCacheKey(req)
	if c.store != nil {
		if snapshot, ok, err := c.store.Get(ctx, key); err == nil && ok {
			return snapshot
		}
	}
	c.mu.Lock()
	entry := c.entries[key]
	if entry != nil && time.Now().Before(entry.expiresAt) {
		snapshot := entry.snapshot()
		c.mu.Unlock()
		return snapshot
	}
	c.mu.Unlock()
	return targetSearchCacheSnapshot{status: targetSearchCacheStatusMissing}
}

func (c *targetSearchCache) build(ctx context.Context, key string, conn connector.SourceConnector, req TargetTreeSearchRequest, build func(context.Context, connector.SourceConnector, TargetTreeSearchRequest) ([]TreeNode, bool, error)) targetSearchCacheSnapshot {
	if ctx == nil {
		ctx = context.Background()
	}
	buildCtx, cancel := context.WithTimeout(ctx, c.ttl)
	defer cancel()
	nodes, truncated, err := build(buildCtx, conn, req)
	snapshot := targetSearchCacheSnapshot{
		nodes:     append([]TreeNode(nil), nodes...),
		status:    targetSearchCacheStatusComplete,
		complete:  true,
		truncated: truncated,
	}
	if err != nil {
		snapshot.nodes = nil
		snapshot.status = targetSearchCacheStatusFailed
		snapshot.complete = false
		snapshot.truncated = false
		snapshot.lastError = err.Error()
	}
	c.mu.Lock()
	entry := c.entries[key]
	if entry == nil {
		entry = &targetSearchCacheEntry{}
		c.entries[key] = entry
	}
	entry.nodes = snapshot.nodes
	entry.truncated = snapshot.truncated
	entry.complete = snapshot.complete
	entry.building = false
	entry.lastError = snapshot.lastError
	entry.expiresAt = time.Now().Add(c.ttl)
	c.mu.Unlock()
	if c.store != nil {
		setCtx, setCancel := context.WithTimeout(context.Background(), 10*time.Second)
		defer setCancel()
		_ = c.store.Set(setCtx, key, snapshot, c.ttl)
	}
	return snapshot
}

func (c *targetSearchCache) buildIfUnlocked(ctx context.Context, conn connector.SourceConnector, req TargetTreeSearchRequest, build func(context.Context, connector.SourceConnector, TargetTreeSearchRequest) ([]TreeNode, bool, error)) targetSearchCacheSnapshot {
	if c == nil {
		return targetSearchCacheSnapshot{status: targetSearchCacheStatusMissing}
	}
	key := targetSearchCacheKey(req)
	if c.store != nil {
		if snapshot, ok, err := c.store.Get(ctx, key); err == nil && ok && snapshot.complete {
			return snapshot
		}
		locked, err := c.store.TryLock(ctx, key, c.ttl)
		if err != nil {
			return targetSearchCacheSnapshot{status: targetSearchCacheStatusFailed, lastError: err.Error()}
		}
		if !locked {
			if snapshot, ok, err := c.store.Get(ctx, key); err == nil && ok {
				return snapshot
			}
			return targetSearchCacheSnapshot{status: targetSearchCacheStatusBuilding, building: true}
		}
		return c.build(ctx, key, conn, req, build)
	}
	c.mu.Lock()
	entry := c.entries[key]
	if entry != nil && time.Now().Before(entry.expiresAt) && entry.complete {
		snapshot := entry.snapshot()
		c.mu.Unlock()
		return snapshot
	}
	if entry != nil && entry.building {
		snapshot := entry.snapshot()
		c.mu.Unlock()
		return snapshot
	}
	c.entries[key] = &targetSearchCacheEntry{building: true, expiresAt: time.Now().Add(c.ttl)}
	c.mu.Unlock()
	return c.build(ctx, key, conn, req, build)
}

func (e *targetSearchCacheEntry) snapshot() targetSearchCacheSnapshot {
	if e == nil {
		return targetSearchCacheSnapshot{}
	}
	nodes := append([]TreeNode(nil), e.nodes...)
	return targetSearchCacheSnapshot{
		nodes:     nodes,
		status:    entryStatus(e),
		building:  e.building,
		complete:  e.complete,
		truncated: e.truncated,
		lastError: e.lastError,
	}
}

func entryStatus(e *targetSearchCacheEntry) string {
	if e == nil {
		return targetSearchCacheStatusMissing
	}
	if e.building {
		return targetSearchCacheStatusBuilding
	}
	if e.complete {
		return targetSearchCacheStatusComplete
	}
	if strings.TrimSpace(e.lastError) != "" {
		return targetSearchCacheStatusFailed
	}
	return targetSearchCacheStatusMissing
}

func targetSearchCacheKey(req TargetTreeSearchRequest) string {
	parts := []string{
		string(req.ConnectorType),
		req.AgentID,
		req.AuthConnectionID,
		stableTargetSearchCacheProviderOptions(req.ConnectorType, req.ProviderOptions),
	}
	return strings.Join(parts, "\x00")
}

func stableTargetSearchCacheProviderOptions(connectorType connector.ConnectorType, options map[string]any) string {
	if connectorType != "feishu" {
		return stableProviderOptions(options)
	}
	if len(options) == 0 {
		return ""
	}
	filtered := make(map[string]any, len(options))
	for key, value := range options {
		switch key {
		case "tenant_key", "tenant_id", "user_id":
			continue
		default:
			filtered[key] = value
		}
	}
	return stableProviderOptions(filtered)
}

func targetSearchCacheRedisKey(key string) string {
	sum := sha256.Sum256([]byte(key))
	return hex.EncodeToString(sum[:])
}

func stableProviderOptions(options map[string]any) string {
	if len(options) == 0 {
		return ""
	}
	keys := make([]string, 0, len(options))
	for key := range options {
		keys = append(keys, key)
	}
	sort.Strings(keys)
	out := make(map[string]any, len(options))
	for _, key := range keys {
		out[key] = options[key]
	}
	data, err := json.Marshal(out)
	if err != nil {
		return ""
	}
	return string(data)
}

func paginateCachedTargetNodes(nodes []TreeNode, keyword string, includeFiles bool, pageSize int, cursor string) (TreeNodePage, error) {
	offset, err := cursorOffset(cursor)
	if err != nil {
		return TreeNodePage{}, err
	}
	matches := make([]TreeNode, 0, pageSize)
	seen := 0
	for _, node := range nodes {
		if !includeFiles && !node.IsContainer {
			continue
		}
		if !treeNodeSearchMatches(node, keyword) {
			continue
		}
		seen++
		if seen <= offset {
			continue
		}
		matches = append(matches, node)
		if len(matches) > pageSize {
			return TreeNodePage{
				Items:      matches[:pageSize],
				HasMore:    true,
				NextCursor: strconv.Itoa(offset + pageSize),
				SearchMode: SearchModeCache,
			}, nil
		}
	}
	return TreeNodePage{Items: matches, ListComplete: true, SearchMode: SearchModeCache}, nil
}

func treeNodeSearchMatches(node TreeNode, keyword string) bool {
	needle := strings.ToLower(strings.TrimSpace(keyword))
	if needle == "" {
		return true
	}
	for _, value := range []string{node.SearchName, node.DisplayName} {
		if strings.Contains(strings.ToLower(value), needle) {
			return true
		}
	}
	return false
}
