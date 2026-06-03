# Changelog

All notable changes to this project, newest first. Entries are grouped by UTC date.

## 2026-06-02

### Add CSV export — 14:30 UTC
**Summary:** Users can now download their report as a spreadsheet file.
**Details:** Adds a GET /api/reports/:id/export endpoint that streams report rows as CSV. Auth-protected, validates ownership, paginates large datasets.
