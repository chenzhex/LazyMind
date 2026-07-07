package modelprovider

import (
	"os"
	"path/filepath"
	"testing"
	"time"

	"gorm.io/driver/sqlite"
	"gorm.io/gorm"

	"lazymind/core/common/orm"
)

func TestModelCatalogDefinesMaxInputTokensForEveryEmbeddingModel(t *testing.T) {
	yamlBytes, err := os.ReadFile(filepath.Join("..", "config", "model_catalog.yaml"))
	if err != nil {
		t.Fatalf("read model catalog: %v", err)
	}
	catalog, err := loadModelCatalog(yamlBytes)
	if err != nil {
		t.Fatalf("parse model catalog: %v", err)
	}

	embedCount := 0
	for _, section := range catalog {
		for _, supplier := range section.Suppliers {
			for _, model := range supplier.Models {
				if model.Type != "embed" {
					continue
				}
				embedCount++
				if model.MaxInputTokens == nil || *model.MaxInputTokens <= 0 {
					t.Errorf("embedding model %s/%s has invalid max_input_tokens", supplier.Name, model.Name)
				}
			}
		}
	}
	if embedCount == 0 {
		t.Fatal("model catalog contains no embedding models")
	}
}

func TestUpsertDefaultModelPersistsAndBackfillsMaxInputTokens(t *testing.T) {
	dbName := "seed_model_" + time.Now().Format("150405.000000000")
	db, err := gorm.Open(sqlite.Open("file:"+dbName+"?mode=memory&cache=shared"), &gorm.Config{})
	if err != nil {
		t.Fatalf("open sqlite: %v", err)
	}
	if err := db.AutoMigrate(
		&orm.DefaultModelProvider{},
		&orm.DefaultModel{},
		&orm.UserModelProvider{},
		&orm.UserModelProviderGroupModel{},
	); err != nil {
		t.Fatalf("migrate: %v", err)
	}

	now := time.Now().UTC()
	if err := db.Create(&orm.DefaultModelProvider{
		ID: "default-qwen", Name: "Qwen", Description: "Qwen", CreatedAt: now, UpdatedAt: now,
	}).Error; err != nil {
		t.Fatalf("create default provider: %v", err)
	}
	if err := db.Create(&orm.UserModelProvider{
		ID:                     "user-provider-qwen",
		DefaultModelProviderID: "default-qwen",
		Name:                   "Qwen",
		Capabilities:           "has_models",
		BaseModel: orm.BaseModel{
			CreateUserID: "user-1", CreatedAt: now, UpdatedAt: now,
		},
	}).Error; err != nil {
		t.Fatalf("create user provider: %v", err)
	}
	if err := db.Create(&orm.UserModelProviderGroupModel{
		ID:                       "user-model-qwen",
		UserModelProviderID:      "user-provider-qwen",
		UserModelProviderGroupID: "group-qwen",
		ProviderName:             "Qwen",
		Name:                     "qwen3-embedding-32m",
		ModelType:                "embed",
		IsDefault:                true,
		BaseModel: orm.BaseModel{
			CreateUserID: "user-1", CreatedAt: now, UpdatedAt: now,
		},
	}).Error; err != nil {
		t.Fatalf("create user model: %v", err)
	}

	limit := int64(32768)
	if err := upsertDefaultModel(db, now.Add(time.Minute), "default-qwen", "Qwen", catalogModel{
		Name: "qwen3-embedding-32m", Type: "embed", MaxInputTokens: &limit,
	}); err != nil {
		t.Fatalf("upsert default model: %v", err)
	}

	var defaultModel orm.DefaultModel
	if err := db.Where("default_model_provider_id = ? AND name = ?", "default-qwen", "qwen3-embedding-32m").Take(&defaultModel).Error; err != nil {
		t.Fatalf("query default model: %v", err)
	}
	if defaultModel.MaxInputTokens == nil || *defaultModel.MaxInputTokens != limit {
		t.Fatalf("default max_input_tokens = %v, want %d", defaultModel.MaxInputTokens, limit)
	}

	var userModel orm.UserModelProviderGroupModel
	if err := db.Where("id = ?", "user-model-qwen").Take(&userModel).Error; err != nil {
		t.Fatalf("query user model: %v", err)
	}
	if userModel.MaxInputTokens == nil || *userModel.MaxInputTokens != limit {
		t.Fatalf("user max_input_tokens = %v, want %d", userModel.MaxInputTokens, limit)
	}
}

func TestUpsertDefaultModelRejectsNonPositiveMaxInputTokens(t *testing.T) {
	zero := int64(0)
	err := upsertDefaultModel(&gorm.DB{}, time.Now(), "provider", "Provider", catalogModel{
		Name: "embedding-model", Type: "embed", MaxInputTokens: &zero,
	})
	if err == nil || err.Error() != "model max_input_tokens must be greater than zero" {
		t.Fatalf("error = %v, want max_input_tokens validation error", err)
	}
}
