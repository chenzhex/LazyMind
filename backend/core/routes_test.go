package main

import (
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/gorilla/mux"
)

func TestAgentThreadEventsRouteWinsOverGenericThreadRoute(t *testing.T) {
	r := mux.NewRouter()
	r.UseEncodedPath()
	registerAllRoutes(r)

	req := httptest.NewRequest(http.MethodGet, "/agent/threads/thr-306c5b7b:events", nil)
	var match mux.RouteMatch
	if !r.Match(req, &match) {
		t.Fatalf("expected events route to match")
	}

	gotTemplate, err := match.Route.GetPathTemplate()
	if err != nil {
		t.Fatalf("get matched route template: %v", err)
	}
	if want := "/agent/threads/{thread_id}:events"; gotTemplate != want {
		t.Fatalf("expected template %q, got %q", want, gotTemplate)
	}
	if gotID := match.Vars["thread_id"]; gotID != "thr-306c5b7b" {
		t.Fatalf("expected thread_id %q, got %q", "thr-306c5b7b", gotID)
	}
}

func TestSkillDraftPreviewRouteWinsOverGenericSkillRoute(t *testing.T) {
	r := mux.NewRouter()
	registerAllRoutes(r)

	req := httptest.NewRequest(http.MethodGet, "/skills/skill-306c5b7b:draft-preview", nil)
	var match mux.RouteMatch
	if !r.Match(req, &match) {
		t.Fatalf("expected draft-preview route to match")
	}

	gotTemplate, err := match.Route.GetPathTemplate()
	if err != nil {
		t.Fatalf("get matched route template: %v", err)
	}
	if want := "/skills/{skill_id}:draft-preview"; gotTemplate != want {
		t.Fatalf("expected template %q, got %q", want, gotTemplate)
	}
	if gotID := match.Vars["skill_id"]; gotID != "skill-306c5b7b" {
		t.Fatalf("expected skill_id %q, got %q", "skill-306c5b7b", gotID)
	}
}

func TestEvalSetStaticRoutesWinOverGenericEvalSetRoute(t *testing.T) {
	r := mux.NewRouter()
	r.UseEncodedPath()
	registerAllRoutes(r)

	cases := []struct {
		path string
		want string
	}{
		{"/eval-sets/datasets", "/eval-sets/datasets"},
		{"/eval-sets/question-types", "/eval-sets/question-types"},
	}

	for _, tc := range cases {
		req := httptest.NewRequest(http.MethodGet, tc.path, nil)
		var match mux.RouteMatch
		if !r.Match(req, &match) {
			t.Fatalf("expected route to match: %s", tc.path)
		}
		gotTemplate, err := match.Route.GetPathTemplate()
		if err != nil {
			t.Fatalf("get matched route template: %v", err)
		}
		if gotTemplate != tc.want {
			t.Fatalf("expected template %q, got %q", tc.want, gotTemplate)
		}
		if gotID := match.Vars["eval_set_id"]; gotID != "" {
			t.Fatalf("static route %s was captured as eval_set_id=%q", tc.path, gotID)
		}
	}
}

func TestNoLegacyQADatasetRoutesRegistered(t *testing.T) {
	r := mux.NewRouter()
	registerAllRoutes(r)

	for _, path := range []string{"/qa-datasets", "/qa-dataset-import-tasks/job_1"} {
		req := httptest.NewRequest(http.MethodGet, path, nil)
		var match mux.RouteMatch
		if r.Match(req, &match) {
			t.Fatalf("unexpected legacy qa dataset route matched: %s", path)
		}
	}
}
