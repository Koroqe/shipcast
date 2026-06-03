# Changelog

All notable changes to this project, newest first. Entries are grouped by UTC date.

## 2026-06-02

### Add CSV export — 14:30 UTC
**Summary:** Users can now download their report as a spreadsheet file.
**Details:** Adds a GET /api/reports/:id/export endpoint that streams report rows as CSV.

### Fix login redirect loop — 11:05 UTC
**Summary:** Logging in no longer gets stuck reloading the page.
**Details:** Removes the duplicate redirect that bounced authenticated users back to the login screen.
