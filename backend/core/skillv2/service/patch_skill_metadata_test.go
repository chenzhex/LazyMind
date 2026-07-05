package service

import (
	"context"
	"encoding/json"
	"testing"

	"gorm.io/gorm"
)

func TestPatchSkillMetadata_DoesNotCreateRevision(t *testing.T) {
	db := newSkillV2TestDB(t)
	seedSkillWithHeadRevision(t, db, "skill1", "rev1")
	svc := NewSkillService(SkillServiceDeps{
		DB:        db,
		BlobStore: NewBlobStore(db, NewLocalObjectStore(t.TempDir())),
		Clock:     fixedClock(),
	})

	resp, err := svc.PatchSkill(context.Background(), PatchSkillRequest{
		SkillID:     "skill1",
		UserID:      "user_001",
		Name:        stringPtr("论文精读 Pro"),
		Category:    stringPtr("research-updated"),
		Description: stringPtr("更新后的描述"),
		Tags:        &[]string{"paper", "deep"},
		AutoEvo:     boolPtr(true),
		IsEnabled:   boolPtr(false),
	})
	if err != nil {
		t.Fatalf("PatchSkill returned error: %v", err)
	}
	if resp.HeadRevisionID != "rev1" {
		t.Fatalf("HeadRevisionID = %q, want rev1", resp.HeadRevisionID)
	}

	var skill testSkillV2SkillRow
	if err := db.Where("id = ?", "skill1").Take(&skill).Error; err != nil {
		t.Fatalf("query skill: %v", err)
	}
	if skill.SkillName != "论文精读 Pro" || skill.Category != "research-updated" || skill.Description != "更新后的描述" {
		t.Fatalf("metadata not updated: %#v", skill)
	}
	assertJSONTags(t, skill.Tags, []string{"paper", "deep"})
	if !skill.AutoEvo {
		t.Fatal("auto_evo = false, want true")
	}
	if skill.IsEnabled {
		t.Fatal("is_enabled = true, want false")
	}
	if skill.HeadRevisionID == nil || *skill.HeadRevisionID != "rev1" {
		t.Fatalf("head_revision_id changed to %v, want rev1", skill.HeadRevisionID)
	}
	if got := countRows(t, db, "skill_revisions", "skill_id = ?", "skill1"); got != 1 {
		t.Fatalf("skill_revisions count = %d, want 1", got)
	}
	if got := countRows(t, db, "skill_revision_entries", "revision_id = ?", "rev1"); got != 1 {
		t.Fatalf("skill_revision_entries count = %d, want 1", got)
	}
}

func seedSkillWithHeadRevision(t *testing.T, db *gorm.DB, skillID, revisionID string) {
	t.Helper()
	now := fixedClock().Now()
	tags, _ := json.Marshal([]string{"paper"})
	head := revisionID
	if err := db.Create(&testSkillV2SkillRow{
		ID:                 skillID,
		OwnerUserID:        "user_001",
		OwnerUserName:      "张三",
		CreateUserID:       "user_001",
		CreateUserName:     "张三",
		Category:           "research",
		SkillName:          "论文精读",
		Description:        "原始描述",
		Tags:               tags,
		RelativeRoot:       "research/论文精读",
		SkillMDPath:        "SKILL.md",
		HeadRevisionID:     &head,
		Version:            1,
		AutoEvoApplyStatus: "idle",
		IsEnabled:          true,
		UpdateStatus:       "up_to_date",
		CreatedAt:          now,
		UpdatedAt:          now,
	}).Error; err != nil {
		t.Fatalf("seed skill: %v", err)
	}
	if err := db.Create(&testSkillV2RevisionRow{
		ID:           revisionID,
		SkillID:      skillID,
		RevisionNo:   1,
		TreeHash:     "tree_hash_v1",
		ChangeSource: "create",
		CreatedAt:    now,
	}).Error; err != nil {
		t.Fatalf("seed revision: %v", err)
	}
	blobHash := "h_skill_v1"
	if err := db.Create(&testSkillV2BlobRow{
		Hash:           blobHash,
		Size:           12,
		Mime:           "text/markdown",
		FileType:       "markdown",
		Binary:         false,
		StorageBackend: "postgres",
		Content:        []byte("# 论文精读\n"),
		CreatedAt:      now,
	}).Error; err != nil {
		t.Fatalf("seed blob: %v", err)
	}
	if err := db.Create(&testSkillV2RevisionEntryRow{
		RevisionID: revisionID,
		Path:       "SKILL.md",
		EntryType:  "file",
		BlobHash:   &blobHash,
		Mime:       "text/markdown",
		FileType:   "markdown",
		Mode:       420,
	}).Error; err != nil {
		t.Fatalf("seed revision entry: %v", err)
	}
	if err := db.Create(&testSkillV2DraftRow{
		SkillID:        skillID,
		BaseRevisionID: &head,
		TaskID:         "",
		Version:        1,
		CreatedAt:      now,
		UpdatedAt:      now,
	}).Error; err != nil {
		t.Fatalf("seed draft: %v", err)
	}
}

func stringPtr(v string) *string {
	return &v
}
