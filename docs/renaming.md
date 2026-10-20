# Upgrading an existing installation to Sourcer

The Python distribution is `sourcer-jobs`, the import package is `sourcer`, and
the command is `sourcer`. All application environment variables use `SOURCER_`.
Install the renamed package into the environment used by each caller and update
imports, `python -m` commands, MCP configuration, and scheduled workflows together.
Existing unprefixed provider credentials, such as ANTHROPIC_API_KEY and the
SNOWFLAKE variables, retain their names.

Back up the existing database before upgrading. Either set SOURCER_DB to its
current path or move it to `data/sourcer.db`. The database schema is unchanged;
no export/reimport is necessary. Rename the application variable prefix in the
process environment, .env files, CI variables, and deployment secret references.
Files outside this public checkout are not migrated automatically.

The Lambda examples use the object key `sourcer.db` and new Sourcer resource
names. Preserve the existing S3 database and data.json, then copy them to the
configured destination before switching a scheduled caller. Do not deploy an
empty state bucket over a working installation. Kubernetes resources, Lambda
functions, ECR images, IAM roles, and buckets require an operator-managed cutover;
changing these examples does not rename deployed cloud resources.

The public repository uses this address:

- https://github.com/marinasofia/sourcer

Update external bookmarks and workflow checkout references to the new addresses.
Historical commits and already published package installations retain their
original identities; replace installed environments rather than mixing packages.
