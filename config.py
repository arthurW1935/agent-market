"""Platform configuration. Model IDs live here so a swap is a one-line change."""

import os

RUBRIC_MODEL = "claude-sonnet-4-6"
VERIFIER_MODEL = "claude-opus-5"

PLATFORM_BASE_URL = os.environ.get("PLATFORM_BASE_URL", "http://localhost:8000")

MOCK_LLM = os.environ.get("MOCK_LLM", "") == "1"

ALLOWED_SKILLS = ["research", "writing", "extraction"]

TAKE_RATE_PERCENT = 5
MAX_ATTEMPTS = 3
DELIVERABLE_TIMEOUT_SECONDS = 30.0
DISPATCH_MAX_TRIES = 5
DISPATCH_BACKOFFS = [0.5, 1.0, 2.0, 4.0]  # sleeps between the 5 tries
DISPATCH_HTTP_TIMEOUT = 5.0

REP_START = 3.0
REP_PASS_DELTA = 0.3
REP_FAIL_DELTA = -0.4
REP_CAP = 5.0
REP_FLOOR = 1.0

RUBRIC_DISCUSSION_SOFT_CAP = 5
SSE_PING_SECONDS = 15.0
