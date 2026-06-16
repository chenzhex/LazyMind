package feishu

import (
	"context"
	"strings"

	"github.com/lazymind/scan_control_plane/internal/sourceengine/connector"
)

func (c *FeishuConnector) search(ctx context.Context, req connector.SearchRequest) (connector.RawObjectPage, error) {
	if err := ctx.Err(); err != nil {
		return connector.RawObjectPage{}, err
	}
	keyword := strings.TrimSpace(req.Keyword)
	if keyword == "" {
		return connector.RawObjectPage{}, connector.NewError(connector.ErrorCodeInvalidArgument, "keyword is required")
	}
	if req.TargetType != "" && !isSupportedTargetType(req.TargetType) {
		return connector.RawObjectPage{}, connector.NewError(connector.ErrorCodeInvalidTarget, "target_type is not supported")
	}
	if c.auth == nil || c.api == nil {
		return connector.RawObjectPage{}, connector.NewError(connector.ErrorCodeInvalidArgument, "feishu clients are not configured")
	}
	if err := validatePageSize(req.PageSize, c.Spec().MaxPageSize); err != nil {
		return connector.RawObjectPage{}, err
	}
	folderToken, err := searchDriveFolderToken(req)
	if err != nil {
		return connector.RawObjectPage{}, err
	}
	token, err := c.loadToken(ctx, req.AuthConnectionID, req.ProviderOptions.String("user_id"))
	if err != nil {
		return connector.RawObjectPage{}, err
	}
	page, err := c.api.SearchDriveFiles(ctx, token.AccessToken, keyword, folderToken, req.Cursor, req.PageSize)
	if err != nil {
		return connector.RawObjectPage{}, err
	}
	return c.buildRawObjectPage(req.AuthConnectionID, page, !page.HasMore), nil
}

func searchDriveFolderToken(req connector.SearchRequest) (string, error) {
	if req.TargetType == TargetTypeWikiNode || isWikiSearchRef(req.NodeRef) || isWikiSearchRef(req.TargetRef) {
		return "", connector.NewError(connector.ErrorCodeUnsupported, "feishu search API is unsupported for this target scope")
	}
	ref := strings.TrimSpace(req.NodeRef)
	if ref == "" {
		ref = strings.TrimSpace(req.TargetRef)
	}
	if ref == "" || ref == VirtualDriveRootRef {
		return "", nil
	}
	if req.TargetType != "" && req.TargetType != TargetTypeDriveFolder {
		return "", connector.NewError(connector.ErrorCodeUnsupported, "feishu search API is unsupported for this target scope")
	}
	return driveFolderToken(ref), nil
}

func isWikiSearchRef(ref string) bool {
	ref = strings.TrimSpace(ref)
	return strings.HasPrefix(ref, "wiki:") || strings.HasPrefix(ref, "feishu:wiki:")
}
