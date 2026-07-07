package agent

import (
	"context"
	"errors"
	"fmt"
	"net/http"
	"net/url"
	"strconv"
	"strings"

	"lazymind/core/common"
)

var resultKindGateStep = map[string]string{
	"datasets":         "dataset",
	"eval-reports":     "eval",
	"analysis-reports": "analysis",
	"diffs":            "repair",
	"abtests":          "abtest",
}

var artifactGateStep = map[string]string{
	"eval.dataset":          "dataset",
	"eval.summary":          "eval",
	"analysis.summary":      "analysis",
	"repair.verified_patch": "repair",
	"abtest.comparison":     "abtest",
}

func fetchThreadResultProxy(ctx context.Context, r *http.Request, threadID, resultKind string, version int) (*upstreamProxyResponse, int, error) {
	step, ok := resultKindGateStep[strings.TrimSpace(resultKind)]
	if !ok {
		return nil, http.StatusBadRequest, fmt.Errorf("unsupported result kind: %s", resultKind)
	}
	client := newEvoClient(forwardedEvoProxyHeaders(r, "application/json"))
	_, selected, ok, err := selectGateVersionByStep(ctx, client, threadID, step, version)
	if err != nil {
		return nil, evoProxyStatusCode(err), err
	}
	if !ok {
		return nil, http.StatusNotFound, fmt.Errorf("thread result gate not found")
	}
	proxy, err := client.doProxyRaw(ctx, http.MethodGet, gateVersionPath(threadID, step, selected, false), nil, nil)
	if err != nil {
		return nil, evoProxyStatusCode(err), err
	}
	return proxy, proxyStatusCode(proxy), nil
}

func fetchThreadResultContent(
	ctx context.Context,
	r *http.Request,
	threadID, resultKind string,
	version int,
) (evoGate, *evoGateContent, bool, error) {
	step := resultKindGateStep[strings.TrimSpace(resultKind)]
	if step == "" {
		return evoGate{}, nil, false, fmt.Errorf("unsupported result kind: %s", resultKind)
	}
	client := newEvoClient(forwardedUpstreamHeaders(r))
	return fetchGateContentByStep(ctx, client, threadID, step, version)
}

func fetchThreadArtifactProxy(ctx context.Context, r *http.Request, threadID, artifactID string) (*upstreamProxyResponse, int, error) {
	ref := parseArtifactRef(artifactID)
	if ref.Base == "" {
		return nil, http.StatusBadRequest, fmt.Errorf("artifact_id required")
	}
	client := newEvoClient(forwardedEvoProxyHeaders(r, "application/json"))
	_, selected, ok, err := selectGateVersionByArtifact(ctx, client, threadID, ref.Base, ref.Version)
	if err != nil {
		return nil, evoProxyStatusCode(err), err
	}
	if !ok {
		return nil, http.StatusNotFound, fmt.Errorf("artifact not found")
	}
	proxy, err := client.doProxyRaw(ctx, http.MethodGet, gateVersionPath(threadID, artifactGateStep[ref.Base], selected, false), nil, nil)
	if err != nil {
		return nil, evoProxyStatusCode(err), err
	}
	return proxy, proxyStatusCode(proxy), nil
}

func selectGateVersionByArtifact(
	ctx context.Context,
	client evoClient,
	threadID, artifactID string,
	version int,
) (evoGate, int, bool, error) {
	step := artifactGateStep[strings.TrimSpace(artifactID)]
	if step == "" {
		return evoGate{}, 0, false, nil
	}
	return selectGateVersionByStep(ctx, client, threadID, step, version)
}

func fetchGateContentByStep(
	ctx context.Context,
	client evoClient,
	threadID, step string,
	version int,
) (evoGate, *evoGateContent, bool, error) {
	gate, selected, ok, err := selectGateVersionByStep(ctx, client, threadID, step, version)
	if err != nil || !ok {
		return gate, nil, ok, err
	}
	content, err := client.GetGateContent(ctx, threadID, step, selected)
	return gate, content, err == nil, err
}

func selectGateVersionByStep(
	ctx context.Context,
	client evoClient,
	threadID, step string,
	version int,
) (evoGate, int, bool, error) {
	gates, err := client.ListGates(ctx, threadID)
	if err != nil {
		return evoGate{}, 0, false, err
	}
	for _, gate := range gates.Gates {
		if gate.Step != step {
			continue
		}
		selected := version
		if selected <= 0 {
			var ok bool
			selected, ok = selectedGateVersion(gate)
			if !ok {
				return gate, 0, false, nil
			}
		}
		return gate, selected, true, nil
	}
	return evoGate{}, 0, false, nil
}

func selectedGateVersion(gate evoGate) (int, bool) {
	if gate.EffectiveVersion != nil && *gate.EffectiveVersion > 0 {
		return *gate.EffectiveVersion, true
	}
	if gate.LatestVersion != nil && *gate.LatestVersion > 0 {
		return *gate.LatestVersion, true
	}
	maxVersion := 0
	for _, version := range gate.Versions {
		if version > maxVersion {
			maxVersion = version
		}
	}
	return maxVersion, maxVersion > 0
}

type parsedArtifactRef struct {
	Base    string
	Version int
}

func parseArtifactRef(raw string) parsedArtifactRef {
	raw = strings.TrimSpace(raw)
	if decoded, err := url.PathUnescape(raw); err == nil {
		raw = decoded
	}
	result := parsedArtifactRef{Base: raw}
	if index := strings.LastIndex(result.Base, "@v"); index > 0 {
		if version, err := strconv.Atoi(strings.TrimSpace(result.Base[index+2:])); err == nil {
			result.Version = version
			result.Base = strings.TrimSpace(result.Base[:index])
		}
	}
	return result
}

func gateVersionPath(threadID, step string, version int, download bool) string {
	suffix := fmt.Sprintf(
		"/gates/%s/versions/%d",
		url.PathEscape(strings.TrimSpace(step)),
		version,
	)
	if download {
		suffix += ":download"
	}
	return "/threads/" + url.PathEscape(strings.TrimSpace(threadID)) + suffix
}

func evoProxyStatusCode(err error) int {
	var httpErr *common.HTTPError
	if errors.As(err, &httpErr) {
		if httpErr.StatusCode >= 400 && httpErr.StatusCode < 500 {
			return httpErr.StatusCode
		}
	}
	return http.StatusBadGateway
}
