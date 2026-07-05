package service

import (
	"context"
	"path/filepath"
	"testing"

	"gorm.io/gorm"
)

func TestCreateSkillFromURL_CreatesInitialRevision(t *testing.T) {
	db := newSkillV2TestDB(t)
	zipPath := filepath.Join(t.TempDir(), "url-skill.zip")
	writeSkillZip(t, zipPath, map[string][]byte{
		"SKILL.md":        []byte("# URL 导入\n"),
		"references/a.md": []byte("# 参考资料\n"),
		"assets/logo.png": minimalPNGBytes(),
	})
	downloader := NewFakeZipDownloader(map[string]string{
		"https://example.test/skill.zip": zipPath,
	})
	svc := NewSkillService(SkillServiceDeps{
		DB:         db,
		Downloader: downloader,
		BlobStore:  NewBlobStore(db, NewLocalObjectStore(t.TempDir())),
		Clock:      fixedClock(),
	})

	resp, err := svc.CreateSkill(context.Background(), CreateSkillRequest{
		OwnerUserID:    "user_001",
		OwnerUserName:  "张三",
		CreateUserID:   "user_001",
		CreateUserName: "张三",
		Name:           "URL 导入",
		Category:       "research",
		Source: SourceInput{
			Type: "url",
			URL:  "https://example.test/skill.zip",
		},
	})
	if err != nil {
		t.Fatalf("CreateSkill from URL returned error: %v", err)
	}
	assertInitialRevision(t, db, resp.SkillID, resp.HeadRevisionID)
	assertRevisionEntries(t, db, resp.HeadRevisionID)
	assertDraftInitialized(t, db, resp.SkillID, resp.HeadRevisionID)
	assertBlobRouting(t, db, resp.HeadRevisionID)
}

func TestCreateSkillFromURL_DownloadFailureDoesNotCreateSkill(t *testing.T) {
	db := newSkillV2TestDB(t)
	downloader := NewFakeZipDownloader(map[string]string{})
	downloader.Fail("https://example.test/timeout.zip")
	svc := NewSkillService(SkillServiceDeps{
		DB:         db,
		Downloader: downloader,
		BlobStore:  NewBlobStore(db, NewLocalObjectStore(t.TempDir())),
		Clock:      fixedClock(),
	})

	_, err := svc.CreateSkill(context.Background(), CreateSkillRequest{
		OwnerUserID:  "user_001",
		CreateUserID: "user_001",
		Name:         "下载失败",
		Category:     "research",
		Source: SourceInput{
			Type: "url",
			URL:  "https://example.test/timeout.zip",
		},
	})
	if err == nil {
		t.Fatal("CreateSkill from failing URL succeeded")
	}
	assertNoSkillTruthRows(t, db)
	if got := countRows(t, db, "skill_blobs", ""); got != 0 {
		t.Fatalf("skill_blobs count = %d, want 0", got)
	}
}

func TestReplaceSkillContentFromUploadedZip_CreatesNewRevision(t *testing.T) {
	db := newSkillV2TestDB(t)
	seedSkillWithHeadRevision(t, db, "skill1", "rev1")
	zipPath := filepath.Join(t.TempDir(), "replacement.zip")
	writeSkillZip(t, zipPath, map[string][]byte{
		"SKILL.md":        []byte("# 论文精读 v2\n"),
		"references/b.md": []byte("# 新资料\n"),
	})
	uploadStore := newFakeUploadStore()
	uploadStore.Put(UploadSession{
		UploadID:    "upload_replace",
		OwnerUserID: "user_001",
		State:       "completed",
		StoredPath:  zipPath,
		Filename:    "replacement.zip",
	})
	svc := NewSkillService(SkillServiceDeps{
		DB:          db,
		UploadStore: uploadStore,
		BlobStore:   NewBlobStore(db, NewLocalObjectStore(t.TempDir())),
		Clock:       fixedClock(),
	})

	resp, err := svc.PatchSkill(context.Background(), PatchSkillRequest{
		SkillID: "skill1",
		UserID:  "user_001",
		Source: &SourceInput{
			Type:     "uploaded_zip",
			UploadID: "upload_replace",
			Filename: "replacement.zip",
		},
	})
	if err != nil {
		t.Fatalf("PatchSkill source replacement returned error: %v", err)
	}
	if resp.HeadRevisionID == "rev1" {
		t.Fatal("source replacement did not create a new head revision")
	}
	assertInitialReplacementRevision(t, db, "skill1", resp.HeadRevisionID)
	assertDraftInitialized(t, db, "skill1", resp.HeadRevisionID)
}

func TestReplaceSkillContent_RejectsWhenDraftExists(t *testing.T) {
	db := newSkillV2TestDB(t)
	seedSkillWithHeadRevision(t, db, "skill1", "rev1")
	now := fixedClock().Now()
	if err := db.Create(&testSkillV2DraftEntryRow{
		SkillID:   "skill1",
		Path:      "SKILL.md",
		Op:        "upsert",
		EntryType: "file",
		UpdatedAt: now,
	}).Error; err != nil {
		t.Fatalf("seed draft entry: %v", err)
	}
	svc := NewSkillService(SkillServiceDeps{DB: db, BlobStore: NewBlobStore(db, NewLocalObjectStore(t.TempDir())), Clock: fixedClock()})

	_, err := svc.PatchSkill(context.Background(), PatchSkillRequest{
		SkillID: "skill1",
		UserID:  "user_001",
		Source:  &SourceInput{Type: "url", URL: "https://example.test/skill.zip"},
	})
	if err == nil {
		t.Fatal("PatchSkill source replacement succeeded while draft overlay exists")
	}
	assertHeadRevisionUnchanged(t, db, "skill1", "rev1")
	if got := countRows(t, db, "skill_draft_entries", "skill_id = ?", "skill1"); got != 1 {
		t.Fatalf("skill_draft_entries count = %d, want 1", got)
	}
}

func TestDeleteSkill_RemovesSkillGraphAndKeepsSharedBlob(t *testing.T) {
	db := newSkillV2TestDB(t)
	seedSkillWithHeadRevision(t, db, "skill1", "rev1")
	seedSkillWithHeadRevision(t, db, "skill2", "rev2")
	sharedHash := "h_shared"
	seedSharedBlobReference(t, db, "rev1", sharedHash)
	seedSharedBlobReference(t, db, "rev2", sharedHash)
	svc := NewSkillService(SkillServiceDeps{DB: db, BlobStore: NewBlobStore(db, NewLocalObjectStore(t.TempDir())), Clock: fixedClock()})

	if err := svc.DeleteSkill(context.Background(), DeleteSkillRequest{SkillID: "skill1", UserID: "user_001"}); err != nil {
		t.Fatalf("DeleteSkill returned error: %v", err)
	}
	for _, table := range []string{"skills", "skill_drafts", "skill_draft_entries", "skill_revisions", "skill_revision_entries"} {
		if got := countRows(t, db, table, "skill_id = ? OR id = ? OR revision_id = ?", "skill1", "skill1", "rev1"); got != 0 {
			t.Fatalf("%s retained skill1 rows: %d", table, got)
		}
	}
	if got := countRows(t, db, "skill_blobs", "hash = ?", sharedHash); got != 1 {
		t.Fatalf("shared blob count = %d, want 1", got)
	}
	if _, err := svc.ReadFile(context.Background(), FileRef{SkillID: "skill2", RefType: "head", Path: "shared.md"}); err != nil {
		t.Fatalf("skill2 shared file became unreadable: %v", err)
	}
}

func TestListAndGetSkill_ReturnsMetadataAndDraftSummary(t *testing.T) {
	db := newSkillV2TestDB(t)
	seedSkillWithHeadRevision(t, db, "skill1", "rev1")
	seedSkillWithHeadRevision(t, db, "skill2", "rev2")
	if err := db.Model(&testSkillV2DraftRow{}).Where("skill_id = ?", "skill1").Updates(map[string]any{
		"task_id": "task1",
		"version": 3,
	}).Error; err != nil {
		t.Fatalf("update draft fixture: %v", err)
	}
	if err := db.Create(&testSkillV2DraftEntryRow{
		SkillID:   "skill1",
		Path:      "SKILL.md",
		Op:        "upsert",
		EntryType: "file",
		UpdatedAt: fixedClock().Now(),
	}).Error; err != nil {
		t.Fatalf("seed draft overlay: %v", err)
	}
	svc := NewSkillService(SkillServiceDeps{DB: db, BlobStore: NewBlobStore(db, NewLocalObjectStore(t.TempDir())), Clock: fixedClock()})

	list, err := svc.ListSkills(context.Background(), ListSkillsRequest{UserID: "user_001"})
	if err != nil {
		t.Fatalf("ListSkills returned error: %v", err)
	}
	if len(list.Items) != 2 {
		t.Fatalf("ListSkills returned %d items, want 2", len(list.Items))
	}
	for _, item := range list.Items {
		if item.FileContent != "" {
			t.Fatalf("list item returned file content: %#v", item)
		}
		if item.HeadRevisionID == "" {
			t.Fatalf("list item missing head revision id: %#v", item)
		}
	}

	detail, err := svc.GetSkill(context.Background(), GetSkillRequest{SkillID: "skill1", UserID: "user_001"})
	if err != nil {
		t.Fatalf("GetSkill returned error: %v", err)
	}
	if !detail.Draft.HasUncommittedDraft || detail.Draft.TaskID != "task1" {
		t.Fatalf("unexpected draft summary: %#v", detail.Draft)
	}
}

func TestAutoEvo_WritesDraftOverlay(t *testing.T) {
	db := newSkillV2TestDB(t)
	seedSkillWithHeadRevision(t, db, "skill1", "rev1")
	svc := NewSkillService(SkillServiceDeps{DB: db, BlobStore: NewBlobStore(db, NewLocalObjectStore(t.TempDir())), Clock: fixedClock()})

	if err := svc.ApplyAutoEvoDraft(context.Background(), AutoEvoDraftRequest{
		SkillID:        "skill1",
		ConversationID: "conv_auto",
		Files: map[string][]byte{
			"SKILL.md": []byte("# 自动演进草稿\n"),
		},
	}); err != nil {
		t.Fatalf("ApplyAutoEvoDraft returned error: %v", err)
	}
	assertHeadRevisionUnchanged(t, db, "skill1", "rev1")
	if got := countRows(t, db, "skill_draft_entries", "skill_id = ?", "skill1"); got != 1 {
		t.Fatalf("skill_draft_entries count = %d, want 1", got)
	}
}

func TestReviewAccept_CommitsDraftOrCreatesRevision(t *testing.T) {
	db := newSkillV2TestDB(t)
	seedSkillWithHeadRevision(t, db, "skill1", "rev1")
	svc := NewSkillService(SkillServiceDeps{DB: db, BlobStore: NewBlobStore(db, NewLocalObjectStore(t.TempDir())), Clock: fixedClock()})

	resp, err := svc.AcceptReview(context.Background(), AcceptReviewRequest{
		SkillID:  "skill1",
		UserID:   "user_001",
		ReviewID: "review1",
		Files: map[string][]byte{
			"SKILL.md": []byte("# Review 接受\n"),
		},
	})
	if err != nil {
		t.Fatalf("AcceptReview returned error: %v", err)
	}
	if resp.HeadRevisionID == "" || resp.HeadRevisionID == "rev1" {
		t.Fatalf("AcceptReview did not create a new head revision: %#v", resp)
	}
	assertNoDraftEntries(t, db, "skill1")
}

func assertInitialReplacementRevision(t *testing.T, db *gorm.DB, skillID, revisionID string) {
	t.Helper()
	var revision testSkillV2RevisionRow
	if err := db.Where("id = ?", revisionID).Take(&revision).Error; err != nil {
		t.Fatalf("query replacement revision: %v", err)
	}
	if revision.SkillID != skillID || revision.RevisionNo != 2 || revision.ParentRevisionID == nil || *revision.ParentRevisionID != "rev1" {
		t.Fatalf("unexpected replacement revision: %#v", revision)
	}
	entries := listRevisionEntries(t, db, revisionID)
	if _, ok := entries["references/b.md"]; !ok {
		t.Fatalf("replacement revision missing references/b.md: %#v", entries)
	}
}

func assertHeadRevisionUnchanged(t *testing.T, db *gorm.DB, skillID, revisionID string) {
	t.Helper()
	var skill testSkillV2SkillRow
	if err := db.Where("id = ?", skillID).Take(&skill).Error; err != nil {
		t.Fatalf("query skill: %v", err)
	}
	if skill.HeadRevisionID == nil || *skill.HeadRevisionID != revisionID {
		t.Fatalf("head_revision_id = %v, want %q", skill.HeadRevisionID, revisionID)
	}
}

func seedSharedBlobReference(t *testing.T, db *gorm.DB, revisionID, blobHash string) {
	t.Helper()
	now := fixedClock().Now()
	var blob testSkillV2BlobRow
	err := db.Where("hash = ?", blobHash).Take(&blob).Error
	if err != nil {
		if err := db.Create(&testSkillV2BlobRow{
			Hash:           blobHash,
			Size:           6,
			Mime:           "text/markdown",
			FileType:       "markdown",
			Binary:         false,
			StorageBackend: "postgres",
			Content:        []byte("shared"),
			CreatedAt:      now,
		}).Error; err != nil {
			t.Fatalf("seed shared blob: %v", err)
		}
	}
	if err := db.Create(&testSkillV2RevisionEntryRow{
		RevisionID: revisionID,
		Path:       "shared.md",
		EntryType:  "file",
		BlobHash:   &blobHash,
		Size:       6,
		Mime:       "text/markdown",
		FileType:   "markdown",
		Mode:       420,
	}).Error; err != nil {
		t.Fatalf("seed shared revision entry: %v", err)
	}
}
