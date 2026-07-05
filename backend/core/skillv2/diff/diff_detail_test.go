package diff

import (
	"context"
	"strings"
	"testing"
)

func TestDiffFile_EscapesHTML(t *testing.T) {
	service := NewService(ServiceDeps{})
	oldFS := fakeReadOnlySkillFS{entries: []EntryInfo{fileEntry("SKILL.md", "h_old", false, "markdown")}, files: map[string][]byte{"SKILL.md": []byte("# safe\n")}}
	newFS := fakeReadOnlySkillFS{entries: []EntryInfo{fileEntry("SKILL.md", "h_new", false, "markdown")}, files: map[string][]byte{"SKILL.md": []byte("<script>alert(1)</script>\n")}}

	result, err := service.CompareFile(context.Background(), oldFS, newFS, DiffOptions{Path: "SKILL.md"})
	if err != nil {
		t.Fatalf("CompareFile returned error: %v", err)
	}
	foundRawText := false
	for _, line := range result.DiffEntryLines {
		if strings.Contains(line.Text, "<script>alert(1)</script>") {
			foundRawText = true
		}
		if strings.Contains(strings.ToLower(line.HTML), "<script>") {
			t.Fatalf("diff HTML contains executable script: %#v", line)
		}
	}
	if !foundRawText {
		t.Fatalf("raw text was not preserved in diff lines: %#v", result.DiffEntryLines)
	}
}

func TestDiffFile_NoNewLineWarning(t *testing.T) {
	service := NewService(ServiceDeps{})
	oldFS := fakeReadOnlySkillFS{entries: []EntryInfo{fileEntry("SKILL.md", "h_old", false, "markdown")}, files: map[string][]byte{"SKILL.md": []byte("line\n")}}
	newFS := fakeReadOnlySkillFS{entries: []EntryInfo{fileEntry("SKILL.md", "h_new", false, "markdown")}, files: map[string][]byte{"SKILL.md": []byte("line")}}

	result, err := service.CompareFile(context.Background(), oldFS, newFS, DiffOptions{Path: "SKILL.md"})
	if err != nil {
		t.Fatalf("CompareFile returned error: %v", err)
	}
	for _, line := range result.DiffEntryLines {
		if line.DisplayNoNewLineWarning {
			return
		}
	}
	t.Fatalf("expected displayNoNewLineWarning in %#v", result.DiffEntryLines)
}

func TestDiffContext_ExpandsInjectedContextWithoutCache(t *testing.T) {
	service := NewService(ServiceDeps{})
	oldFS := fakeReadOnlySkillFS{entries: []EntryInfo{fileEntry("SKILL.md", "h_old", false, "markdown")}, files: map[string][]byte{"SKILL.md": []byte("a\nb\nc\nd\ne\nf\n")}}
	newFS := fakeReadOnlySkillFS{entries: []EntryInfo{fileEntry("SKILL.md", "h_new", false, "markdown")}, files: map[string][]byte{"SKILL.md": []byte("a\nB\nc\nd\nE\nf\n")}}

	result, err := service.CompareFile(context.Background(), oldFS, newFS, DiffOptions{Path: "SKILL.md", Mode: "context", OldStart: 3, NewStart: 3, Lines: 2})
	if err != nil {
		t.Fatalf("CompareFile context returned error: %v", err)
	}
	assertLineTypes(t, result.DiffEntryLines, "INJECTED_CONTEXT")
	if result.CacheWritten {
		t.Fatal("context expansion wrote diff/tree cache")
	}
}
