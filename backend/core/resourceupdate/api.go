package resourceupdate

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"strings"
	"time"

	"gorm.io/gorm"

	"lazymind/core/common"
	"lazymind/core/common/orm"
	"lazymind/core/resourcechange"
	"lazymind/core/store"
)

type taskResponse struct {
	ID             string     `json:"id"`
	TaskType       string     `json:"task_type"`
	ResourceType   string     `json:"resource_type"`
	UserID         string     `json:"user_id"`
	ResourceID     string     `json:"resource_id"`
	TriggerType    string     `json:"trigger_type"`
	TriggerID      string     `json:"trigger_id"`
	Status         string     `json:"status"`
	ReviewResultID string     `json:"review_result_id,omitempty"`
	ResultID       string     `json:"result_id,omitempty"`
	ErrorCode      string     `json:"error_code,omitempty"`
	ErrorMessage   string     `json:"error_message,omitempty"`
	AttemptCount   int        `json:"attempt_count"`
	NextRunAt      time.Time  `json:"next_run_at"`
	CreatedAt      time.Time  `json:"created_at"`
	UpdatedAt      time.Time  `json:"updated_at"`
	StartedAt      *time.Time `json:"started_at,omitempty"`
	FinishedAt     *time.Time `json:"finished_at,omitempty"`
}

type skillReviewResultResponse struct {
	ID             string    `json:"id"`
	SkillName      string    `json:"skill_name"`
	Type           string    `json:"type"`
	ReviewStatus   string    `json:"review_status"`
	UserID         string    `json:"userid"`
	RequestID      string    `json:"requestid"`
	SkillContent   string    `json:"skill_content,omitempty"`
	CurrentContent string    `json:"current_content,omitempty"`
	Summary        string    `json:"summary"`
	Time           time.Time `json:"time"`
}

type memoryReviewResultResponse struct {
	ID            string          `json:"id"`
	UserID        string          `json:"user_id"`
	Target        string          `json:"target"`
	SessionID     string          `json:"session_id"`
	SourceContent string          `json:"source_content"`
	Content       string          `json:"content"`
	Operations    json.RawMessage `json:"operations,omitempty"`
	State         string          `json:"state"`
	ReviewStatus  string          `json:"review_status"`
	Time          time.Time       `json:"time"`
}

func ListTasks(w http.ResponseWriter, r *http.Request) {
	db, userID, ok := requestDBAndUser(w, r)
	if !ok {
		return
	}
	page := parsePositiveQueryInt(r.URL.Query().Get("page"), 1, 0)
	pageSize := parsePositiveQueryInt(r.URL.Query().Get("page_size"), 20, 100)
	query := db.WithContext(r.Context()).Model(&orm.ResourceUpdateTask{}).Where("user_id = ?", userID)
	if status := strings.TrimSpace(r.URL.Query().Get("status")); status != "" {
		query = query.Where("status = ?", status)
	}
	if resourceType := strings.TrimSpace(r.URL.Query().Get("resource_type")); resourceType != "" {
		query = query.Where("resource_type = ?", resourceType)
	}
	if taskType := strings.TrimSpace(r.URL.Query().Get("task_type")); taskType != "" {
		query = query.Where("task_type = ?", taskType)
	}
	var total int64
	if err := query.Count(&total).Error; err != nil {
		common.ReplyErr(w, "query tasks failed", http.StatusInternalServerError)
		return
	}
	var rows []orm.ResourceUpdateTask
	if err := query.Order("created_at DESC").
		Offset((page - 1) * pageSize).
		Limit(pageSize).
		Find(&rows).Error; err != nil {
		common.ReplyErr(w, "query tasks failed", http.StatusInternalServerError)
		return
	}
	items := make([]taskResponse, 0, len(rows))
	for _, row := range rows {
		items = append(items, taskToResponse(row))
	}
	common.ReplyOK(w, map[string]any{"items": items, "page": page, "page_size": pageSize, "total": total})
}

func GetTask(w http.ResponseWriter, r *http.Request) {
	db, userID, ok := requestDBAndUser(w, r)
	if !ok {
		return
	}
	taskID := common.PathVar(r, "task_id")
	if strings.TrimSpace(taskID) == "" {
		common.ReplyErr(w, "missing task_id", http.StatusBadRequest)
		return
	}
	var row orm.ResourceUpdateTask
	if err := db.WithContext(r.Context()).Where("id = ? AND user_id = ?", taskID, userID).Take(&row).Error; err != nil {
		if errors.Is(err, gorm.ErrRecordNotFound) {
			common.ReplyErr(w, "task not found", http.StatusNotFound)
			return
		}
		common.ReplyErr(w, "query task failed", http.StatusInternalServerError)
		return
	}
	common.ReplyOK(w, taskToResponse(row))
}

func ListSkillReviewResults(w http.ResponseWriter, r *http.Request) {
	db, userID, ok := requestDBAndUser(w, r)
	if !ok {
		return
	}
	page := parsePositiveQueryInt(r.URL.Query().Get("page"), 1, 0)
	pageSize := parsePositiveQueryInt(r.URL.Query().Get("page_size"), 20, 100)
	query := skillResultSelect(db.WithContext(r.Context())).Where("userid = ?", userID)
	if status := strings.TrimSpace(r.URL.Query().Get("review_status")); status != "" {
		query = query.Where("review_status = ?", status)
	}
	if typ := strings.TrimSpace(r.URL.Query().Get("type")); typ != "" {
		query = query.Where("type = ?", typ)
	}
	var total int64
	if err := query.Count(&total).Error; err != nil {
		common.ReplyErr(w, "query skill review results failed", http.StatusInternalServerError)
		return
	}
	var rows []SkillReviewResult
	if err := query.Order("time DESC, id DESC").
		Offset((page - 1) * pageSize).
		Limit(pageSize).
		Find(&rows).Error; err != nil {
		common.ReplyErr(w, "query skill review results failed", http.StatusInternalServerError)
		return
	}
	items := make([]skillReviewResultResponse, 0, len(rows))
	for _, row := range rows {
		items = append(items, skillResultToResponse(row))
	}
	common.ReplyOK(w, map[string]any{"items": items, "page": page, "page_size": pageSize, "total": total})
}

func GetSkillReviewResult(w http.ResponseWriter, r *http.Request) {
	db, userID, ok := requestDBAndUser(w, r)
	if !ok {
		return
	}
	resultID := common.PathVar(r, "review_result_id")
	var row SkillReviewResult
	err := skillResultSelect(db.WithContext(r.Context())).Where("id = ? AND userid = ?", resultID, userID).Take(&row).Error
	if err != nil {
		if errors.Is(err, gorm.ErrRecordNotFound) {
			common.ReplyErr(w, "skill review result not found", http.StatusNotFound)
			return
		}
		common.ReplyErr(w, "query skill review result failed", http.StatusInternalServerError)
		return
	}
	resp, err := skillResultDetailResponse(r.Context(), db, row)
	if err != nil {
		mapReviewError(w, err, "query skill review result")
		return
	}
	common.ReplyOK(w, resp)
}

func AcceptSkillReviewResult(w http.ResponseWriter, r *http.Request) {
	db, userID, ok := requestDBAndUser(w, r)
	if !ok {
		return
	}
	userName := strings.TrimSpace(store.UserName(r))
	resultID := common.PathVar(r, "review_result_id")
	row, err := acceptSkillReviewResult(r.Context(), db, userID, userName, resultID)
	if err != nil {
		mapReviewError(w, err, "accept skill review result")
		return
	}
	resourceUpdateInfo(logEventReviewAccepted).
		Str("resource_type", orm.ResourceUpdateResourceTypeSkill).
		Str("review_result_id", row.ID).
		Str("user_id", row.UserID).
		Str("review_type", row.Type).
		Msg(logEventReviewAccepted)
	common.ReplyOK(w, skillResultToResponse(row))
}

func RejectSkillReviewResult(w http.ResponseWriter, r *http.Request) {
	db, userID, ok := requestDBAndUser(w, r)
	if !ok {
		return
	}
	resultID := common.PathVar(r, "review_result_id")
	row, err := rejectSkillReviewResult(r.Context(), db, userID, resultID)
	if err != nil {
		mapReviewError(w, err, "reject skill review result")
		return
	}
	resourceUpdateInfo(logEventReviewRejected).
		Str("resource_type", orm.ResourceUpdateResourceTypeSkill).
		Str("review_result_id", row.ID).
		Str("user_id", row.UserID).
		Str("review_type", row.Type).
		Msg(logEventReviewRejected)
	common.ReplyOK(w, skillResultToResponse(row))
}

func ListMemoryReviewResults(w http.ResponseWriter, r *http.Request) {
	db, userID, ok := requestDBAndUser(w, r)
	if !ok {
		return
	}
	page := parsePositiveQueryInt(r.URL.Query().Get("page"), 1, 0)
	pageSize := parsePositiveQueryInt(r.URL.Query().Get("page_size"), 20, 100)
	query := memoryResultSelect(db.WithContext(r.Context())).Where("user_id = ?", userID)
	if status := strings.TrimSpace(r.URL.Query().Get("review_status")); status != "" {
		query = query.Where("review_status = ?", status)
	}
	if target := strings.TrimSpace(r.URL.Query().Get("target")); target != "" {
		query = query.Where("target = ?", target)
	}
	var total int64
	if err := query.Count(&total).Error; err != nil {
		common.ReplyErr(w, "query memory review results failed", http.StatusInternalServerError)
		return
	}
	var rows []MemoryReviewResult
	if err := query.Order("time DESC, id DESC").
		Offset((page - 1) * pageSize).
		Limit(pageSize).
		Find(&rows).Error; err != nil {
		common.ReplyErr(w, "query memory review results failed", http.StatusInternalServerError)
		return
	}
	items := make([]memoryReviewResultResponse, 0, len(rows))
	for _, row := range rows {
		items = append(items, memoryResultToResponse(row))
	}
	common.ReplyOK(w, map[string]any{"items": items, "page": page, "page_size": pageSize, "total": total})
}

func GetMemoryReviewResult(w http.ResponseWriter, r *http.Request) {
	db, userID, ok := requestDBAndUser(w, r)
	if !ok {
		return
	}
	resultID := common.PathVar(r, "review_result_id")
	var row MemoryReviewResult
	err := memoryResultSelect(db.WithContext(r.Context())).Where("id = ? AND user_id = ?", resultID, userID).Take(&row).Error
	if err != nil {
		if errors.Is(err, gorm.ErrRecordNotFound) {
			common.ReplyErr(w, "memory review result not found", http.StatusNotFound)
			return
		}
		common.ReplyErr(w, "query memory review result failed", http.StatusInternalServerError)
		return
	}
	common.ReplyOK(w, memoryResultToResponse(row))
}

func AcceptMemoryReviewResult(w http.ResponseWriter, r *http.Request) {
	db, userID, ok := requestDBAndUser(w, r)
	if !ok {
		return
	}
	resultID := common.PathVar(r, "review_result_id")
	row, err := acceptMemoryReviewResult(r.Context(), db, userID, resultID)
	if err != nil {
		mapReviewError(w, err, "accept memory review result")
		return
	}
	resourceUpdateInfo(logEventReviewAccepted).
		Str("resource_type", normalizeReviewTarget(row.Target)).
		Str("review_result_id", row.ID).
		Str("user_id", row.UserID).
		Str("target", row.Target).
		Msg(logEventReviewAccepted)
	common.ReplyOK(w, memoryResultToResponse(row))
}

func RejectMemoryReviewResult(w http.ResponseWriter, r *http.Request) {
	db, userID, ok := requestDBAndUser(w, r)
	if !ok {
		return
	}
	resultID := common.PathVar(r, "review_result_id")
	row, err := rejectMemoryReviewResult(r.Context(), db, userID, resultID)
	if err != nil {
		mapReviewError(w, err, "reject memory review result")
		return
	}
	resourceUpdateInfo(logEventReviewRejected).
		Str("resource_type", normalizeReviewTarget(row.Target)).
		Str("review_result_id", row.ID).
		Str("user_id", row.UserID).
		Str("target", row.Target).
		Msg(logEventReviewRejected)
	common.ReplyOK(w, memoryResultToResponse(row))
}

func acceptSkillReviewResult(ctx context.Context, db *gorm.DB, userID, userName, resultID string) (SkillReviewResult, error) {
	now := time.Now().UTC()
	var out SkillReviewResult
	err := db.WithContext(ctx).Transaction(func(tx *gorm.DB) error {
		row, err := lockSkillReviewResultForUser(ctx, tx, resultID, userID)
		if err != nil {
			return err
		}
		if strings.TrimSpace(row.ReviewStatus) != reviewStatusPending {
			return errReviewConflict
		}
		switch strings.TrimSpace(row.Type) {
		case skillReviewTypePatch:
			resource, err := mapSkillPatchResultToResource(withUpdateLock(tx).WithContext(ctx), row)
			if err != nil {
				if errors.Is(err, gorm.ErrRecordNotFound) {
					return errReviewNotFound
				}
				return err
			}
			if err := applySkillPatchResult(ctx, tx, row, resource, now, resourcechange.Source{
				ChangeSource:  resourcechange.ChangeSourceReviewAccept,
				SourceRefType: resourcechange.SourceRefTypeSkillReviewResult,
				SourceRefID:   row.ID,
				ChangedAt:     now,
			}); err != nil {
				return err
			}
		case skillReviewTypeNew:
			if _, err := createSkillFromNewResult(ctx, tx, row, userName, now, resourcechange.Source{
				ChangeSource:  resourcechange.ChangeSourceReviewAccept,
				SourceRefType: resourcechange.SourceRefTypeSkillReviewResult,
				SourceRefID:   row.ID,
				ChangedAt:     now,
			}); err != nil {
				return err
			}
			if err := updateSkillReviewStatus(ctx, tx, row.ID, reviewStatusAccepted); err != nil {
				return err
			}
		default:
			return errReviewInvalid
		}
		row.ReviewStatus = reviewStatusAccepted
		out = row
		return nil
	})
	return out, err
}

func rejectSkillReviewResult(ctx context.Context, db *gorm.DB, userID, resultID string) (SkillReviewResult, error) {
	var out SkillReviewResult
	err := db.WithContext(ctx).Transaction(func(tx *gorm.DB) error {
		row, err := lockSkillReviewResultForUser(ctx, tx, resultID, userID)
		if err != nil {
			return err
		}
		if strings.TrimSpace(row.ReviewStatus) != reviewStatusPending {
			return errReviewConflict
		}
		if err := updateSkillReviewStatus(ctx, tx, row.ID, reviewStatusRejected); err != nil {
			return err
		}
		row.ReviewStatus = reviewStatusRejected
		out = row
		return nil
	})
	return out, err
}

func acceptMemoryReviewResult(ctx context.Context, db *gorm.DB, userID, resultID string) (MemoryReviewResult, error) {
	now := time.Now().UTC()
	var out MemoryReviewResult
	err := db.WithContext(ctx).Transaction(func(tx *gorm.DB) error {
		row, err := lockMemoryReviewResultForUser(ctx, tx, resultID, userID)
		if err != nil {
			return err
		}
		if strings.TrimSpace(row.ReviewStatus) != reviewStatusPending {
			return errReviewConflict
		}
		if strings.TrimSpace(row.State) != memoryReviewStateSuccess {
			return errReviewInvalid
		}
		switch normalizeReviewTarget(row.Target) {
		case orm.ResourceUpdateResourceTypeMemory:
			resource, err := mapMemoryReviewResultToMemory(withUpdateLock(tx).WithContext(ctx), row)
			if err != nil {
				if errors.Is(err, gorm.ErrRecordNotFound) {
					return errReviewNotFound
				}
				return err
			}
			if err := applyMemoryReviewResult(ctx, tx, row, resource, now, false, resourcechange.Source{
				ChangeSource:  resourcechange.ChangeSourceReviewAccept,
				SourceRefType: resourcechange.SourceRefTypeMemoryReview,
				SourceRefID:   row.ID,
				ChangedAt:     now,
			}); err != nil {
				return err
			}
		case orm.ResourceUpdateResourceTypeUserPreference:
			resource, err := mapMemoryReviewResultToPreference(withUpdateLock(tx).WithContext(ctx), row)
			if err != nil {
				if errors.Is(err, gorm.ErrRecordNotFound) {
					return errReviewNotFound
				}
				return err
			}
			if err := applyPreferenceReviewResult(ctx, tx, row, resource, now, false, resourcechange.Source{
				ChangeSource:  resourcechange.ChangeSourceReviewAccept,
				SourceRefType: resourcechange.SourceRefTypeMemoryReview,
				SourceRefID:   row.ID,
				ChangedAt:     now,
			}); err != nil {
				return err
			}
		default:
			return errReviewInvalid
		}
		row.ReviewStatus = reviewStatusAccepted
		out = row
		return nil
	})
	return out, err
}

func rejectMemoryReviewResult(ctx context.Context, db *gorm.DB, userID, resultID string) (MemoryReviewResult, error) {
	var out MemoryReviewResult
	err := db.WithContext(ctx).Transaction(func(tx *gorm.DB) error {
		row, err := lockMemoryReviewResultForUser(ctx, tx, resultID, userID)
		if err != nil {
			return err
		}
		if strings.TrimSpace(row.ReviewStatus) != reviewStatusPending {
			return errReviewConflict
		}
		if err := updateMemoryReviewStatus(ctx, tx, row.ID, reviewStatusRejected); err != nil {
			return err
		}
		row.ReviewStatus = reviewStatusRejected
		out = row
		return nil
	})
	return out, err
}

func requestDBAndUser(w http.ResponseWriter, r *http.Request) (*gorm.DB, string, bool) {
	db := store.DB()
	if db == nil {
		common.ReplyErr(w, "store not initialized", http.StatusInternalServerError)
		return nil, "", false
	}
	userID := strings.TrimSpace(store.UserID(r))
	if userID == "" {
		common.ReplyErr(w, "missing X-User-Id", http.StatusBadRequest)
		return nil, "", false
	}
	return db, userID, true
}

func taskToResponse(row orm.ResourceUpdateTask) taskResponse {
	return taskResponse{
		ID:             row.ID,
		TaskType:       row.TaskType,
		ResourceType:   row.ResourceType,
		UserID:         row.UserID,
		ResourceID:     row.ResourceID,
		TriggerType:    row.TriggerType,
		TriggerID:      row.TriggerID,
		Status:         row.Status,
		ReviewResultID: row.ReviewResultID,
		ResultID:       row.ResultID,
		ErrorCode:      row.ErrorCode,
		ErrorMessage:   row.ErrorMessage,
		AttemptCount:   row.AttemptCount,
		NextRunAt:      row.NextRunAt,
		CreatedAt:      row.CreatedAt,
		UpdatedAt:      row.UpdatedAt,
		StartedAt:      row.StartedAt,
		FinishedAt:     row.FinishedAt,
	}
}

func skillResultToResponse(row SkillReviewResult) skillReviewResultResponse {
	return skillReviewResultResponse{
		ID:             row.ID,
		SkillName:      row.SkillName,
		Type:           row.Type,
		ReviewStatus:   row.ReviewStatus,
		UserID:         row.UserID,
		RequestID:      row.RequestID,
		SkillContent:   row.SkillContent,
		CurrentContent: "",
		Summary:        row.Summary,
		Time:           row.Time,
	}
}

func skillResultDetailResponse(ctx context.Context, db *gorm.DB, row SkillReviewResult) (skillReviewResultResponse, error) {
	resp := skillResultToResponse(row)
	if strings.TrimSpace(row.Type) != skillReviewTypePatch {
		return resp, nil
	}
	resource, err := mapSkillPatchResultToResource(db.WithContext(ctx), row)
	if err != nil {
		if errors.Is(err, gorm.ErrRecordNotFound) {
			return skillReviewResultResponse{}, errReviewNotFound
		}
		return skillReviewResultResponse{}, err
	}
	resp.CurrentContent = resource.Content
	return resp, nil
}

func memoryResultToResponse(row MemoryReviewResult) memoryReviewResultResponse {
	return memoryReviewResultResponse{
		ID:            row.ID,
		UserID:        row.UserID,
		Target:        row.Target,
		SessionID:     row.SessionID,
		SourceContent: row.SourceContent,
		Content:       row.Content,
		Operations:    row.Operations,
		State:         row.State,
		ReviewStatus:  row.ReviewStatus,
		Time:          row.Time,
	}
}

func lockSkillReviewResultForUser(ctx context.Context, tx *gorm.DB, id, userID string) (SkillReviewResult, error) {
	var row SkillReviewResult
	err := skillResultSelect(withUpdateLock(tx).WithContext(ctx)).
		Where("id = ? AND userid = ?", strings.TrimSpace(id), strings.TrimSpace(userID)).
		Take(&row).Error
	if errors.Is(err, gorm.ErrRecordNotFound) {
		return SkillReviewResult{}, errReviewNotFound
	}
	return row, err
}

func lockMemoryReviewResultForUser(ctx context.Context, tx *gorm.DB, id, userID string) (MemoryReviewResult, error) {
	var row MemoryReviewResult
	err := memoryResultSelect(withUpdateLock(tx).WithContext(ctx)).
		Where("id = ? AND user_id = ?", strings.TrimSpace(id), strings.TrimSpace(userID)).
		Take(&row).Error
	if errors.Is(err, gorm.ErrRecordNotFound) {
		return MemoryReviewResult{}, errReviewNotFound
	}
	return row, err
}

func updateSkillReviewStatus(ctx context.Context, tx *gorm.DB, id, status string) error {
	result := tx.WithContext(ctx).
		Table("skill_review_results").
		Where("id = ? AND review_status = ?", id, reviewStatusPending).
		Update("review_status", status)
	if result.Error != nil {
		return result.Error
	}
	if result.RowsAffected == 0 {
		return errReviewConflict
	}
	return nil
}

func updateMemoryReviewStatus(ctx context.Context, tx *gorm.DB, id, status string) error {
	result := tx.WithContext(ctx).
		Table("memory_review").
		Where("id = ? AND review_status = ?", id, reviewStatusPending).
		Update("review_status", status)
	if result.Error != nil {
		return result.Error
	}
	if result.RowsAffected == 0 {
		return errReviewConflict
	}
	return nil
}
