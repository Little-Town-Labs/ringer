# Central evidence store

## What this is

This is a shared Postgres store for Ringer attempt rows. JSONL remains the local source of truth; the database is a shared reporting and ingestion destination.

## Provision

Create a `.env` file beside `compose.yaml` with three independently generated passwords:

```sh
umask 077
cat > .env <<EOF_ENV
RINGER_OWNER_PW=$(openssl rand -hex 24)
RINGER_WRITER_PW=$(openssl rand -hex 24)
RINGER_READER_PW=$(openssl rand -hex 24)
EOF_ENV
chmod 600 .env
```

Start Postgres:

```sh
docker compose --env-file .env up -d
```

Apply the schema as the owner. The schema file uses `psql` variables for the writer and reader passwords:

```sh
set -a
. ./.env
set +a
docker exec -i ringer-postgres psql -U ringer_owner -d ringer -v ON_ERROR_STOP=1 -v writer_pw="$RINGER_WRITER_PW" -v reader_pw="$RINGER_READER_PW" < schema.sql
```

Verify the objects and roles:

```sh
docker exec -it ringer-postgres psql -U ringer_owner -d ringer -c "SELECT table_schema, table_name FROM information_schema.tables WHERE table_schema = 'ringer' ORDER BY table_name;"
docker exec -it ringer-postgres psql -U ringer_owner -d ringer -c "SELECT table_name FROM information_schema.views WHERE table_schema = 'ringer' ORDER BY table_name;"
docker exec -it ringer-postgres psql -U ringer_owner -d ringer -c "SELECT rolname FROM pg_roles WHERE rolname IN ('ringer_owner', 'ringer_writer', 'ringer_reader') ORDER BY rolname;"
```

## Roles

`ringer_writer` is insert-only. `ringer_reader` is select-only. Use the owner role for administration. Give machines only the writer credential and reporting tools only the reader credential.

## Client env file

Create a client env file outside the repository and set its mode to 600. Replace the placeholders with the database endpoint and the writer credential:

```dotenv
RINGER_DB_HOST=<db-host>
RINGER_DB_PORT=<db-port>
RINGER_DB_USER=ringer_writer
RINGER_DB_PASSWORD=<writer-password>
RINGER_DB_NAME=ringer
```

## Reporting

The `ringer.model_scoreboard` and `ringer.daily_activity` views provide reporting data. Configure Grafana's PostgreSQL datasource with the reader role:

```yaml
type: postgres
url: <db-host>:<db-port>
user: ringer_reader
secureJsonData:
  password: <reader-password>
database: ringer
sslmode: disable
```

DuckDB can read the same views using its PostgreSQL extension:

```sql
INSTALL postgres;
LOAD postgres;
ATTACH 'host=<db-host> port=<db-port> dbname=ringer user=ringer_reader password=<reader-password>' AS evidence (TYPE postgres);
SELECT * FROM evidence.ringer.model_scoreboard;
```

## Backups and restore

Run `backup.sh` nightly as the database service user. For example, set `RINGER_BACKUP_DIR` in the job environment if the default directory is not suitable, then add this cron entry:

```cron
17 3 * * * /path/to/central-evidence/backup.sh
```

The script writes compressed `pg_dump` files and removes dumps older than 14 days. To restore a dump into an empty database, create the database and roles as appropriate, then stream the dump to its owner connection:

```sh
gunzip -c /path/to/ringer-YYYY-MM-DD.sql.gz | psql -v ON_ERROR_STOP=1 -U ringer_owner -d ringer
```

## Rotating credentials

Generate new values for the relevant password variables with `openssl rand -hex 24`, update the protected `.env` files used by the service and clients, then apply the schema again to reset the writer and reader role passwords. Restart the service if its owner password changed; update clients with the new role password before removing the old value from their protected env files.

## Troubleshooting

The writer cannot `SELECT` from the attempts table; this is expected for the insert-only role. Use the reader credential for queries. An `INSERT ... ON CONFLICT` that names a conflict target such as `(attempt_uid)` needs `SELECT` privilege on that column, so clients must use `ON CONFLICT DO NOTHING` without a named target.
