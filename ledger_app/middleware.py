from __future__ import annotations

from typing import Callable, Iterable


class PrefixMiddleware:
    """
    Make Flask work under a URL prefix like /PM behind Nginx.

    It relies on Nginx setting:
      proxy_set_header X-Forwarded-Prefix /PM;

    This middleware will:
    - set SCRIPT_NAME to that prefix
    - strip the prefix from PATH_INFO (if present)
    so url_for() and redirects keep the prefix stable.
    """

    def __init__(self, app: Callable, prefix_header: str = "HTTP_X_FORWARDED_PREFIX"):
        self.app = app
        self.prefix_header = prefix_header

    def __call__(self, environ: dict, start_response: Callable):
        prefix = (environ.get(self.prefix_header) or "").strip()
        if prefix:
            if not prefix.startswith("/"):
                prefix = "/" + prefix
            prefix = prefix.rstrip("/")

            path = environ.get("PATH_INFO", "") or ""
            if path == prefix:
                environ["PATH_INFO"] = "/"
            elif path.startswith(prefix + "/"):
                environ["PATH_INFO"] = path[len(prefix) :]

            environ["SCRIPT_NAME"] = prefix

        return self.app(environ, start_response)

