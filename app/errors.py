"""Errors shown to the user, as a code + parameters: the page translates them.

The server never builds user-facing sentences; `code` is looked up under
"errors" in static/i18n/<lang>.json (e.g. "gemini.overloaded"), with
`params` filling the {placeholders}.
"""


class AppError(Exception):
    status = 400  # HTTP status of the response

    # `http_status`, not `status`: messages use {status} for the provider's own
    # error code, passed in **params.
    def __init__(self, code: str, http_status: int | None = None, **params):
        super().__init__(code)
        self.code = code
        self.params = params
        if http_status is not None:
            self.status = http_status

    def detail(self) -> dict:
        return {"code": self.code, "params": self.params}

    def __str__(self) -> str:  # logs and tests
        return f"{self.code} {self.params}" if self.params else self.code
