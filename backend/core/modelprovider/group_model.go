package modelprovider

import (
	"encoding/json"
	"errors"
	"net/http"
	"strings"
	"time"

	"github.com/gorilla/mux"
	"gorm.io/gorm"

	"lazymind/core/common"
	"lazymind/core/common/orm"
	"lazymind/core/store"
)

type addGroupModelRequest struct {
	Name      string `json:"name"`
	ModelType string `json:"model_type"`
}

type addGroupModelResponse struct {
	ID                       string `json:"id"`
	UserModelProviderID      string `json:"user_model_provider_id"`
	UserModelProviderGroupID string `json:"user_model_provider_group_id"`
	Name                     string `json:"name"`
	ModelType                string `json:"model_type"`
	ProviderName             string `json:"provider_name"`
	GroupName                string `json:"group_name"`
	BaseURL                  string `json:"base_url"`
	IsDefault                bool   `json:"is_default"`
}

type groupModelListItem struct {
	ID                       string `json:"id"`
	UserModelProviderID      string `json:"user_model_provider_id"`
	UserModelProviderGroupID string `json:"user_model_provider_group_id"`
	Name                     string `json:"name"`
	ModelType                string `json:"model_type"`
	ProviderName             string `json:"provider_name"`
	GroupName                string `json:"group_name"`
	BaseURL                  string `json:"base_url"`
	IsDefault                bool   `json:"is_default"`
	MaxInputTokens           *int64 `json:"max_input_tokens"`
}

type groupModelListResponse struct {
	Models []groupModelListItem `json:"models"`
}

// AddGroupModel inserts a user-defined model row under a connection group (custom model name and model_type).
func AddGroupModel(w http.ResponseWriter, r *http.Request) {
	db := store.DB()
	if db == nil {
		common.ReplyErr(w, "store not initialized", http.StatusInternalServerError)
		return
	}
	userID := strings.TrimSpace(store.UserID(r))
	userName := strings.TrimSpace(store.UserName(r))
	if userID == "" {
		common.ReplyErr(w, "missing X-User-Id", http.StatusBadRequest)
		return
	}

	parentID := strings.TrimSpace(mux.Vars(r)["model_provider_id"])
	groupID := strings.TrimSpace(mux.Vars(r)["group_id"])
	if parentID == "" || groupID == "" {
		common.ReplyErr(w, "missing model_provider_id or group_id", http.StatusBadRequest)
		return
	}

	var req addGroupModelRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		common.ReplyErr(w, "invalid body", http.StatusBadRequest)
		return
	}
	name := strings.TrimSpace(req.Name)
	modelType := strings.TrimSpace(req.ModelType)
	if name == "" || modelType == "" {
		common.ReplyErr(w, "name and model_type are required", http.StatusBadRequest)
		return
	}

	var parent orm.UserModelProvider
	err := db.WithContext(r.Context()).
		Where("id = ? AND create_user_id = ? AND deleted_at IS NULL", parentID, userID).
		Take(&parent).Error
	if err != nil {
		if errors.Is(err, gorm.ErrRecordNotFound) {
			common.ReplyErr(w, "model provider not found", http.StatusNotFound)
			return
		}
		common.ReplyErr(w, "query model provider failed", http.StatusInternalServerError)
		return
	}

	if !parent.HasCapability("has_models") {
		common.ReplyErr(w, "this provider does not support models", http.StatusBadRequest)
		return
	}

	var group orm.UserModelProviderGroup
	err = db.WithContext(r.Context()).
		Where("id = ? AND user_model_provider_id = ? AND create_user_id = ? AND deleted_at IS NULL", groupID, parent.ID, userID).
		Take(&group).Error
	if err != nil {
		if errors.Is(err, gorm.ErrRecordNotFound) {
			common.ReplyErr(w, "group not found", http.StatusNotFound)
			return
		}
		common.ReplyErr(w, "query group failed", http.StatusInternalServerError)
		return
	}

	var dupUser int64
	if err := db.WithContext(r.Context()).Model(&orm.UserModelProviderGroupModel{}).
		Where(
			"user_model_provider_group_id = ? AND create_user_id = ? AND deleted_at IS NULL AND name = ?",
			group.ID, userID, name,
		).Count(&dupUser).Error; err != nil {
		common.ReplyErr(w, "check existing model failed", http.StatusInternalServerError)
		return
	}
	if dupUser > 0 {
		common.ReplyErr(w, "model name already exists in this group", http.StatusConflict)
		return
	}

	now := time.Now()
	row := orm.UserModelProviderGroupModel{
		ID:                       common.GenerateID(),
		UserModelProviderID:      parent.ID,
		UserModelProviderGroupID: group.ID,
		ProviderName:             parent.Name,
		Name:                     name,
		ModelType:                modelType,
		IsDefault:                false,
		BaseModel: orm.BaseModel{
			CreateUserID:   userID,
			CreateUserName: userName,
			CreatedAt:      now,
			UpdatedAt:      now,
			DeletedAt:      nil,
		},
	}
	if err := db.WithContext(r.Context()).Create(&row).Error; err != nil {
		common.ReplyErr(w, "create model failed", http.StatusInternalServerError)
		return
	}

	common.ReplyOK(w, addGroupModelResponse{
		ID:                       row.ID,
		UserModelProviderID:      row.UserModelProviderID,
		UserModelProviderGroupID: row.UserModelProviderGroupID,
		Name:                     row.Name,
		ModelType:                row.ModelType,
		ProviderName:             row.ProviderName,
		GroupName:                group.Name,
		BaseURL:                  group.BaseURL,
		IsDefault:                row.IsDefault,
	})
}

// ListGroupModels returns active models under a connection group.
func ListGroupModels(w http.ResponseWriter, r *http.Request) {
	db := store.DB()
	if db == nil {
		common.ReplyErr(w, "store not initialized", http.StatusInternalServerError)
		return
	}
	userID := strings.TrimSpace(store.UserID(r))
	if userID == "" {
		common.ReplyErr(w, "missing X-User-Id", http.StatusBadRequest)
		return
	}

	parentID := strings.TrimSpace(mux.Vars(r)["model_provider_id"])
	groupID := strings.TrimSpace(mux.Vars(r)["group_id"])
	if parentID == "" || groupID == "" {
		common.ReplyErr(w, "missing model_provider_id or group_id", http.StatusBadRequest)
		return
	}

	var parent orm.UserModelProvider
	err := db.WithContext(r.Context()).
		Where("id = ? AND create_user_id = ? AND deleted_at IS NULL", parentID, userID).
		Take(&parent).Error
	if err != nil {
		if errors.Is(err, gorm.ErrRecordNotFound) {
			common.ReplyErr(w, "model provider not found", http.StatusNotFound)
			return
		}
		common.ReplyErr(w, "query model provider failed", http.StatusInternalServerError)
		return
	}

	// Providers without has_models return an empty list rather than an error.
	if !parent.HasCapability("has_models") {
		common.ReplyOK(w, groupModelListResponse{Models: []groupModelListItem{}})
		return
	}

	var group orm.UserModelProviderGroup
	err = db.WithContext(r.Context()).
		Where("id = ? AND user_model_provider_id = ? AND create_user_id = ? AND deleted_at IS NULL", groupID, parent.ID, userID).
		Take(&group).Error
	if err != nil {
		if errors.Is(err, gorm.ErrRecordNotFound) {
			common.ReplyErr(w, "group not found", http.StatusNotFound)
			return
		}
		common.ReplyErr(w, "query group failed", http.StatusInternalServerError)
		return
	}

	var rows []orm.UserModelProviderGroupModel
	if err := db.WithContext(r.Context()).
		Where(
			"user_model_provider_group_id = ? AND create_user_id = ? AND deleted_at IS NULL",
			group.ID, userID,
		).
		Order("name ASC").
		Find(&rows).Error; err != nil {
		common.ReplyErr(w, "list models failed", http.StatusInternalServerError)
		return
	}

	out := make([]groupModelListItem, 0, len(rows))
	for i := range rows {
		m := rows[i]
		out = append(out, groupModelListItem{
			ID:                       m.ID,
			UserModelProviderID:      m.UserModelProviderID,
			UserModelProviderGroupID: m.UserModelProviderGroupID,
			Name:                     m.Name,
			ModelType:                m.ModelType,
			ProviderName:             m.ProviderName,
			GroupName:                group.Name,
			BaseURL:                  group.BaseURL,
			IsDefault:                m.IsDefault,
			MaxInputTokens:           m.MaxInputTokens,
		})
	}
	common.ReplyOK(w, groupModelListResponse{Models: out})
}

// ListUserModelsByModelType lists the current user's models across all user_model_providers,
// filtered by required query model_type. Response shape matches ListGroupModels.
func ListUserModelsByModelType(w http.ResponseWriter, r *http.Request) {
	// 获取全局数据库连接对象
	db := store.DB()

	// 判断数据库是否初始化成功
	if db == nil {
		// 如果数据库对象为空，说明 store 还没有初始化，返回 500 错误
		common.ReplyErr(w, "store not initialized", http.StatusInternalServerError)
		return
	}

	// 从 HTTP 请求中获取用户 ID，并去掉前后空格
	// store.UserID(r) 很可能是从请求头 X-User-Id 中读取用户 ID
	userID := strings.TrimSpace(store.UserID(r))

	// 判断用户 ID 是否为空
	if userID == "" {
		// 如果没有传 X-User-Id，返回 400 参数错误
		common.ReplyErr(w, "missing X-User-Id", http.StatusBadRequest)
		return
	}

	// 从 URL 查询参数中获取 model_type，并去掉前后空格
	//
	// 例如请求：
	// /api/models?model_type=evo_llm
	//
	// 那么这里拿到的就是 "evo_llm"
	modelType := strings.TrimSpace(r.URL.Query().Get("model_type"))

	// 判断 model_type 是否为空
	if modelType == "" {
		// 如果 model_type 没有传，返回 400 参数错误
		common.ReplyErr(w, "model_type is required", http.StatusBadRequest)
		return
	}

	// 从 URL 查询参数中获取 keyword，并去掉前后空格
	//
	// keyword 是搜索关键字，可传可不传
	// 例如：
	// /api/models?model_type=evo_llm&keyword=gpt
	keyword := strings.TrimSpace(r.URL.Query().Get("keyword"))

	// Translate runtime_models.yaml role key (e.g. "evo_llm") to the lazyllm
	// technical type (e.g. "llm") stored in user_model_provider_group_models.
	//
	// 将前端或 YAML 中使用的模型角色类型转换成数据库中真正存储的模型类型
	//
	// 例如：
	// modelType = "evo_llm"
	// dbModelType = "llm"
	//
	// 这样后面查询数据库时，才能和 user_model_provider_group_models.model_type 对上
	dbModelType := resolveModelType(r.Context(), modelType)

	// 构造数据库查询对象 q
	//
	// db.WithContext(r.Context()) 表示数据库查询绑定当前请求上下文
	// 如果请求取消、超时，数据库操作也可以感知
	q := db.WithContext(r.Context()).

		// 关联 user_model_providers 表
		//
		// JOIN 条件：
		// 1. user_model_providers.id = user_model_provider_group_models.user_model_provider_id
		//    表示模型所属的 provider 要对应上
		//
		// 2. user_model_providers.deleted_at IS NULL
		//    表示 provider 没有被软删除
		//
		// 3. user_model_providers.capabilities LIKE '%has_models%'
		//    表示这个 provider 必须具备 has_models 能力
		Joins("JOIN user_model_providers ON user_model_providers.id = user_model_provider_group_models.user_model_provider_id AND user_model_providers.deleted_at IS NULL AND user_model_providers.capabilities LIKE '%has_models%'").

		// 关联 user_model_provider_groups 表
		//
		// JOIN 条件：
		// 1. group 表的 id 要等于 model 表中的 group_id
		// 2. group 表的 create_user_id 要等于 model 表中的 create_user_id
		// 3. group 没有被软删除
		// 4. group 必须是已验证状态 is_verified = true
		Joins("JOIN user_model_provider_groups ON user_model_provider_groups.id = user_model_provider_group_models.user_model_provider_group_id AND user_model_provider_groups.create_user_id = user_model_provider_group_models.create_user_id AND user_model_provider_groups.deleted_at IS NULL AND user_model_provider_groups.is_verified = ?", true).

		// 查询条件：
		// 1. 当前模型属于当前用户
		// 2. 当前模型没有被软删除
		// 3. 当前模型类型等于转换后的 dbModelType
		Where("user_model_provider_group_models.create_user_id = ? AND user_model_provider_group_models.deleted_at IS NULL AND user_model_provider_group_models.model_type = ?", userID, dbModelType)

	// 如果传入了搜索关键字 keyword，则追加模糊搜索条件
	if keyword != "" {
		// 拼接 SQL LIKE 查询需要的格式
		//
		// keyword = "gpt"
		// like = "%gpt%"
		//
		// 表示只要字段中包含 gpt 就能匹配
		like := "%" + keyword + "%"

		// 追加 WHERE 条件
		//
		// 支持从三个字段里搜索：
		// 1. 模型名称
		// 2. provider 名称
		// 3. group 名称
		q = q.Where(
			"user_model_provider_group_models.name LIKE ? OR user_model_provider_group_models.provider_name LIKE ? OR user_model_provider_groups.name LIKE ?",
			like,
			like,
			like,
		)
	}

	// 定义 rows，用来接收数据库查询出来的模型列表
	var rows []orm.UserModelProviderGroupModel

	// 执行数据库查询
	if err := q.Order("user_model_provider_group_models.user_model_provider_id ASC, user_model_provider_group_models.user_model_provider_group_id ASC, user_model_provider_group_models.name ASC").

		// 将查询结果保存到 rows 中
		Find(&rows).Error; err != nil {

		// 如果查询失败，返回 500 错误
		common.ReplyErr(w, "list models failed", http.StatusInternalServerError)
		return
	}

	// 创建一个 groupIDs 切片，用来保存所有涉及到的 group ID
	groupIDs := make([]string, 0)

	// 创建一个 map，用来去重 group ID
	//
	// map[string]struct{} 是 Go 中常见的 set 写法
	// 表示只关心 key 是否存在，不关心 value
	seenGroup := make(map[string]struct{})

	// 遍历查询出来的模型 rows
	for i := range rows {
		// 获取当前模型所属的 group ID
		gid := rows[i].UserModelProviderGroupID

		// 判断这个 group ID 是否已经出现过
		if _, ok := seenGroup[gid]; !ok {
			// 如果没有出现过，就记录下来
			seenGroup[gid] = struct{}{}

			// 加入 groupIDs 列表
			groupIDs = append(groupIDs, gid)
		}
	}

	// 定义一个临时结构体，用来保存 group 的部分信息
	type groupInfo struct {
		// group 名称
		name string

		// group 的 base URL
		baseURL string

		// group 是否已经验证
		isVerified bool
	}

	// 创建一个 map，用 groupID 快速查找 group 信息
	groupByID := make(map[string]groupInfo)

	// 如果前面查询出来的模型中存在 group ID，则继续查询 group 信息
	if len(groupIDs) > 0 {
		// 定义 grps，用来接收 group 查询结果
		var grps []orm.UserModelProviderGroup

		// 查询 user_model_provider_groups 表
		if err := db.WithContext(r.Context()).

			// 查询条件：
			// 1. id 在 groupIDs 列表中
			// 2. create_user_id 等于当前用户
			// 3. deleted_at IS NULL，表示没有被软删除
			Where("id IN ? AND create_user_id = ? AND deleted_at IS NULL", groupIDs, userID).

			// 执行查询，并把结果放到 grps
			Find(&grps).Error; err != nil {

			// 如果查询 group 失败，返回 500 错误
			common.ReplyErr(w, "list groups failed", http.StatusInternalServerError)
			return
		}

		// 遍历查询出来的 group
		for i := range grps {
			// 以 group ID 为 key，保存 group 信息
			groupByID[grps[i].ID] = groupInfo{
				name:       grps[i].Name,
				baseURL:    grps[i].BaseURL,
				isVerified: grps[i].IsVerified,
			}
		}
	}

	// 创建最终返回给前端的模型列表
	//
	// len(rows) 作为初始容量，减少 append 时的扩容次数
	out := make([]groupModelListItem, 0, len(rows))

	// 遍历数据库查询出来的模型 rows
	for i := range rows {
		// 取出当前模型
		m := rows[i]

		// 根据当前模型的 group ID 查找 group 信息
		grp, ok := groupByID[m.UserModelProviderGroupID]

		// 如果找不到 group，或者 group 不是已验证状态，则跳过
		if !ok || !grp.isVerified {
			continue
		}

		// 将数据库模型对象转换成接口返回对象
		out = append(out, groupModelListItem{
			// 模型 ID
			ID: m.ID,

			// 用户模型供应商 ID
			UserModelProviderID: m.UserModelProviderID,

			// 用户模型供应商分组 ID
			UserModelProviderGroupID: m.UserModelProviderGroupID,

			// 模型名称
			Name: m.Name,

			// 模型类型
			ModelType: m.ModelType,

			// 供应商名称
			ProviderName: m.ProviderName,

			// 分组名称
			GroupName: grp.name,

			// 分组 BaseURL
			BaseURL: grp.baseURL,

			// 是否默认模型
			IsDefault: m.IsDefault,

			// 模型最大输入 token 数；自定义或未配置的模型为 null
			MaxInputTokens: m.MaxInputTokens,
		})
	}

	// 返回成功响应
	// 响应内容是 groupModelListResponse，里面包含 Models: out
	common.ReplyOK(w, groupModelListResponse{Models: out})
}

type deleteGroupModelResponse struct {
	ID string `json:"id"`
}

// DeleteGroupModel soft-deletes one user_model_provider_group_models row under the given group.
func DeleteGroupModel(w http.ResponseWriter, r *http.Request) {
	db := store.DB()
	if db == nil {
		common.ReplyErr(w, "store not initialized", http.StatusInternalServerError)
		return
	}
	userID := strings.TrimSpace(store.UserID(r))
	if userID == "" {
		common.ReplyErr(w, "missing X-User-Id", http.StatusBadRequest)
		return
	}

	parentID := strings.TrimSpace(mux.Vars(r)["model_provider_id"])
	groupID := strings.TrimSpace(mux.Vars(r)["group_id"])
	modelID := strings.TrimSpace(mux.Vars(r)["model_id"])
	if parentID == "" || groupID == "" || modelID == "" {
		common.ReplyErr(w, "missing model_provider_id, group_id, or model_id", http.StatusBadRequest)
		return
	}

	var parent orm.UserModelProvider
	err := db.WithContext(r.Context()).
		Where("id = ? AND create_user_id = ? AND deleted_at IS NULL", parentID, userID).
		Take(&parent).Error
	if err != nil {
		if errors.Is(err, gorm.ErrRecordNotFound) {
			common.ReplyErr(w, "model provider not found", http.StatusNotFound)
			return
		}
		common.ReplyErr(w, "query model provider failed", http.StatusInternalServerError)
		return
	}

	if !parent.HasCapability("has_models") {
		common.ReplyErr(w, "this provider does not support models", http.StatusBadRequest)
		return
	}

	var group orm.UserModelProviderGroup
	err = db.WithContext(r.Context()).
		Where("id = ? AND user_model_provider_id = ? AND create_user_id = ? AND deleted_at IS NULL", groupID, parent.ID, userID).
		Take(&group).Error
	if err != nil {
		if errors.Is(err, gorm.ErrRecordNotFound) {
			common.ReplyErr(w, "group not found", http.StatusNotFound)
			return
		}
		common.ReplyErr(w, "query group failed", http.StatusInternalServerError)
		return
	}

	var row orm.UserModelProviderGroupModel
	err = db.WithContext(r.Context()).
		Where(
			"id = ? AND user_model_provider_group_id = ? AND user_model_provider_id = ? AND create_user_id = ? AND deleted_at IS NULL",
			modelID, group.ID, parent.ID, userID,
		).
		Take(&row).Error
	if err != nil {
		if errors.Is(err, gorm.ErrRecordNotFound) {
			common.ReplyErr(w, "model not found", http.StatusNotFound)
			return
		}
		common.ReplyErr(w, "query model failed", http.StatusInternalServerError)
		return
	}

	clearMultimodalSelection := isMultimodalEmbeddingModelType(row.ModelType)
	now := time.Now().UTC()
	if err := db.WithContext(r.Context()).Transaction(func(tx *gorm.DB) error {
		if err := tx.Model(&orm.UserModelProviderGroupModel{}).
			Where("id = ? AND create_user_id = ? AND deleted_at IS NULL", row.ID, userID).
			Updates(map[string]interface{}{
				"deleted_at": now,
				"updated_at": now,
			}).Error; err != nil {
			return err
		}
		// Drop any default-model rows pointing at this model (avoids stale share=true).
		if err := tx.Where("user_model_provider_group_model_id = ?", row.ID).
			Delete(&orm.UserSelectedModel{}).Error; err != nil {
			return err
		}
		return nil
	}); err != nil {
		common.ReplyErr(w, "delete model failed", http.StatusInternalServerError)
		return
	}

	if clearMultimodalSelection {
		maybeScheduleImageGroupLazyReset(r.Context(), db)
	}

	common.ReplyOK(w, deleteGroupModelResponse{ID: modelID})
}
