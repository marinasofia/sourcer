"""AWS Lambda entry point.

Runs the same pipeline as ``python -m sourcer.main``, with the SQLite database
persisted to S3 between invocations (Lambda filesystems are ephemeral):
download before the run, upload after, so dedupe keeps working and each
scheduled run only pays for new postings.

Required environment:
  SOURCER_API_KEY_PARAM  SSM SecureString holding the API key (deploy/aws.sh
                        stores it there); ANTHROPIC_API_KEY works for local runs
  SOURCER_S3_BUCKET    bucket for sourcer.db and data.json
  SOURCER_S3_BUCKET_OWNER  the account that must own that bucket
  SOURCER_DB=/tmp/sourcer.db   (Lambda can only write under /tmp)

``SOURCER_SKIP_S3=1`` runs the pipeline without persistence, for local
container smoke tests with the Lambda runtime emulator. boto3 is provided by
the Lambda runtime and is an optional extra (``pip install sourcer-jobs[aws]``).
"""

from __future__ import annotations

import os
from typing import Any

# Lambda isolates /tmp to this function execution environment.
os.environ.setdefault("SOURCER_DB", "/tmp/sourcer.db")  # noqa: S108  # nosec B108

from . import config, log
from .export import export
from .main import run

DB_KEY = "sourcer.db"
DATA_KEY = "data.json"
# The export shares the function-isolated Lambda temporary directory.
DATA_PATH = "/tmp/data.json"  # noqa: S108  # nosec B108
SUMMARY_KEYS = (
    "companies_covered",
    "postings_seen",
    "new_postings",
    "dedupe_count",
    "parse_failures",
    "score_failures",
    "excluded_count",
    "est_cost_usd",
    "duration_s",
)

logger = log.get_logger(__name__)


def _s3() -> Any:
    import boto3  # noqa: PLC0415  optional dependency, present in the Lambda runtime

    return boto3.client("s3")


def _load_api_key() -> None:
    """The key from SSM, decrypted at cold start, so it never sits in the
    function's configuration where lambda:GetFunctionConfiguration shows it."""
    name = os.environ.get("SOURCER_API_KEY_PARAM")
    if not name or os.environ.get("ANTHROPIC_API_KEY"):
        return
    import boto3  # noqa: PLC0415  optional dependency, present in the Lambda runtime

    reply = boto3.client("ssm").get_parameter(Name=name, WithDecryption=True)
    os.environ["ANTHROPIC_API_KEY"] = reply["Parameter"]["Value"]


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    del event, context
    log.configure()
    _load_api_key()
    skip_s3 = os.environ.get("SOURCER_SKIP_S3") == "1"
    s3 = None if skip_s3 else _s3()
    bucket = os.environ.get("SOURCER_S3_BUCKET", "")
    owner = os.environ.get("SOURCER_S3_BUCKET_OWNER")
    # a bucket of the same name in another account is refused, not written to
    extra = {"ExtraArgs": {"ExpectedBucketOwner": owner}} if owner else {}
    if s3 is not None:
        try:
            s3.download_file(bucket, DB_KEY, str(config.DB_PATH), **extra)
            logger.info("restored %s from s3://%s", DB_KEY, bucket)
        except s3.exceptions.ClientError:
            logger.info("no existing database in S3; starting fresh")

    final = run()
    export(final["conn"], DATA_PATH)
    if s3 is not None:
        s3.upload_file(str(config.DB_PATH), bucket, DB_KEY, **extra)
        s3.upload_file(DATA_PATH, bucket, DATA_KEY, **extra)

    summary = {k: final["stats"].get(k) for k in SUMMARY_KEYS}
    logger.info("summary: %s", summary)
    return summary
