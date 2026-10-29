"""Tag log lines with the ID the browser sends in X-Request-ID, so an error a
user reports can be found in every log (Caddy, gunicorn, Django).

Kept free of model imports: LOGGING loads the filter before apps are ready."""

import contextvars
import logging
import re

request_id = contextvars.ContextVar('request_id', default='-')

VALID_ID = re.compile(r'[A-Za-z0-9-]{1,64}')


class RequestIDMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        value = request.headers.get('X-Request-ID', '')
        # Not reset on the way out: Django logs 4xx/5xx after the middleware returns
        request_id.set(value if VALID_ID.fullmatch(value) else '-')
        return self.get_response(request)


class RequestIDFilter(logging.Filter):
    def filter(self, record):
        record.request_id = request_id.get()
        return True
