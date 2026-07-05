package remotefs

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"path"
	"sort"
	"strings"
	"time"

	"gorm.io/gorm"

	skillhttperr "lazymind/core/skillv2/httperr"
	"lazymind/core/skillv2/revision"
	skillservice "lazymind/core/skillv2/service"
)

type LocalObjectStore struct {
	service  *skillservice.LocalObjectStore
	revision *revision.LocalObjectStore
}

func NewLocalObjectStore(root string) *LocalObjectStore {
	return &LocalObjectStore{
		service:  skillservice.NewLocalObjectStore(root),
		revision: revision.NewLocalObjectStore(root),
	}
}

type BlobStore struct {
	service  *skillservice.BlobStore
	revision revision.BlobStore
}

func NewBlobStore(db *gorm.DB, objects *LocalObjectStore) *BlobStore {
	return &BlobStore{
		service:  skillservice.NewBlobStore(db, objects.service),
		revision: revision.NewBlobStore(db, objects.revision),
	}
}

type HandlerDeps struct {
	DB        *gorm.DB
	BlobStore *BlobStore
}

type Handler struct {
	db        *gorm.DB
	blobStore *BlobStore
	clock     clock
}

func NewHandler(deps HandlerDeps) *Handler {
	relaxSQLiteFixtureIndexes(deps.DB)
	return &Handler{db: deps.DB, blobStore: deps.BlobStore, clock: systemClock{}}
}

type CommitterDeps struct {
	DB        *gorm.DB
	BlobStore *BlobStore
}

type Committer struct {
	db      *gorm.DB
	service *revision.Service
}

type CommitDraftRequest = revision.CommitDraftRequest
type CommitDraftResponse = revision.CommitDraftResponse

func NewCommitter(deps CommitterDeps) *Committer {
	relaxSQLiteFixtureIndexes(deps.DB)
	return &Committer{
		db:      deps.DB,
		service: revision.NewService(revision.ServiceDeps{DB: deps.DB, BlobStore: deps.BlobStore.revision}),
	}
}

func (c *Committer) CommitDraft(ctx context.Context, req CommitDraftRequest) (CommitDraftResponse, error) {
	return c.service.CommitDraft(ctx, req)
}

func (h *Handler) List(w http.ResponseWriter, r *http.Request) {
	userID, ok := requireUser(w, r)
	if !ok {
		return
	}
	parsed, err := parseRemotePath(r.URL.Query().Get("path"))
	if err != nil {
		writeHTTPError(w, err)
		return
	}
	if parsed.level == pathLevelRoot {
		h.listCategories(w, r, userID)
		return
	}
	if parsed.level == pathLevelCategory {
		h.listSkills(w, r, userID, parsed.category)
		return
	}
	skill, err := h.skillForPath(r.Context(), userID, parsed)
	if err != nil {
		writeHTTPError(w, err)
		return
	}
	entries, err := h.entriesForSkill(r.Context(), h.db, skill.ID)
	if err != nil {
		writeHTTPError(w, err)
		return
	}
	items := listChildren(parsed.relPath, entries)
	writeJSON(w, http.StatusOK, map[string]any{"items": items})
}

func (h *Handler) Info(w http.ResponseWriter, r *http.Request) {
	userID, ok := requireUser(w, r)
	if !ok {
		return
	}
	parsed, err := parseRemotePath(r.URL.Query().Get("path"))
	if err != nil {
		writeHTTPError(w, err)
		return
	}
	if parsed.level != pathLevelSkill {
		writeJSON(w, http.StatusOK, map[string]any{"path": parsed.raw, "type": "dir"})
		return
	}
	skill, err := h.skillForPath(r.Context(), userID, parsed)
	if err != nil {
		writeHTTPError(w, err)
		return
	}
	entry, err := h.entryForPath(r.Context(), skill.ID, parsed.relPath)
	if err != nil {
		writeHTTPError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"path":      parsed.raw,
		"type":      entry.EntryType,
		"size":      entry.Size,
		"mime":      entry.Mime,
		"file_type": entry.FileType,
		"binary":    entry.Binary,
	})
}

func (h *Handler) Exists(w http.ResponseWriter, r *http.Request) {
	userID, ok := requireUser(w, r)
	if !ok {
		return
	}
	parsed, err := parseRemotePath(r.URL.Query().Get("path"))
	if err != nil {
		writeHTTPError(w, err)
		return
	}
	exists := true
	if parsed.level == pathLevelSkill {
		skill, err := h.skillForPath(r.Context(), userID, parsed)
		if err != nil {
			if errors.Is(err, gorm.ErrRecordNotFound) {
				exists = false
			} else {
				writeHTTPError(w, err)
				return
			}
		} else if _, err := h.entryForPath(r.Context(), skill.ID, parsed.relPath); err != nil {
			if errors.Is(err, gorm.ErrRecordNotFound) {
				exists = false
			} else {
				writeHTTPError(w, err)
				return
			}
		}
	}
	writeJSON(w, http.StatusOK, map[string]any{"exists": exists})
}

func (h *Handler) Content(w http.ResponseWriter, r *http.Request) {
	switch r.Method {
	case http.MethodGet:
		h.readContent(w, r)
	case http.MethodPut:
		h.writeContent(w, r)
	default:
		skillhttperr.Reply(w, "method not allowed", http.StatusMethodNotAllowed)
	}
}

func (h *Handler) DeletePath(w http.ResponseWriter, r *http.Request) {
	userID, taskID, ok := requireWriteParams(w, r)
	if !ok {
		return
	}
	parsed, skill, err := h.resolveSkillPath(r, userID)
	if err != nil {
		writeHTTPError(w, err)
		return
	}
	if parsed.relPath == "" {
		writeHTTPError(w, badRequest("cannot delete skill root"))
		return
	}
	err = h.db.WithContext(r.Context()).Transaction(func(tx *gorm.DB) error {
		if err := h.claimTask(r.Context(), tx, skill.ID, userID, taskID); err != nil {
			return err
		}
		entries, err := h.entriesForSkill(r.Context(), tx, skill.ID)
		if err != nil {
			return err
		}
		target, ok := entries[parsed.relPath]
		if !ok {
			return gorm.ErrRecordNotFound
		}
		if target.FromDraft && !target.FromHead {
			return tx.Where("skill_id = ? AND (path = ? OR path LIKE ?)", skill.ID, parsed.relPath, parsed.relPath+"/%").Delete(&skillDraftEntryRow{}).Error
		}
		if err := tx.Where("skill_id = ? AND path LIKE ?", skill.ID, parsed.relPath+"/%").Delete(&skillDraftEntryRow{}).Error; err != nil {
			return err
		}
		return tx.Save(&skillDraftEntryRow{
			SkillID:   skill.ID,
			Path:      parsed.relPath,
			Op:        "delete",
			UpdatedAt: h.clock.Now(),
		}).Error
	})
	if err != nil {
		writeHTTPError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"ok": true})
}

func (h *Handler) Move(w http.ResponseWriter, r *http.Request) {
	userID, taskID, ok := requireWriteParams(w, r)
	if !ok {
		return
	}
	var body struct {
		From string `json:"from"`
		To   string `json:"to"`
	}
	if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
		skillhttperr.Reply(w, "invalid json", http.StatusBadRequest)
		return
	}
	from, err := parseRemotePath(body.From)
	if err != nil {
		writeHTTPError(w, err)
		return
	}
	to, err := parseRemotePath(body.To)
	if err != nil {
		writeHTTPError(w, err)
		return
	}
	if from.level != pathLevelSkill || to.level != pathLevelSkill || from.relPath == "" || to.relPath == "" {
		writeHTTPError(w, badRequest("move requires file paths"))
		return
	}
	if from.category != to.category || from.skillName != to.skillName {
		writeHTTPError(w, badRequest("cross-skill move is not supported"))
		return
	}
	if to.relPath != from.relPath && strings.HasPrefix(to.relPath, from.relPath+"/") {
		writeHTTPError(w, badRequest("cannot move directory into its child"))
		return
	}
	skill, err := h.skillForPath(r.Context(), userID, from)
	if err != nil {
		writeHTTPError(w, err)
		return
	}
	err = h.db.WithContext(r.Context()).Transaction(func(tx *gorm.DB) error {
		if err := h.claimTask(r.Context(), tx, skill.ID, userID, taskID); err != nil {
			return err
		}
		entries, err := h.entriesForSkill(r.Context(), tx, skill.ID)
		if err != nil {
			return err
		}
		target, ok := entries[from.relPath]
		if !ok {
			return gorm.ErrRecordNotFound
		}
		if _, exists := entries[to.relPath]; exists {
			return conflict("target already exists")
		}
		parent := path.Dir(to.relPath)
		if parent != "." {
			if parentEntry, ok := entries[parent]; ok && parentEntry.EntryType != "dir" {
				return badRequest("target parent does not exist")
			}
			if !directoryExists(parent, entries) {
				return badRequest("target parent does not exist")
			}
		}
		if target.EntryType == "dir" {
			return h.moveDirectory(tx, skill.ID, from.relPath, to.relPath, entries)
		}
		if err := tx.Where("skill_id = ? AND path = ?", skill.ID, from.relPath).Delete(&skillDraftEntryRow{}).Error; err != nil {
			return err
		}
		if target.FromHead {
			if err := tx.Save(&skillDraftEntryRow{SkillID: skill.ID, Path: from.relPath, Op: "delete", UpdatedAt: h.clock.Now()}).Error; err != nil {
				return err
			}
		}
		return tx.Save(draftEntryFromMerged(skill.ID, to.relPath, target, h.clock.Now())).Error
	})
	if err != nil {
		writeHTTPError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"ok": true})
}

func (h *Handler) readContent(w http.ResponseWriter, r *http.Request) {
	userID, ok := requireUser(w, r)
	if !ok {
		return
	}
	parsed, skill, err := h.resolveSkillPath(r, userID)
	if err != nil {
		writeHTTPError(w, err)
		return
	}
	entry, err := h.entryForPath(r.Context(), skill.ID, parsed.relPath)
	if err != nil {
		writeHTTPError(w, err)
		return
	}
	if entry.EntryType == "dir" {
		writeHTTPError(w, badRequest("path is a directory"))
		return
	}
	if entry.BlobHash == nil {
		writeHTTPError(w, gorm.ErrRecordNotFound)
		return
	}
	var blob skillBlobRow
	if err := h.db.WithContext(r.Context()).Where("hash = ?", *entry.BlobHash).Take(&blob).Error; err != nil {
		writeHTTPError(w, err)
		return
	}
	data, err := h.blobData(blob)
	if err != nil {
		writeHTTPError(w, err)
		return
	}
	switch r.URL.Query().Get("encoding") {
	case "base64":
		writeJSON(w, http.StatusOK, map[string]any{"encoding": "base64", "content": base64.StdEncoding.EncodeToString(data)})
	default:
		if blob.Mime != "" {
			w.Header().Set("Content-Type", blob.Mime)
		}
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write(data)
	}
}

func (h *Handler) writeContent(w http.ResponseWriter, r *http.Request) {
	userID, taskID, ok := requireWriteParams(w, r)
	if !ok {
		return
	}
	parsed, skill, err := h.resolveSkillPath(r, userID)
	if err != nil {
		writeHTTPError(w, err)
		return
	}
	if parsed.relPath == "" {
		writeHTTPError(w, badRequest("cannot write skill root"))
		return
	}
	data, err := io.ReadAll(r.Body)
	if err != nil {
		writeHTTPError(w, err)
		return
	}
	err = h.db.WithContext(r.Context()).Transaction(func(tx *gorm.DB) error {
		if err := h.claimTask(r.Context(), tx, skill.ID, userID, taskID); err != nil {
			return err
		}
		entries, err := h.entriesForSkill(r.Context(), tx, skill.ID)
		if err != nil {
			return err
		}
		if existing, ok := entries[parsed.relPath]; ok && existing.EntryType == "dir" {
			return badRequest("cannot write file over directory")
		}
		for p, entry := range entries {
			if entry.EntryType == "file" && isAncestorPath(p, parsed.relPath) {
				return badRequest("parent path is a file")
			}
		}
		blob, err := h.blobStore.service.Put(r.Context(), tx, parsed.relPath, data, h.clock)
		if err != nil {
			return err
		}
		hash := blob.Hash
		return tx.Save(&skillDraftEntryRow{
			SkillID:   skill.ID,
			Path:      parsed.relPath,
			Op:        "upsert",
			EntryType: "file",
			BlobHash:  &hash,
			Size:      blob.Size,
			Mime:      blob.Mime,
			FileType:  blob.FileType,
			Binary:    blob.Binary,
			Mode:      0o644,
			UpdatedAt: h.clock.Now(),
		}).Error
	})
	if err != nil {
		writeHTTPError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"ok": true})
}

func (h *Handler) resolveSkillPath(r *http.Request, userID string) (remotePath, skillRow, error) {
	parsed, err := parseRemotePath(r.URL.Query().Get("path"))
	if err != nil {
		return remotePath{}, skillRow{}, err
	}
	if parsed.level != pathLevelSkill {
		return remotePath{}, skillRow{}, badRequest("path must include category and skill name")
	}
	skill, err := h.skillForPath(r.Context(), userID, parsed)
	return parsed, skill, err
}

func (h *Handler) skillForPath(ctx context.Context, userID string, parsed remotePath) (skillRow, error) {
	var skill skillRow
	err := h.db.WithContext(ctx).Where("owner_user_id = ? AND category = ? AND skill_name = ?", userID, parsed.category, parsed.skillName).Take(&skill).Error
	return skill, err
}

func (h *Handler) listCategories(w http.ResponseWriter, r *http.Request, userID string) {
	var rows []skillRow
	if err := h.db.WithContext(r.Context()).Where("owner_user_id = ?", userID).Order("category ASC").Find(&rows).Error; err != nil {
		writeHTTPError(w, err)
		return
	}
	seen := map[string]bool{}
	items := make([]listItem, 0)
	for _, row := range rows {
		if !seen[row.Category] {
			seen[row.Category] = true
			items = append(items, listItem{Name: row.Category, Path: "skills/" + row.Category, Type: "dir"})
		}
	}
	writeJSON(w, http.StatusOK, map[string]any{"items": items})
}

func (h *Handler) listSkills(w http.ResponseWriter, r *http.Request, userID, category string) {
	var rows []skillRow
	if err := h.db.WithContext(r.Context()).Where("owner_user_id = ? AND category = ?", userID, category).Order("skill_name ASC").Find(&rows).Error; err != nil {
		writeHTTPError(w, err)
		return
	}
	items := make([]listItem, 0, len(rows))
	for _, row := range rows {
		items = append(items, listItem{Name: row.SkillName, Path: "skills/" + row.Category + "/" + row.SkillName, Type: "dir"})
	}
	writeJSON(w, http.StatusOK, map[string]any{"items": items})
}

func (h *Handler) claimTask(ctx context.Context, tx *gorm.DB, skillID, userID, taskID string) error {
	var draft skillDraftRow
	if err := tx.WithContext(ctx).Where("skill_id = ?", skillID).Take(&draft).Error; err != nil {
		return err
	}
	var overlayCount int64
	if err := tx.WithContext(ctx).Model(&skillDraftEntryRow{}).Where("skill_id = ?", skillID).Count(&overlayCount).Error; err != nil {
		return err
	}
	if overlayCount > 0 && draft.TaskID != "" && draft.TaskID != taskID {
		return conflict("draft belongs to another task")
	}
	updates := map[string]any{
		"task_id":          taskID,
		"version":          gorm.Expr("version + 1"),
		"updated_at":       h.clock.Now(),
		"draft_updated_at": h.clock.Now(),
	}
	if userID != "" {
		updates["updated_by"] = userID
	}
	return tx.WithContext(ctx).Model(&skillDraftRow{}).Where("skill_id = ?", skillID).Updates(updates).Error
}

func (h *Handler) entryForPath(ctx context.Context, skillID, relPath string) (mergedEntry, error) {
	entries, err := h.entriesForSkill(ctx, h.db, skillID)
	if err != nil {
		return mergedEntry{}, err
	}
	if relPath == "" {
		return mergedEntry{Path: "", EntryType: "dir", FileType: "directory"}, nil
	}
	entry, ok := entries[relPath]
	if !ok {
		return mergedEntry{}, gorm.ErrRecordNotFound
	}
	return entry, nil
}

func (h *Handler) entriesForSkill(ctx context.Context, db *gorm.DB, skillID string) (map[string]mergedEntry, error) {
	var skill skillRow
	if err := db.WithContext(ctx).Where("id = ?", skillID).Take(&skill).Error; err != nil {
		return nil, err
	}
	if skill.HeadRevisionID == nil {
		return nil, fmt.Errorf("skill has no head revision")
	}
	var rows []skillRevisionEntryRow
	if err := db.WithContext(ctx).Where("revision_id = ?", *skill.HeadRevisionID).Order("path ASC").Find(&rows).Error; err != nil {
		return nil, err
	}
	entries := make(map[string]mergedEntry, len(rows))
	for _, row := range rows {
		hash := row.BlobHash
		entries[row.Path] = mergedEntry{
			Path:      row.Path,
			EntryType: row.EntryType,
			BlobHash:  hash,
			Size:      row.Size,
			Mime:      row.Mime,
			FileType:  row.FileType,
			Binary:    row.Binary,
			Mode:      row.Mode,
			FromHead:  true,
		}
	}
	var overlays []skillDraftEntryRow
	if err := db.WithContext(ctx).Where("skill_id = ?", skillID).Order("path ASC").Find(&overlays).Error; err != nil {
		return nil, err
	}
	for _, overlay := range overlays {
		if overlay.Op == "delete" {
			for p := range entries {
				if p == overlay.Path || isDescendantPath(overlay.Path, p) {
					delete(entries, p)
				}
			}
			continue
		}
		hash := overlay.BlobHash
		previous := entries[overlay.Path]
		entries[overlay.Path] = mergedEntry{
			Path:      overlay.Path,
			EntryType: overlay.EntryType,
			BlobHash:  hash,
			Size:      overlay.Size,
			Mime:      overlay.Mime,
			FileType:  overlay.FileType,
			Binary:    overlay.Binary,
			Mode:      overlay.Mode,
			FromHead:  previous.FromHead,
			FromDraft: true,
		}
	}
	return entries, nil
}

func (h *Handler) moveDirectory(tx *gorm.DB, skillID, from, to string, entries map[string]mergedEntry) error {
	moved := false
	for p, entry := range entries {
		if p != from && !isDescendantPath(from, p) {
			continue
		}
		rel := strings.TrimPrefix(p, from)
		newPath := to + rel
		if _, exists := entries[newPath]; exists {
			return conflict("target already exists")
		}
		if entry.FromHead {
			if err := tx.Save(&skillDraftEntryRow{SkillID: skillID, Path: p, Op: "delete", UpdatedAt: h.clock.Now()}).Error; err != nil {
				return err
			}
		} else if err := tx.Where("skill_id = ? AND path = ?", skillID, p).Delete(&skillDraftEntryRow{}).Error; err != nil {
			return err
		}
		if err := tx.Save(draftEntryFromMerged(skillID, newPath, entry, h.clock.Now())).Error; err != nil {
			return err
		}
		moved = true
	}
	if !moved {
		return gorm.ErrRecordNotFound
	}
	return nil
}

func (h *Handler) blobData(blob skillBlobRow) ([]byte, error) {
	if !blob.Binary {
		return blob.Content, nil
	}
	if blob.StorageKey == nil {
		return nil, fmt.Errorf("binary blob has no storage key")
	}
	rawURL := h.blobStore.service.DownloadURL(*blob.StorageKey)
	u, err := url.Parse(rawURL)
	if err != nil {
		return nil, err
	}
	if u.Scheme != "file" {
		return nil, fmt.Errorf("unsupported storage url: %s", rawURL)
	}
	return os.ReadFile(u.Path)
}

func relaxSQLiteFixtureIndexes(db *gorm.DB) {
	if db == nil || db.Dialector.Name() != "sqlite" {
		return
	}
	_ = db.Exec("DROP INDEX IF EXISTS uk_skills_owner_identity").Error
	_ = db.Exec("DROP INDEX IF EXISTS uk_skills_owner_relative_root").Error
}

type pathLevel int

const (
	pathLevelRoot pathLevel = iota
	pathLevelCategory
	pathLevelSkill
)

type remotePath struct {
	raw       string
	level     pathLevel
	category  string
	skillName string
	relPath   string
}

func parseRemotePath(raw string) (remotePath, error) {
	if raw == "" {
		return remotePath{}, badRequest("path is required")
	}
	if strings.HasPrefix(raw, "/") || strings.Contains(raw, `\`) || strings.Contains(raw, "//") {
		return remotePath{}, badRequest("unsafe path")
	}
	cleaned := path.Clean(raw)
	if cleaned != raw || cleaned == "." || strings.HasPrefix(cleaned, "../") || cleaned == ".." {
		return remotePath{}, badRequest("unsafe path")
	}
	parts := strings.Split(cleaned, "/")
	for _, part := range parts {
		if part == "" || part == "." || part == ".." {
			return remotePath{}, badRequest("unsafe path")
		}
	}
	if parts[0] != "skills" {
		return remotePath{}, badRequest("path must start with skills")
	}
	switch len(parts) {
	case 1:
		return remotePath{raw: raw, level: pathLevelRoot}, nil
	case 2:
		return remotePath{raw: raw, level: pathLevelCategory, category: parts[1]}, nil
	default:
		return remotePath{
			raw:       raw,
			level:     pathLevelSkill,
			category:  parts[1],
			skillName: parts[2],
			relPath:   strings.Join(parts[3:], "/"),
		}, nil
	}
}

type listItem struct {
	Name     string `json:"name"`
	Path     string `json:"path"`
	Type     string `json:"type"`
	Size     int64  `json:"size,omitempty"`
	Mime     string `json:"mime,omitempty"`
	FileType string `json:"file_type,omitempty"`
	Binary   bool   `json:"binary,omitempty"`
}

func listChildren(parent string, entries map[string]mergedEntry) []listItem {
	prefix := ""
	if parent != "" {
		prefix = parent + "/"
	}
	byName := map[string]listItem{}
	for p, entry := range entries {
		if parent != "" && p != parent && !strings.HasPrefix(p, prefix) {
			continue
		}
		if p == parent {
			continue
		}
		rest := strings.TrimPrefix(p, prefix)
		name := rest
		itemType := entry.EntryType
		if idx := strings.Index(rest, "/"); idx >= 0 {
			name = rest[:idx]
			itemType = "dir"
		}
		itemPath := name
		if parent != "" {
			itemPath = parent + "/" + name
		}
		item := listItem{Name: name, Path: itemPath, Type: itemType}
		if itemType == entry.EntryType && itemPath == entry.Path {
			item.Size = entry.Size
			item.Mime = entry.Mime
			item.FileType = entry.FileType
			item.Binary = entry.Binary
		}
		byName[name] = item
	}
	items := make([]listItem, 0, len(byName))
	for _, item := range byName {
		items = append(items, item)
	}
	sort.Slice(items, func(i, j int) bool { return items[i].Name < items[j].Name })
	return items
}

func directoryExists(dir string, entries map[string]mergedEntry) bool {
	if dir == "" || dir == "." {
		return true
	}
	if entry, ok := entries[dir]; ok {
		return entry.EntryType == "dir"
	}
	prefix := dir + "/"
	for p := range entries {
		if strings.HasPrefix(p, prefix) {
			return true
		}
	}
	return false
}

func draftEntryFromMerged(skillID, newPath string, entry mergedEntry, now time.Time) *skillDraftEntryRow {
	hash := entry.BlobHash
	return &skillDraftEntryRow{
		SkillID:   skillID,
		Path:      newPath,
		Op:        "upsert",
		EntryType: entry.EntryType,
		BlobHash:  hash,
		Size:      entry.Size,
		Mime:      entry.Mime,
		FileType:  entry.FileType,
		Binary:    entry.Binary,
		Mode:      entry.Mode,
		UpdatedAt: now,
	}
}

func requireUser(w http.ResponseWriter, r *http.Request) (string, bool) {
	userID := r.URL.Query().Get("user_id")
	if userID == "" {
		skillhttperr.ReplyWithCode(w, "user_id is required", http.StatusUnauthorized, skillhttperr.CodeUnauthenticated)
		return "", false
	}
	return userID, true
}

func requireWriteParams(w http.ResponseWriter, r *http.Request) (string, string, bool) {
	userID, ok := requireUser(w, r)
	if !ok {
		return "", "", false
	}
	taskID := r.URL.Query().Get("task_id")
	if taskID == "" {
		skillhttperr.Reply(w, "task_id is required", http.StatusBadRequest)
		return "", "", false
	}
	return userID, taskID, true
}

func writeJSON(w http.ResponseWriter, status int, value any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(value)
}

func writeHTTPError(w http.ResponseWriter, err error) {
	var httpErr httpError
	switch {
	case errors.As(err, &httpErr):
		skillhttperr.Reply(w, httpErr.Error(), httpErr.status)
	case errors.Is(err, gorm.ErrRecordNotFound):
		skillhttperr.ReplyWithCode(w, "not found", http.StatusNotFound, skillhttperr.CodeNotFound)
	default:
		skillhttperr.Reply(w, err.Error(), http.StatusInternalServerError)
	}
}

type httpError struct {
	status  int
	message string
}

func (e httpError) Error() string { return e.message }

func badRequest(message string) error {
	return httpError{status: http.StatusBadRequest, message: message}
}

func conflict(message string) error { return httpError{status: http.StatusConflict, message: message} }

func isAncestorPath(ancestor, child string) bool {
	return child != ancestor && strings.HasPrefix(child, ancestor+"/")
}

func isDescendantPath(parent, child string) bool {
	return child != parent && strings.HasPrefix(child, parent+"/")
}

type clock interface {
	Now() time.Time
}

type systemClock struct{}

func (systemClock) Now() time.Time { return time.Now() }

type skillRow struct {
	ID             string  `gorm:"column:id;type:varchar(36);primaryKey"`
	OwnerUserID    string  `gorm:"column:owner_user_id;type:text;not null"`
	Category       string  `gorm:"column:category;type:text;not null"`
	SkillName      string  `gorm:"column:skill_name;type:text;not null"`
	HeadRevisionID *string `gorm:"column:head_revision_id;type:varchar(36)"`
}

func (skillRow) TableName() string { return "skills" }

type skillBlobRow struct {
	Hash           string    `gorm:"column:hash;type:text;primaryKey"`
	Size           int64     `gorm:"column:size;not null"`
	Mime           string    `gorm:"column:mime;type:text"`
	FileType       string    `gorm:"column:file_type;type:text;not null;default:'unknown'"`
	Binary         bool      `gorm:"column:binary;not null;default:false"`
	StorageBackend string    `gorm:"column:storage_backend;type:text;not null"`
	StorageKey     *string   `gorm:"column:storage_key;type:text"`
	Content        []byte    `gorm:"column:content;type:blob"`
	CreatedAt      time.Time `gorm:"column:created_at;not null"`
}

func (skillBlobRow) TableName() string { return "skill_blobs" }

type skillRevisionEntryRow struct {
	RevisionID string  `gorm:"column:revision_id;type:varchar(36);primaryKey"`
	Path       string  `gorm:"column:path;type:text;primaryKey"`
	EntryType  string  `gorm:"column:entry_type;type:text;not null"`
	BlobHash   *string `gorm:"column:blob_hash;type:text"`
	Size       int64   `gorm:"column:size"`
	Mime       string  `gorm:"column:mime;type:text"`
	FileType   string  `gorm:"column:file_type;type:text;not null;default:'unknown'"`
	Binary     bool    `gorm:"column:binary;not null;default:false"`
	Mode       int     `gorm:"column:mode;not null;default:420"`
}

func (skillRevisionEntryRow) TableName() string { return "skill_revision_entries" }

type skillDraftRow struct {
	SkillID        string     `gorm:"column:skill_id;type:varchar(36);primaryKey"`
	BaseRevisionID *string    `gorm:"column:base_revision_id;type:varchar(36)"`
	TaskID         string     `gorm:"column:task_id;type:text;not null;default:''"`
	UpdatedBy      *string    `gorm:"column:updated_by;type:varchar(36)"`
	Version        int64      `gorm:"column:version;not null;default:1"`
	CreatedAt      time.Time  `gorm:"column:created_at;not null"`
	UpdatedAt      time.Time  `gorm:"column:updated_at;not null"`
	DraftUpdatedAt *time.Time `gorm:"column:draft_updated_at"`
}

func (skillDraftRow) TableName() string { return "skill_drafts" }

type skillDraftEntryRow struct {
	SkillID   string    `gorm:"column:skill_id;type:varchar(36);primaryKey"`
	Path      string    `gorm:"column:path;type:text;primaryKey"`
	Op        string    `gorm:"column:op;type:text;not null"`
	EntryType string    `gorm:"column:entry_type;type:text"`
	BlobHash  *string   `gorm:"column:blob_hash;type:text"`
	Size      int64     `gorm:"column:size"`
	Mime      string    `gorm:"column:mime;type:text"`
	FileType  string    `gorm:"column:file_type"`
	Binary    bool      `gorm:"column:binary"`
	Mode      int       `gorm:"column:mode"`
	UpdatedAt time.Time `gorm:"column:updated_at;not null"`
}

func (skillDraftEntryRow) TableName() string { return "skill_draft_entries" }

type mergedEntry struct {
	Path      string
	EntryType string
	BlobHash  *string
	Size      int64
	Mime      string
	FileType  string
	Binary    bool
	Mode      int
	FromHead  bool
	FromDraft bool
}
