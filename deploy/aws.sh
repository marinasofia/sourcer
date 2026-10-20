#!/bin/bash
# One-shot AWS Lambda deploy for Sourcer. Prereqs: aws configure done, Docker
# running, ANTHROPIC_API_KEY in ../.env or exported. Idempotent where the AWS
# APIs allow; safe to re-run. The EventBridge schedule is deliberately NOT
# created: GitHub Actions is the one scheduler. Invoke manually to test.
set -euo pipefail
cd "$(dirname "$0")/.."

REGION="${AWS_REGION:-us-east-1}"
ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
ECR="$ACCOUNT.dkr.ecr.$REGION.amazonaws.com"
BUCKET="sourcer-state-$ACCOUNT"
KEY="${ANTHROPIC_API_KEY:-$(grep '^ANTHROPIC_API_KEY=' .env | cut -d= -f2-)}"
[ -n "$KEY" ] || { echo "no ANTHROPIC_API_KEY found"; exit 1; }

echo "== account $ACCOUNT, region $REGION"

aws s3api head-bucket --bucket "$BUCKET" --expected-bucket-owner "$ACCOUNT" 2>/dev/null \
  || aws s3 mb "s3://$BUCKET" --region "$REGION"
# private, versioned, encrypted at rest, whatever the account defaults are
aws s3api put-public-access-block --bucket "$BUCKET" --public-access-block-configuration \
  BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
aws s3api put-bucket-versioning --bucket "$BUCKET" --versioning-configuration Status=Enabled
aws s3api put-bucket-encryption --bucket "$BUCKET" --server-side-encryption-configuration \
  '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"}}]}'
# the API key lives in SSM as a SecureString, read by the function at cold start
PARAM=/sourcer/anthropic-api-key
aws ssm put-parameter --name "$PARAM" --type SecureString --value "$KEY" --overwrite \
  --region "$REGION" >/dev/null
aws ecr describe-repositories --repository-names sourcer --region "$REGION" >/dev/null 2>&1 \
  || aws ecr create-repository --repository-name sourcer --region "$REGION" >/dev/null

aws ecr get-login-password --region "$REGION" | docker login --username AWS --password-stdin "$ECR"
docker build -f Dockerfile.lambda -t "$ECR/sourcer:latest" .
docker push "$ECR/sourcer:latest"

NEW_ROLE=0
aws iam get-role --role-name sourcer-lambda >/dev/null 2>&1 || {
  aws iam create-role --role-name sourcer-lambda --assume-role-policy-document \
    '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"lambda.amazonaws.com"},"Action":"sts:AssumeRole"}]}' >/dev/null
  aws iam attach-role-policy --role-name sourcer-lambda --policy-arn arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole
  NEW_ROLE=1
}
# Least privilege, refreshed on every deploy: exactly two keys in one bucket
# (DB_KEY and DATA_KEY in sourcer/lambda_handler.py) and the one parameter.
aws iam put-role-policy --role-name sourcer-lambda --policy-name sourcer-state-s3 \
  --policy-document "{\"Version\":\"2012-10-17\",\"Statement\":[
    {\"Effect\":\"Allow\",\"Action\":[\"s3:GetObject\",\"s3:PutObject\"],
     \"Resource\":[\"arn:aws:s3:::$BUCKET/sourcer.db\",\"arn:aws:s3:::$BUCKET/data.json\"]},
    {\"Effect\":\"Allow\",\"Action\":[\"s3:ListBucket\"],
     \"Resource\":\"arn:aws:s3:::$BUCKET\"},
    {\"Effect\":\"Allow\",\"Action\":[\"ssm:GetParameter\"],
     \"Resource\":\"arn:aws:ssm:$REGION:$ACCOUNT:parameter$PARAM\"}]}"
if [ "$NEW_ROLE" = 1 ]; then echo "role created; waiting for IAM propagation"; sleep 12; fi
ENVIRONMENT="Variables={SOURCER_API_KEY_PARAM=$PARAM,SOURCER_S3_BUCKET=$BUCKET,SOURCER_S3_BUCKET_OWNER=$ACCOUNT,SOURCER_COST_CAP=1.00}"

if aws lambda get-function --function-name sourcer --region "$REGION" >/dev/null 2>&1; then
  aws lambda update-function-code --function-name sourcer \
    --image-uri "$ECR/sourcer:latest" --region "$REGION" >/dev/null
  aws lambda wait function-updated --function-name sourcer --region "$REGION"
  # moves an older deploy's plaintext key out of the function configuration
  aws lambda update-function-configuration --function-name sourcer \
    --environment "$ENVIRONMENT" --region "$REGION" >/dev/null
  echo "== function code updated"
else
  aws lambda create-function --function-name sourcer \
    --package-type Image --code ImageUri="$ECR/sourcer:latest" \
    --role "arn:aws:iam::$ACCOUNT:role/sourcer-lambda" \
    --timeout 900 --memory-size 1024 --region "$REGION" \
    --environment "$ENVIRONMENT" >/dev/null
  echo "== function created"
fi
# one run at a time: each invocation may spend up to its cost cap
aws lambda put-function-concurrency --function-name sourcer \
  --reserved-concurrent-executions 1 --region "$REGION" >/dev/null

echo "== deployed. Test with:"
echo "aws lambda invoke --function-name sourcer --region $REGION --cli-read-timeout 900 /dev/stdout"
