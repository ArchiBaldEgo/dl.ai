"""Custom admin authentication helpers.

All admin authentication now goes through the external DLSID cookie + the
``ExternalAuthMiddleware`` flow. There is no separate test-panel login,
no auto-login-from-external hook, and no custom admin login form. The
``AIAdminSite`` redirects unauthenticated users to ``dl.gsu.by`` via the
``has_permission`` check; see ``ai/admin/site.py``.
"""
