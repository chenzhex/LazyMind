package diff

import (
	"archive/zip"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"html"
	"io"
	"path"
	"path/filepath"
	"reflect"
	"sort"
	"strings"
	"unicode/utf8"

	"gorm.io/gorm"
)

const defaultMaxTextBytes = 512 * 1024
const maxInlineDiffCells = 20000

type ServiceDeps struct {
	MaxTextBytes int
}

type Service struct {
	maxTextBytes int
}

type ReadOnlySkillFS interface {
	ListAll(ctx context.Context) ([]EntryInfo, error)
	ReadFile(ctx context.Context, path string) ([]byte, error)
}

type EntryInfo struct {
	Path     string
	Type     string
	BlobHash string
	Binary   bool
	FileType string
	Size     int64
}

type DiffOptions struct {
	Path         string
	ContextLines int
	Mode         string
	OldStart     int
	NewStart     int
	Lines        int
}

type SkillDiff struct {
	Files        []DiffFile
	CacheWritten bool
}

type DiffFile struct {
	Path           string
	Type           string
	Status         string
	Binary         bool
	TooLarge       bool
	CacheWritten   bool
	DiffEntryLines []DiffEntryLine
}

type DiffEntryLine struct {
	Type                    string
	Text                    string
	HTML                    string
	OldLine                 int
	NewLine                 int
	DisplayNoNewLineWarning bool
}

func NewService(deps ServiceDeps) *Service {
	maxTextBytes := deps.MaxTextBytes
	if maxTextBytes <= 0 {
		maxTextBytes = defaultMaxTextBytes
	}
	return &Service{maxTextBytes: maxTextBytes}
}

func (s *Service) Compare(ctx context.Context, oldFS, newFS ReadOnlySkillFS, opts DiffOptions) (SkillDiff, error) {
	oldEntries, err := oldFS.ListAll(ctx)
	if err != nil {
		return SkillDiff{}, err
	}
	newEntries, err := newFS.ListAll(ctx)
	if err != nil {
		return SkillDiff{}, err
	}

	oldByPath := entriesByPath(oldEntries)
	newByPath := entriesByPath(newEntries)
	paths := unionPaths(oldByPath, newByPath)

	files := make([]DiffFile, 0, len(paths))
	for _, path := range paths {
		oldEntry, oldOK := oldByPath[path]
		newEntry, newOK := newByPath[path]
		file := DiffFile{Path: path}
		switch {
		case !oldOK:
			file.Type = newEntry.Type
			file.Status = "added"
			file.Binary = newEntry.Binary
		case !newOK:
			file.Type = oldEntry.Type
			file.Status = "deleted"
			file.Binary = oldEntry.Binary
		default:
			file.Type = newEntry.Type
			file.Binary = newEntry.Binary
			if oldEntry.Type == newEntry.Type && oldEntry.BlobHash == newEntry.BlobHash {
				file.Status = "unchanged"
			} else {
				file.Status = "modified"
			}
		}
		files = append(files, file)
	}

	return SkillDiff{Files: files}, nil
}

func (s *Service) CompareFile(ctx context.Context, oldFS, newFS ReadOnlySkillFS, opts DiffOptions) (DiffFile, error) {
	tree, err := s.Compare(ctx, oldFS, newFS, opts)
	if err != nil {
		return DiffFile{}, err
	}
	result := DiffFile{Path: opts.Path, Status: "unchanged", Type: "file"}
	for _, file := range tree.Files {
		if file.Path == opts.Path {
			result = file
			break
		}
	}
	if result.Type == "dir" || result.Binary {
		return result, nil
	}

	var oldBytes, newBytes []byte
	if result.Status != "added" {
		var oldErr error
		oldBytes, oldErr = oldFS.ReadFile(ctx, opts.Path)
		if oldErr != nil {
			return DiffFile{}, oldErr
		}
	}
	if result.Status != "deleted" {
		var newErr error
		newBytes, newErr = newFS.ReadFile(ctx, opts.Path)
		if newErr != nil {
			return DiffFile{}, newErr
		}
	}
	if len(oldBytes) > s.maxTextBytes || len(newBytes) > s.maxTextBytes {
		result.TooLarge = true
		return result, nil
	}
	if !utf8.Valid(oldBytes) || !utf8.Valid(newBytes) {
		result.Binary = true
		return result, nil
	}

	if strings.EqualFold(opts.Mode, "context") {
		text := injectedContextText(string(newBytes), opts)
		result.DiffEntryLines = []DiffEntryLine{{
			Type:    "INJECTED_CONTEXT",
			Text:    text,
			HTML:    html.EscapeString(text),
			NewLine: opts.NewStart,
			OldLine: opts.OldStart,
		}}
		return result, nil
	}

	result.DiffEntryLines = buildTextDiff(string(oldBytes), string(newBytes))
	return result, nil
}

func entriesByPath(entries []EntryInfo) map[string]EntryInfo {
	byPath := make(map[string]EntryInfo, len(entries))
	for _, entry := range entries {
		byPath[entry.Path] = entry
	}
	return byPath
}

func unionPaths(oldByPath, newByPath map[string]EntryInfo) []string {
	seen := map[string]bool{}
	paths := make([]string, 0, len(oldByPath)+len(newByPath))
	for path := range oldByPath {
		seen[path] = true
		paths = append(paths, path)
	}
	for path := range newByPath {
		if !seen[path] {
			paths = append(paths, path)
		}
	}
	sort.Strings(paths)
	return paths
}

func buildTextDiff(oldText, newText string) []DiffEntryLine {
	oldLines, oldHasFinalNewline := splitLines(oldText)
	newLines, newHasFinalNewline := splitLines(newText)
	lines := []DiffEntryLine{{Type: "HUNK", Text: "@@ -1 +1 @@", HTML: "@@ -1 +1 @@"}}
	oldLine, newLine := 1, 1
	for len(oldLines) > 0 || len(newLines) > 0 {
		switch {
		case len(oldLines) > 0 && len(newLines) > 0 && oldLines[0] == newLines[0]:
			lines = append(lines, diffLine("CONTEXT", oldLines[0], oldLine, newLine))
			oldLines = oldLines[1:]
			newLines = newLines[1:]
			oldLine++
			newLine++
		case len(oldLines) > 0 && len(newLines) > 0:
			deletion, addition := changedLinePair(oldLines[0], newLines[0], oldLine, newLine)
			lines = append(lines, deletion, addition)
			oldLines = oldLines[1:]
			newLines = newLines[1:]
			oldLine++
			newLine++
		case len(oldLines) > 0:
			lines = append(lines, highlightedDiffLine("DELETION", oldLines[0], oldLine, 0, "diff-deletion"))
			oldLines = oldLines[1:]
			oldLine++
		default:
			lines = append(lines, highlightedDiffLine("ADDITION", newLines[0], 0, newLine, "diff-addition"))
			newLines = newLines[1:]
			newLine++
		}
	}
	if oldHasFinalNewline != newHasFinalNewline && len(lines) > 0 {
		lines[len(lines)-1].DisplayNoNewLineWarning = true
	}
	return lines
}

func splitLines(text string) ([]string, bool) {
	if text == "" {
		return nil, true
	}
	hasFinalNewline := strings.HasSuffix(text, "\n")
	parts := strings.Split(strings.TrimSuffix(text, "\n"), "\n")
	return parts, hasFinalNewline
}

func diffLine(typ, text string, oldLine, newLine int) DiffEntryLine {
	return DiffEntryLine{
		Type:    typ,
		Text:    text,
		HTML:    html.EscapeString(text),
		OldLine: oldLine,
		NewLine: newLine,
	}
}

func highlightedDiffLine(typ, text string, oldLine, newLine int, class string) DiffEntryLine {
	line := diffLine(typ, text, oldLine, newLine)
	line.HTML = wrapDiffSpan(class, html.EscapeString(text))
	return line
}

func changedLinePair(oldText, newText string, oldLine, newLine int) (DiffEntryLine, DiffEntryLine) {
	oldHTML, newHTML := inlineDiffHTML(oldText, newText)
	deletion := diffLine("DELETION", oldText, oldLine, 0)
	addition := diffLine("ADDITION", newText, 0, newLine)
	deletion.HTML = oldHTML
	addition.HTML = newHTML
	return deletion, addition
}

func inlineDiffHTML(oldText, newText string) (string, string) {
	oldRunes := []rune(oldText)
	newRunes := []rune(newText)
	if len(oldRunes) == 0 {
		return "", wrapDiffSpan("diff-addition", html.EscapeString(newText))
	}
	if len(newRunes) == 0 {
		return wrapDiffSpan("diff-deletion", html.EscapeString(oldText)), ""
	}
	if len(oldRunes)*len(newRunes) > maxInlineDiffCells {
		return wrapDiffSpan("diff-deletion", html.EscapeString(oldText)), wrapDiffSpan("diff-addition", html.EscapeString(newText))
	}

	oldChanged, newChanged := changedRuneMasks(oldRunes, newRunes)
	return renderInlineDiffHTML(oldRunes, oldChanged, "diff-deletion"), renderInlineDiffHTML(newRunes, newChanged, "diff-addition")
}

func changedRuneMasks(oldRunes, newRunes []rune) ([]bool, []bool) {
	m, n := len(oldRunes), len(newRunes)
	lcs := make([][]int, m+1)
	for i := range lcs {
		lcs[i] = make([]int, n+1)
	}
	for i := m - 1; i >= 0; i-- {
		for j := n - 1; j >= 0; j-- {
			if oldRunes[i] == newRunes[j] {
				lcs[i][j] = lcs[i+1][j+1] + 1
				continue
			}
			if lcs[i+1][j] >= lcs[i][j+1] {
				lcs[i][j] = lcs[i+1][j]
			} else {
				lcs[i][j] = lcs[i][j+1]
			}
		}
	}

	oldChanged := make([]bool, m)
	newChanged := make([]bool, n)
	for i := range oldChanged {
		oldChanged[i] = true
	}
	for i := range newChanged {
		newChanged[i] = true
	}
	for i, j := 0, 0; i < m && j < n; {
		if oldRunes[i] == newRunes[j] {
			oldChanged[i] = false
			newChanged[j] = false
			i++
			j++
			continue
		}
		if lcs[i+1][j] >= lcs[i][j+1] {
			i++
		} else {
			j++
		}
	}
	return oldChanged, newChanged
}

func renderInlineDiffHTML(runes []rune, changed []bool, class string) string {
	var builder strings.Builder
	for i := 0; i < len(runes); {
		j := i + 1
		for j < len(runes) && changed[j] == changed[i] {
			j++
		}
		escaped := html.EscapeString(string(runes[i:j]))
		if changed[i] {
			builder.WriteString(wrapDiffSpan(class, escaped))
		} else {
			builder.WriteString(escaped)
		}
		i = j
	}
	return builder.String()
}

func wrapDiffSpan(class, escapedText string) string {
	return `<span class="` + class + `">` + escapedText + `</span>`
}

func injectedContextText(text string, opts DiffOptions) string {
	lines, _ := splitLines(text)
	if len(lines) == 0 {
		return ""
	}
	start := opts.NewStart
	if start <= 0 {
		start = 1
	}
	count := opts.Lines
	if count <= 0 {
		count = 1
	}
	start--
	if start >= len(lines) {
		return ""
	}
	end := start + count
	if end > len(lines) {
		end = len(lines)
	}
	return strings.Join(lines[start:end], "\n")
}

type RefResolverDeps struct {
	DB          *gorm.DB
	UploadStore any
}

type RefResolver struct {
	db          *gorm.DB
	uploadStore any
}

type ResolvePairRequest struct {
	UserID string
	Old    DiffRef
	New    DiffRef
}

type DiffRef struct {
	Type       string
	SkillID    string
	RevisionID string
	UploadID   string
}

type UploadSession struct {
	UploadID    string
	OwnerUserID string
	State       string
	StoredPath  string
	Filename    string
}

func NewRefResolver(deps RefResolverDeps) *RefResolver {
	return &RefResolver{db: deps.DB, uploadStore: deps.UploadStore}
}

func (r *RefResolver) ResolvePair(ctx context.Context, req ResolvePairRequest) (ReadOnlySkillFS, ReadOnlySkillFS, error) {
	oldFS, err := r.Resolve(ctx, req.UserID, req.Old)
	if err != nil {
		return nil, nil, err
	}
	newFS, err := r.Resolve(ctx, req.UserID, req.New)
	if err != nil {
		return nil, nil, err
	}
	return oldFS, newFS, nil
}

func (r *RefResolver) Resolve(ctx context.Context, userID string, ref DiffRef) (ReadOnlySkillFS, error) {
	refType := strings.ToLower(strings.TrimSpace(ref.Type))
	switch refType {
	case "revision":
		if ref.SkillID == "" || ref.RevisionID == "" {
			return nil, fmt.Errorf("revision ref requires skill_id and revision_id")
		}
		return newRevisionFS(ctx, r.db, ref.SkillID, ref.RevisionID)
	case "head":
		if ref.SkillID == "" {
			return nil, fmt.Errorf("head ref requires skill_id")
		}
		revisionID, err := headRevisionID(ctx, r.db, ref.SkillID)
		if err != nil {
			return nil, err
		}
		return newRevisionFS(ctx, r.db, ref.SkillID, revisionID)
	case "draft":
		if ref.SkillID == "" {
			return nil, fmt.Errorf("draft ref requires skill_id")
		}
		return newDraftFS(ctx, r.db, ref.SkillID)
	case "uploaded":
		if ref.UploadID == "" {
			return nil, fmt.Errorf("uploaded ref requires upload_id")
		}
		session, err := getUploadSession(ctx, r.uploadStore, ref.UploadID)
		if err != nil {
			return nil, err
		}
		if session.OwnerUserID != userID {
			return nil, fmt.Errorf("upload belongs to another user")
		}
		if session.State != "completed" {
			return nil, fmt.Errorf("upload is not completed")
		}
		return newZipFS(session.StoredPath)
	default:
		return nil, fmt.Errorf("unsupported diff ref type %q", ref.Type)
	}
}

type revisionFS struct {
	db         *gorm.DB
	skillID    string
	revisionID string
}

func newRevisionFS(ctx context.Context, db *gorm.DB, skillID, revisionID string) (*revisionFS, error) {
	if db == nil {
		return nil, fmt.Errorf("db is not configured")
	}
	var row skillRevisionRow
	if err := db.WithContext(ctx).Where("id = ? AND skill_id = ?", revisionID, skillID).Take(&row).Error; err != nil {
		return nil, err
	}
	return &revisionFS{db: db, skillID: skillID, revisionID: revisionID}, nil
}

func (fs *revisionFS) ListAll(ctx context.Context) ([]EntryInfo, error) {
	var rows []skillRevisionEntryRow
	if err := fs.db.WithContext(ctx).Where("revision_id = ?", fs.revisionID).Order("path ASC").Find(&rows).Error; err != nil {
		return nil, err
	}
	entries := make([]EntryInfo, 0, len(rows))
	for _, row := range rows {
		blobHash := ""
		if row.BlobHash != nil {
			blobHash = *row.BlobHash
		}
		entries = append(entries, EntryInfo{
			Path:     row.Path,
			Type:     row.EntryType,
			BlobHash: blobHash,
			Binary:   row.Binary,
			FileType: row.FileType,
			Size:     row.Size,
		})
	}
	return entries, nil
}

func (fs *revisionFS) ReadFile(ctx context.Context, filePath string) ([]byte, error) {
	var entry skillRevisionEntryRow
	if err := fs.db.WithContext(ctx).Where("revision_id = ? AND path = ?", fs.revisionID, filePath).Take(&entry).Error; err != nil {
		return nil, err
	}
	return readRevisionEntryBlob(ctx, fs.db, entry, filePath)
}

type draftFS struct {
	db      *gorm.DB
	entries []skillRevisionEntryRow
}

func newDraftFS(ctx context.Context, db *gorm.DB, skillID string) (*draftFS, error) {
	if db == nil {
		return nil, fmt.Errorf("db is not configured")
	}
	entries, err := draftEntriesForSkill(ctx, db, skillID)
	if err != nil {
		return nil, err
	}
	return &draftFS{db: db, entries: entries}, nil
}

func (fs *draftFS) ListAll(ctx context.Context) ([]EntryInfo, error) {
	entries := make([]EntryInfo, 0, len(fs.entries))
	for _, row := range fs.entries {
		blobHash := ""
		if row.BlobHash != nil {
			blobHash = *row.BlobHash
		}
		entries = append(entries, EntryInfo{
			Path:     row.Path,
			Type:     row.EntryType,
			BlobHash: blobHash,
			Binary:   row.Binary,
			FileType: row.FileType,
			Size:     row.Size,
		})
	}
	return entries, nil
}

func (fs *draftFS) ReadFile(ctx context.Context, filePath string) ([]byte, error) {
	for _, entry := range fs.entries {
		if entry.Path == filePath {
			return readRevisionEntryBlob(ctx, fs.db, entry, filePath)
		}
	}
	return nil, fmt.Errorf("file not found: %s", filePath)
}

func readRevisionEntryBlob(ctx context.Context, db *gorm.DB, entry skillRevisionEntryRow, filePath string) ([]byte, error) {
	if entry.EntryType != "file" || entry.BlobHash == nil {
		return nil, fmt.Errorf("file not found: %s", filePath)
	}
	var blob skillBlobRow
	if err := db.WithContext(ctx).Where("hash = ?", *entry.BlobHash).Take(&blob).Error; err != nil {
		return nil, err
	}
	if blob.Binary {
		return nil, fmt.Errorf("binary content is not available: %s", filePath)
	}
	return blob.Content, nil
}

func draftEntriesForSkill(ctx context.Context, db *gorm.DB, skillID string) ([]skillRevisionEntryRow, error) {
	revisionID, err := headRevisionID(ctx, db, skillID)
	if err != nil {
		return nil, err
	}
	var rows []skillRevisionEntryRow
	if err := db.WithContext(ctx).Where("revision_id = ?", revisionID).Order("path ASC").Find(&rows).Error; err != nil {
		return nil, err
	}
	entriesByPath := make(map[string]skillRevisionEntryRow, len(rows))
	for _, row := range rows {
		entriesByPath[row.Path] = row
	}

	var overlays []skillDraftEntryRow
	if err := db.WithContext(ctx).Where("skill_id = ?", skillID).Order("path ASC").Find(&overlays).Error; err != nil {
		return nil, err
	}
	for _, overlay := range overlays {
		if overlay.Op == "delete" {
			for entryPath := range entriesByPath {
				if entryPath == overlay.Path || isDescendantPath(overlay.Path, entryPath) {
					delete(entriesByPath, entryPath)
				}
			}
			continue
		}
		hash := overlay.BlobHash
		entriesByPath[overlay.Path] = skillRevisionEntryRow{
			Path:      overlay.Path,
			EntryType: overlay.EntryType,
			BlobHash:  hash,
			Size:      overlay.Size,
			Mime:      overlay.Mime,
			FileType:  overlay.FileType,
			Binary:    overlay.Binary,
			Mode:      overlay.Mode,
		}
	}
	return revisionEntriesFromMap(entriesByPath), nil
}

func revisionEntriesFromMap(entries map[string]skillRevisionEntryRow) []skillRevisionEntryRow {
	paths := make([]string, 0, len(entries))
	for entryPath := range entries {
		paths = append(paths, entryPath)
	}
	sort.Strings(paths)
	out := make([]skillRevisionEntryRow, 0, len(paths))
	for _, entryPath := range paths {
		out = append(out, entries[entryPath])
	}
	return out
}

type zipFS struct {
	entries []EntryInfo
	files   map[string][]byte
}

func newZipFS(zipPath string) (*zipFS, error) {
	reader, err := zip.OpenReader(zipPath)
	if err != nil {
		return nil, err
	}
	defer reader.Close()

	files := map[string][]byte{}
	dirs := map[string]bool{}
	for _, entry := range reader.File {
		name := strings.TrimSuffix(entry.Name, "/")
		if entry.FileInfo().IsDir() {
			cleaned, err := cleanSkillPath(name)
			if err != nil {
				return nil, err
			}
			dirs[cleaned] = true
			continue
		}
		cleaned, err := cleanSkillPath(entry.Name)
		if err != nil {
			return nil, err
		}
		rc, err := entry.Open()
		if err != nil {
			return nil, err
		}
		data, readErr := io.ReadAll(rc)
		closeErr := rc.Close()
		if readErr != nil {
			return nil, readErr
		}
		if closeErr != nil {
			return nil, closeErr
		}
		files[cleaned] = data
		for dir := path.Dir(cleaned); dir != "."; dir = path.Dir(dir) {
			dirs[dir] = true
		}
	}

	entries := make([]EntryInfo, 0, len(dirs)+len(files))
	for dir := range dirs {
		entries = append(entries, EntryInfo{Path: dir, Type: "dir", FileType: "directory"})
	}
	for filePath, data := range files {
		entries = append(entries, EntryInfo{
			Path:     filePath,
			Type:     "file",
			BlobHash: contentHash(data),
			Binary:   isBinaryFile(filePath, data),
			FileType: fileTypeForPath(filePath),
			Size:     int64(len(data)),
		})
	}
	sort.Slice(entries, func(i, j int) bool { return entries[i].Path < entries[j].Path })
	return &zipFS{entries: entries, files: files}, nil
}

func (fs *zipFS) ListAll(ctx context.Context) ([]EntryInfo, error) {
	return append([]EntryInfo(nil), fs.entries...), nil
}

func (fs *zipFS) ReadFile(ctx context.Context, filePath string) ([]byte, error) {
	data, ok := fs.files[filePath]
	if !ok {
		return nil, fmt.Errorf("file not found: %s", filePath)
	}
	return append([]byte(nil), data...), nil
}

type skillRow struct {
	ID             string  `gorm:"column:id;type:varchar(36);primaryKey"`
	HeadRevisionID *string `gorm:"column:head_revision_id;type:varchar(36)"`
}

func (skillRow) TableName() string { return "skills" }

type skillRevisionRow struct {
	ID      string `gorm:"column:id;type:varchar(36);primaryKey"`
	SkillID string `gorm:"column:skill_id;type:varchar(36);not null"`
}

func (skillRevisionRow) TableName() string { return "skill_revisions" }

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

type skillDraftEntryRow struct {
	SkillID   string  `gorm:"column:skill_id;type:varchar(36);primaryKey"`
	Path      string  `gorm:"column:path;type:text;primaryKey"`
	Op        string  `gorm:"column:op;type:text;not null"`
	EntryType string  `gorm:"column:entry_type;type:text"`
	BlobHash  *string `gorm:"column:blob_hash;type:text"`
	Size      int64   `gorm:"column:size"`
	Mime      string  `gorm:"column:mime;type:text"`
	FileType  string  `gorm:"column:file_type;type:text"`
	Binary    bool    `gorm:"column:binary"`
	Mode      int     `gorm:"column:mode"`
}

func (skillDraftEntryRow) TableName() string { return "skill_draft_entries" }

type skillBlobRow struct {
	Hash    string `gorm:"column:hash;type:text;primaryKey"`
	Binary  bool   `gorm:"column:binary;not null;default:false"`
	Content []byte `gorm:"column:content;type:blob"`
}

func (skillBlobRow) TableName() string { return "skill_blobs" }

func headRevisionID(ctx context.Context, db *gorm.DB, skillID string) (string, error) {
	if db == nil {
		return "", fmt.Errorf("db is not configured")
	}
	var skill skillRow
	if err := db.WithContext(ctx).Where("id = ?", skillID).Take(&skill).Error; err != nil {
		return "", err
	}
	if skill.HeadRevisionID == nil || *skill.HeadRevisionID == "" {
		return "", fmt.Errorf("skill has no head revision")
	}
	return *skill.HeadRevisionID, nil
}

func getUploadSession(ctx context.Context, store any, uploadID string) (UploadSession, error) {
	if store == nil {
		return UploadSession{}, fmt.Errorf("upload store is not configured")
	}
	method := reflect.ValueOf(store).MethodByName("Get")
	if !method.IsValid() {
		return UploadSession{}, fmt.Errorf("upload store does not implement Get")
	}
	out := method.Call([]reflect.Value{reflect.ValueOf(ctx), reflect.ValueOf(uploadID)})
	if len(out) != 2 {
		return UploadSession{}, fmt.Errorf("upload store Get has invalid signature")
	}
	if errValue := out[1]; !errValue.IsNil() {
		err, ok := errValue.Interface().(error)
		if !ok {
			return UploadSession{}, errors.New("upload store returned non-error failure")
		}
		return UploadSession{}, err
	}
	return uploadSessionFromValue(out[0])
}

func uploadSessionFromValue(value reflect.Value) (UploadSession, error) {
	if value.Kind() == reflect.Pointer {
		value = value.Elem()
	}
	if value.Kind() != reflect.Struct {
		return UploadSession{}, fmt.Errorf("upload store returned invalid session")
	}
	return UploadSession{
		UploadID:    stringField(value, "UploadID"),
		OwnerUserID: stringField(value, "OwnerUserID"),
		State:       stringField(value, "State"),
		StoredPath:  stringField(value, "StoredPath"),
		Filename:    stringField(value, "Filename"),
	}, nil
}

func stringField(value reflect.Value, name string) string {
	field := value.FieldByName(name)
	if !field.IsValid() || field.Kind() != reflect.String {
		return ""
	}
	return field.String()
}

func cleanSkillPath(name string) (string, error) {
	if name == "" || strings.HasPrefix(name, "/") || strings.Contains(name, `\`) || strings.Contains(name, "//") {
		return "", fmt.Errorf("unsafe path %q", name)
	}
	cleaned := path.Clean(name)
	if cleaned == "." || cleaned != name || strings.HasPrefix(cleaned, "../") || cleaned == ".." {
		return "", fmt.Errorf("unsafe path %q", name)
	}
	for _, part := range strings.Split(cleaned, "/") {
		if part == "" || part == "." || part == ".." {
			return "", fmt.Errorf("unsafe path %q", name)
		}
	}
	return cleaned, nil
}

func contentHash(data []byte) string {
	sum := sha256.Sum256(data)
	return hex.EncodeToString(sum[:])
}

func isDescendantPath(parent, child string) bool {
	return child != parent && strings.HasPrefix(child, parent+"/")
}

func isBinaryFile(filePath string, data []byte) bool {
	if !utf8.Valid(data) {
		return true
	}
	if strings.ContainsRune(string(data), '\x00') {
		return true
	}
	switch strings.ToLower(filepath.Ext(filePath)) {
	case ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".pdf", ".zip":
		return true
	default:
		return false
	}
}

func fileTypeForPath(filePath string) string {
	switch strings.ToLower(filepath.Ext(filePath)) {
	case ".md", ".markdown":
		return "markdown"
	case ".txt":
		return "text"
	case ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico":
		return "image"
	default:
		return "unknown"
	}
}
