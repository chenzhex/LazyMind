package modelprovider

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/gorilla/mux"
	"gorm.io/driver/sqlite"
	"gorm.io/gorm"

	"lazymind/core/common/orm"
	"lazymind/core/store"
)

func TestListGroupModelsIncludesMaxInputTokens(t *testing.T) {
	dbName := "group_models_" + strings.ReplaceAll(t.Name(), "/", "_")
	db, err := gorm.Open(sqlite.Open("file:"+dbName+"?mode=memory&cache=shared"), &gorm.Config{})
	if err != nil {
		t.Fatalf("open sqlite: %v", err)
	}
	if err := db.AutoMigrate(
		&orm.UserModelProvider{},
		&orm.UserModelProviderGroup{},
		&orm.UserModelProviderGroupModel{},
	); err != nil {
		t.Fatalf("migrate: %v", err)
	}
	store.Init(db, db, nil)

	now := time.Now().UTC()
	if err := db.Create(&orm.UserModelProvider{
		ID: "provider-1", Name: "OpenAI", Capabilities: "has_models",
		BaseModel: orm.BaseModel{CreateUserID: "user-1", CreatedAt: now, UpdatedAt: now},
	}).Error; err != nil {
		t.Fatalf("create provider: %v", err)
	}
	if err := db.Create(&orm.UserModelProviderGroup{
		ID: "group-1", UserModelProviderID: "provider-1", Name: "default", BaseURL: "https://api.openai.com/v1/",
		IsVerified: true,
		BaseModel:  orm.BaseModel{CreateUserID: "user-1", CreatedAt: now, UpdatedAt: now},
	}).Error; err != nil {
		t.Fatalf("create group: %v", err)
	}
	limit := int64(8192)
	models := []orm.UserModelProviderGroupModel{
		{
			ID: "model-default", UserModelProviderID: "provider-1", UserModelProviderGroupID: "group-1",
			ProviderName: "OpenAI", Name: "text-embedding-3-small", ModelType: "embed",
			MaxInputTokens: &limit, IsDefault: true,
			BaseModel: orm.BaseModel{CreateUserID: "user-1", CreatedAt: now, UpdatedAt: now},
		},
		{
			ID: "model-custom", UserModelProviderID: "provider-1", UserModelProviderGroupID: "group-1",
			ProviderName: "OpenAI", Name: "custom-embedding", ModelType: "embed",
			BaseModel: orm.BaseModel{CreateUserID: "user-1", CreatedAt: now, UpdatedAt: now},
		},
	}
	if err := db.Create(&models).Error; err != nil {
		t.Fatalf("create models: %v", err)
	}

	req := httptest.NewRequest(http.MethodGet, "/api/core/model_providers/provider-1/groups/group-1/models", nil)
	req.Header.Set("X-User-Id", "user-1")
	req = mux.SetURLVars(req, map[string]string{"model_provider_id": "provider-1", "group_id": "group-1"})
	rec := httptest.NewRecorder()
	ListGroupModels(rec, req)
	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d, body = %s", rec.Code, rec.Body.String())
	}

	var response struct {
		Data struct {
			Models []struct {
				Name           string `json:"name"`
				MaxInputTokens *int64 `json:"max_input_tokens"`
			} `json:"models"`
		} `json:"data"`
	}
	if err := json.Unmarshal(rec.Body.Bytes(), &response); err != nil {
		t.Fatalf("decode response: %v", err)
	}
	if len(response.Data.Models) != 2 {
		t.Fatalf("models length = %d, want 2", len(response.Data.Models))
	}
	byName := make(map[string]*int64, len(response.Data.Models))
	for _, model := range response.Data.Models {
		byName[model.Name] = model.MaxInputTokens
	}
	if got := byName["text-embedding-3-small"]; got == nil || *got != limit {
		t.Fatalf("default max_input_tokens = %v, want %d", got, limit)
	}
	if got := byName["custom-embedding"]; got != nil {
		t.Fatalf("custom max_input_tokens = %d, want null", *got)
	}

	const role = "test_embed_limit_role"
	roleTypeCache.Delete(role)
	algo := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/api/model/role_type" || r.URL.Query().Get("role") != role {
			t.Errorf("unexpected role type request: %s", r.URL.String())
		}
		_ = json.NewEncoder(w).Encode(algoRoleTypeResponse{Role: role, Type: "embed"})
	}))
	defer algo.Close()
	t.Setenv("LAZYMIND_CHAT_SERVICE_URL", algo.URL)

	globalReq := httptest.NewRequest(http.MethodGet, "/api/core/model_providers/models?model_type="+role, nil)
	globalReq.Header.Set("X-User-Id", "user-1")
	globalRec := httptest.NewRecorder()
	ListUserModelsByModelType(globalRec, globalReq)
	if globalRec.Code != http.StatusOK {
		t.Fatalf("global list status = %d, body = %s", globalRec.Code, globalRec.Body.String())
	}
	response = struct {
		Data struct {
			Models []struct {
				Name           string `json:"name"`
				MaxInputTokens *int64 `json:"max_input_tokens"`
			} `json:"models"`
		} `json:"data"`
	}{}
	if err := json.Unmarshal(globalRec.Body.Bytes(), &response); err != nil {
		t.Fatalf("decode global response: %v", err)
	}
	byName = make(map[string]*int64, len(response.Data.Models))
	for _, model := range response.Data.Models {
		byName[model.Name] = model.MaxInputTokens
	}
	if got := byName["text-embedding-3-small"]; got == nil || *got != limit {
		t.Fatalf("global default max_input_tokens = %v, want %d", got, limit)
	}
}
