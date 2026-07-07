package modelprovider

import (
	"context"
	"errors"
	"os"
	"strings"
	"time"

	"gopkg.in/yaml.v3"
	"gorm.io/gorm"

	"lazymind/core/common"
	"lazymind/core/common/orm"
	"lazymind/core/log"
)

type catalogModel struct {
	Name           string `yaml:"name"`
	Type           string `yaml:"type"`
	MaxInputTokens *int64 `yaml:"max_input_tokens"`
}

type catalogSupplier struct {
	Name         string         `yaml:"name"`
	Description  string         `yaml:"description"`
	BaseURL      string         `yaml:"base_url"`
	Capabilities []string       `yaml:"capabilities"` // overrides section-level default when non-empty
	Models       []catalogModel `yaml:"models"`
}

type catalogSection struct {
	Capabilities []string          `yaml:"capabilities"`
	Suppliers    []catalogSupplier `yaml:"suppliers"`
}

// modelCatalog is a map from section key (e.g. "model_providers") to its section.
type modelCatalog map[string]catalogSection

var endpointPathMarkers = []string{"/embeddings", "/rerank", "/embed"}

// normalizeBaseURL appends a trailing slash to generic API roots; endpoint-specific URLs are kept as-is.
func normalizeBaseURL(raw string) string {
	url := strings.TrimSpace(raw)
	if url == "" {
		return url
	}
	for _, marker := range endpointPathMarkers {
		if strings.Contains(url, marker) {
			return url
		}
	}
	if !strings.HasSuffix(url, "/") {
		return url + "/"
	}
	return url
}

func loadModelCatalog(yamlBytes []byte) (modelCatalog, error) {
	var catalog modelCatalog
	if err := yaml.Unmarshal(yamlBytes, &catalog); err != nil {
		return nil, err
	}
	return catalog, nil
}

func upsertDefaultProvider(tx *gorm.DB, now time.Time, category string, caps []string, item catalogSupplier) (string, error) {
	// 从传入的 catalogSupplier 中取出供应商名称，并去掉前后空格
	name := strings.TrimSpace(item.Name)

	// 判断供应商名称是否为空
	if name == "" {
		// 如果供应商名称为空，返回空字符串和错误信息
		return "", errors.New("provider name is required")
	}

	// Supplier-level capabilities override section-level when present.
	// 如果 supplier 自己配置了 capabilities，则优先使用 supplier 自己的 capabilities
	// 否则使用 section 层级传进来的 caps
	effectiveCaps := caps

	// 判断当前 supplier 是否单独配置了 capabilities
	if len(item.Capabilities) > 0 {
		// 如果 supplier 自己配置了 capabilities，则覆盖 section 级别的 caps
		effectiveCaps = item.Capabilities
	}

	// 将 capabilities 切片转换成逗号分隔的字符串
	//
	// 例如：
	// []string{"chat", "embedding"}
	// 会变成：
	// "chat,embedding"
	capStr := strings.Join(effectiveCaps, ",")

	// 对供应商的 BaseURL 进行规范化处理
	// 例如去掉末尾多余的斜杠、去掉空格等
	// 具体规则取决于 normalizeBaseURL 函数的实现
	baseURL := normalizeBaseURL(item.BaseURL)

	// 声明一个 DefaultModelProvider 类型的变量 row
	// 用于接收数据库中查询出来的供应商记录
	var row orm.DefaultModelProvider

	// 根据供应商名称 name 查询 default_model_providers 表中是否已经存在该供应商
	//
	// 查询条件：
	// name = 当前供应商名称
	//
	// Take(&row) 表示查询一条记录，并把查询结果保存到 row 中
	// .Error 表示获取查询过程中产生的错误
	err := tx.Where("name = ?", name).Take(&row).Error

	// 判断查询错误是否是“记录不存在”
	if errors.Is(err, gorm.ErrRecordNotFound) {
		// 如果数据库中不存在该供应商，则创建一条新的供应商记录
		row = orm.DefaultModelProvider{
			// 生成新的供应商 ID
			ID: common.GenerateID(),

			// 设置供应商名称
			Name: name,

			// 设置供应商描述
			Description: item.Description,

			// 设置规范化后的 BaseURL
			BaseURL: baseURL,

			// 设置供应商分类
			Category: category,

			// 设置供应商能力列表字符串
			Capabilities: capStr,

			// 设置创建时间
			CreatedAt: now,

			// 设置更新时间
			UpdatedAt: now,
		}

		// 将新创建的供应商记录插入数据库
		//
		// 返回值说明：
		// row.ID：新供应商的 ID
		// tx.Create(&row).Error：数据库插入操作的错误信息
		return row.ID, tx.Create(&row).Error
	}

	// 如果 err 不为空，并且不是 gorm.ErrRecordNotFound
	// 说明查询数据库时发生了其他错误
	if err != nil {
		// 返回空字符串和具体错误
		return "", err
	}

	// 走到这里，说明数据库中已经存在该供应商记录
	// 因此执行更新操作
	return row.ID, tx.Model(&orm.DefaultModelProvider{}).

		// 指定只更新 ID 等于 row.ID 的那一条供应商记录
		Where("id = ?", row.ID).

		// 批量更新指定字段
		Updates(map[string]any{
			// 更新供应商描述
			"description": item.Description,

			// 更新供应商 BaseURL
			"base_url": baseURL,

			// 更新供应商分类
			"category": category,

			// 更新供应商能力
			"capabilities": capStr,

			// 更新时间改为当前时间
			"updated_at": now,

			// 将 deleted_at 设置为 nil
			// 如果这条供应商记录之前被软删除，这里相当于恢复该记录
			"deleted_at": nil,
		}).Error
}

func upsertDefaultModel(tx *gorm.DB, now time.Time, providerID, providerName string, item catalogModel) error {
	// 从传入的 catalogModel 中取出模型名称，并去掉前后空格
	name := strings.TrimSpace(item.Name)

	// 从传入的 catalogModel 中取出模型类型，并去掉前后空格
	modelType := strings.TrimSpace(item.Type)

	// 判断模型名称或模型类型是否为空
	if name == "" || modelType == "" {
		// 如果 name 或 modelType 为空，则返回错误
		return errors.New("model name and type are required")
	}
	if item.MaxInputTokens != nil && *item.MaxInputTokens <= 0 {
		return errors.New("model max_input_tokens must be greater than zero")
	}

	// 声明一个 DefaultModel 类型的变量 row
	// 用于接收数据库中查询出来的默认模型记录
	var row orm.DefaultModel

	// 根据 providerID 和 name 查询 default_models 表中是否已经存在该模型
	//
	// 查询条件：
	// default_model_provider_id = providerID
	// name = name
	//
	// Take(&row) 表示查询一条记录，并把结果保存到 row 中
	// .Error 表示获取查询过程中产生的错误
	err := tx.Where("default_model_provider_id = ? AND name = ?", providerID, name).Take(&row).Error

	// 判断查询错误是否是“记录不存在”
	if errors.Is(err, gorm.ErrRecordNotFound) {
		// 如果记录不存在，则创建一个新的 DefaultModel 对象
		row = orm.DefaultModel{
			// 生成新的模型 ID
			ID: common.GenerateID(),

			// 设置该模型所属的默认模型供应商 ID
			DefaultModelProviderID: providerID,

			// 设置供应商名称
			ProviderName: providerName,

			// 设置模型名称
			Name: name,

			// 设置模型类型
			ModelType: modelType,

			// 设置模型最大输入 token 数；未配置时保持为空
			MaxInputTokens: item.MaxInputTokens,

			// 设置创建时间
			CreatedAt: now,

			// 设置更新时间
			UpdatedAt: now,
		}

		// 将新创建的 row 插入到数据库中
		// 如果插入成功，返回 nil
		// 如果插入失败，返回具体错误
		if err := tx.Create(&row).Error; err != nil {
			return err
		}
		return syncDefaultModelMaxInputTokens(tx, now, providerID, name, item.MaxInputTokens)
	}

	// 如果 err 不为空，并且不是 gorm.ErrRecordNotFound
	// 说明查询数据库时发生了其他错误
	if err != nil {
		// 直接返回该错误
		return err
	}

	// 走到这里，说明数据库中已经存在该模型记录
	// 因此执行更新操作
	if err := tx.Model(&orm.DefaultModel{}).

		// 指定只更新 ID 等于 row.ID 的那一条记录
		Where("id = ?", row.ID).

		// 批量更新指定字段
		Updates(map[string]any{
			// 更新供应商名称
			"provider_name": providerName,

			// 更新模型类型
			"model_type": modelType,

			// 更新模型最大输入 token 数；nil 会清空已经失效的目录值
			"max_input_tokens": item.MaxInputTokens,

			// 更新修改时间
			"updated_at": now,

			// 将 deleted_at 设置为 nil
			// 如果这条记录之前被软删除，这里相当于恢复该记录
			"deleted_at": nil,
		}).Error; err != nil {
		return err
	}
	return syncDefaultModelMaxInputTokens(tx, now, providerID, name, item.MaxInputTokens)
}

// syncDefaultModelMaxInputTokens backfills known catalog metadata into default models already
// copied to user groups. Unknown limits and custom models are intentionally left untouched.
func syncDefaultModelMaxInputTokens(tx *gorm.DB, now time.Time, providerID, modelName string, maxInputTokens *int64) error {
	if maxInputTokens == nil {
		return nil
	}
	providerIDs := tx.Model(&orm.UserModelProvider{}).
		Select("id").
		Where("default_model_provider_id = ? AND deleted_at IS NULL", providerID)
	return tx.Model(&orm.UserModelProviderGroupModel{}).
		Where("is_default = ? AND name = ? AND user_model_provider_id IN (?) AND deleted_at IS NULL", true, modelName, providerIDs).
		Where("max_input_tokens IS NULL OR max_input_tokens <> ?", *maxInputTokens).
		Updates(map[string]any{
			"max_input_tokens": maxInputTokens,
			"updated_at":       now,
		}).Error
}

// SeedModelCatalog upserts default_model_providers and default_models from the YAML catalog file.
// Section keys ending with "_providers" derive their category by trimming that suffix.
func SeedModelCatalog(ctx context.Context, db *gorm.DB, yamlPath string) error {
	return seedCatalog(ctx, db, yamlPath, "_providers", "")
}

// SeedDatasourceCatalog upserts default_model_providers from the datasource YAML catalog file.
// All suppliers are seeded with category "datasource" regardless of section key.
func SeedDatasourceCatalog(ctx context.Context, db *gorm.DB, yamlPath string) error {
	return seedCatalog(ctx, db, yamlPath, "_sources", "datasource")
}

func seedCatalog(ctx context.Context, db *gorm.DB, yamlPath, categorySuffix, forceCategory string) error {
	// 去掉 yamlPath 前后的空格，避免路径中因为多余空格导致读取失败
	yamlPath = strings.TrimSpace(yamlPath)

	// 判断 YAML 文件路径是否为空
	if yamlPath == "" {
		// 如果路径为空，直接返回错误
		return errors.New("catalog yaml path is required")
	}

	// 根据 YAML 文件路径读取文件内容
	// yamlBytes 是读取出来的文件字节内容
	yamlBytes, err := os.ReadFile(yamlPath)

	// 判断读取文件是否失败
	if err != nil {
		// 如果文件不存在、路径错误、权限不足等，都会返回错误
		return err
	}

	// 将读取到的 YAML 字节内容解析成 Go 中的 catalog 数据结构
	catalog, err := loadModelCatalog(yamlBytes)

	// 判断 YAML 解析是否失败
	if err != nil {
		// 如果 YAML 格式错误，或者字段结构不符合要求，则返回错误
		return err
	}

	// 获取当前 UTC 时间
	// 用于后续数据库记录的 CreatedAt 和 UpdatedAt 字段
	now := time.Now().UTC()

	// 使用 db.WithContext(ctx) 将 ctx 绑定到数据库操作中
	// 这样数据库操作可以响应超时、取消等上下文信号
	//
	// Transaction 表示开启一个数据库事务
	// 事务中的所有操作要么全部成功，要么全部回滚
	return db.WithContext(ctx).Transaction(func(tx *gorm.DB) error {

		// 遍历 catalog 中的每一个 section
		// sectionKey 是 YAML 中的分组 key
		// section 是该分组下的具体内容
		for sectionKey, section := range catalog {

			// 默认使用 forceCategory 作为当前分类
			category := forceCategory

			// 如果 forceCategory 为空，说明没有强制指定分类
			if category == "" {
				// 从 sectionKey 中去掉 categorySuffix 后缀，得到分类名
				//
				// 例如：
				// sectionKey = "llm_providers"
				// categorySuffix = "_providers"
				// category = "llm"
				category = strings.TrimSuffix(sectionKey, categorySuffix)
			}

			// 遍历当前 section 下的所有供应商
			for _, supplier := range section.Suppliers {

				// 插入或更新 default_model_providers 表中的供应商记录
				//
				// 参数说明：
				// tx：当前事务对象
				// now：当前时间
				// category：供应商分类
				// section.Capabilities：当前 section 的能力配置
				// supplier：当前供应商信息
				//
				// 返回值 providerID 是数据库中该供应商的 ID
				providerID, err := upsertDefaultProvider(tx, now, category, section.Capabilities, supplier)

				// 判断供应商插入或更新是否失败
				if err != nil {
					// 如果失败，返回错误
					// 事务会自动回滚
					return err
				}

				// 遍历当前供应商下的所有模型
				for _, model := range supplier.Models {

					// 插入或更新 default_models 表中的模型记录
					//
					// providerID：当前模型所属的供应商 ID
					// supplier.Name：当前模型所属的供应商名称
					// model：当前模型信息
					if err := upsertDefaultModel(tx, now, providerID, supplier.Name, model); err != nil {
						// 如果模型插入或更新失败，返回错误
						// 事务会自动回滚
						return err
					}
				}
			}
		}

		// 所有 section、supplier、model 都处理成功
		// 返回 nil 表示事务可以提交
		return nil
	})
}

// MustSeedModelCatalog runs SeedModelCatalog using config/model_catalog.yaml under the working directory.
func MustSeedModelCatalog(ctx context.Context, db *gorm.DB, yamlPath string) {
	// 调用 SeedModelCatalog 函数，根据指定的 YAML 文件路径初始化/导入模型目录数据
	// 如果 SeedModelCatalog 返回错误，则进入 if 语句内部
	if err := SeedModelCatalog(ctx, db, yamlPath); err != nil {

		// 使用 zerolog 记录致命错误日志
		// Fatal() 表示这是一个严重错误，通常会导致程序退出
		// Err(err) 记录具体的错误信息
		// Str("path", yamlPath) 额外记录 YAML 文件路径，方便排查是哪个文件出错
		// Msg("seed model catalog failed") 输出日志消息：模型目录初始化失败
		log.Logger.Fatal().Err(err).Str("path", yamlPath).Msg("seed model catalog failed")
	}

	// 如果 SeedModelCatalog 没有返回错误，说明模型目录初始化成功
	// 这里记录一条 Info 级别日志
	// Str("path", yamlPath) 记录成功加载的 YAML 文件路径
	// Msg("model catalog seeded from YAML") 输出日志消息：模型目录已从 YAML 文件初始化
	log.Logger.Info().Str("path", yamlPath).Msg("model catalog seeded from YAML")
}

// MustSeedDatasourceCatalog runs SeedDatasourceCatalog using config/datasource_catalog.yaml under the working directory.
func MustSeedDatasourceCatalog(ctx context.Context, db *gorm.DB, yamlPath string) {
	if err := SeedDatasourceCatalog(ctx, db, yamlPath); err != nil {
		log.Logger.Fatal().Err(err).Str("path", yamlPath).Msg("seed datasource catalog failed")
	}
	log.Logger.Info().Str("path", yamlPath).Msg("datasource catalog seeded from YAML")
}
