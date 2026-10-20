"""Lambda handler tests: S3 restore and upload paths, and the emulator path without S3."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from sourcer import lambda_handler


class FakeClientError(Exception):
    pass


def fake_s3(*, existing_db: bool) -> MagicMock:
    s3 = MagicMock()
    s3.exceptions.ClientError = FakeClientError
    if not existing_db:
        s3.download_file.side_effect = FakeClientError("404")
    return s3


FINAL = {"conn": MagicMock(), "stats": {"new_postings": 3, "est_cost_usd": 0.01, "duration_s": 1.0}}


def test_handler_restores_runs_and_uploads(monkeypatch):
    monkeypatch.delenv("SOURCER_SKIP_S3", raising=False)
    monkeypatch.setenv("SOURCER_S3_BUCKET", "bucket")
    s3 = fake_s3(existing_db=True)
    with (
        patch("sourcer.lambda_handler._s3", return_value=s3),
        patch("sourcer.lambda_handler.run", return_value=FINAL) as run,
        patch("sourcer.lambda_handler.export") as export,
    ):
        summary = lambda_handler.handler({}, None)
    s3.download_file.assert_called_once()
    assert s3.download_file.call_args.args[:2] == ("bucket", "sourcer.db")
    run.assert_called_once_with()
    export.assert_called_once_with(FINAL["conn"], lambda_handler.DATA_PATH)
    assert s3.upload_file.call_count == 2
    assert [c.args[2] for c in s3.upload_file.call_args_list] == ["sourcer.db", "data.json"]
    assert summary["new_postings"] == 3
    assert set(summary) == set(lambda_handler.SUMMARY_KEYS)
    assert summary["companies_covered"] is None


def test_handler_starts_fresh_when_no_database_in_s3(monkeypatch):
    monkeypatch.delenv("SOURCER_SKIP_S3", raising=False)
    monkeypatch.setenv("SOURCER_S3_BUCKET", "bucket")
    s3 = fake_s3(existing_db=False)
    with (
        patch("sourcer.lambda_handler._s3", return_value=s3),
        patch("sourcer.lambda_handler.run", return_value=FINAL),
        patch("sourcer.lambda_handler.export"),
    ):
        lambda_handler.handler({}, None)
    assert s3.upload_file.call_count == 2


def test_handler_skips_s3_for_local_emulator(monkeypatch):
    monkeypatch.setenv("SOURCER_SKIP_S3", "1")
    with (
        patch("sourcer.lambda_handler._s3") as s3_factory,
        patch("sourcer.lambda_handler.run", return_value=FINAL),
        patch("sourcer.lambda_handler.export") as export,
    ):
        summary = lambda_handler.handler({}, None)
    s3_factory.assert_not_called()
    export.assert_called_once()
    assert summary["est_cost_usd"] == 0.01


def test_s3_factory_builds_a_boto3_client():
    fake_boto3 = MagicMock()
    with patch.dict("sys.modules", {"boto3": fake_boto3}):
        client = lambda_handler._s3()
    fake_boto3.client.assert_called_once_with("s3")
    assert client is fake_boto3.client.return_value


def test_db_path_defaults_under_tmp():
    assert lambda_handler.DATA_PATH.startswith("/tmp/")


def test_handler_refuses_a_bucket_owned_by_another_account(monkeypatch):
    monkeypatch.delenv("SOURCER_SKIP_S3", raising=False)
    monkeypatch.setenv("SOURCER_S3_BUCKET", "bucket")
    monkeypatch.setenv("SOURCER_S3_BUCKET_OWNER", "111122223333")
    s3 = fake_s3(existing_db=True)
    with (
        patch("sourcer.lambda_handler._s3", return_value=s3),
        patch("sourcer.lambda_handler.run", return_value=FINAL),
        patch("sourcer.lambda_handler.export"),
    ):
        lambda_handler.handler({}, None)
    owner = {"ExtraArgs": {"ExpectedBucketOwner": "111122223333"}}
    assert s3.download_file.call_args.kwargs == owner
    assert all(c.kwargs == owner for c in s3.upload_file.call_args_list)


def test_the_api_key_comes_from_ssm_when_not_in_the_environment(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("SOURCER_API_KEY_PARAM", "/test/model-key-param")
    ssm = MagicMock()
    ssm.get_parameter.return_value = {"Parameter": {"Value": "sk-from-ssm"}}
    boto3 = MagicMock()
    boto3.client.return_value = ssm
    with patch.dict("sys.modules", {"boto3": boto3}):
        lambda_handler._load_api_key()
    ssm.get_parameter.assert_called_once_with(Name="/test/model-key-param", WithDecryption=True)
    import os  # noqa: PLC0415

    assert os.environ["ANTHROPIC_API_KEY"] == "sk-from-ssm"
    monkeypatch.delenv("ANTHROPIC_API_KEY")
