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

Apply the schema as the owner. The schema file uses `psql` variables for the writer and reader passwords. Send them through standard input so they never appear in a process's command line, where other local users could read them with `ps`:

```sh
set -a
. ./.env
set +a
{
  printf '\\set writer_pw %s\n' "'$RINGER_WRITER_PW'"
  printf '\\set reader_pw %s\n' "'$RINGER_READER_PW'"
  cat schema.sql
} | docker exec -i ringer-postgres psql -U ringer_owner -d ringer -v ON_ERROR_STOP=1
```

`printf` is a shell builtin, so the values are not passed as arguments to any program. Generated values from `openssl rand -hex 24` contain no quote characters; if you choose your own passwords, do not use a single quote. Do not pass the passwords with `psql -v writer_pw=...`.

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

### Grafana dashboard

`grafana-dashboard.json` is a ready-made dashboard for the same datasource: attempts, first-try pass rate, tokens and central-write fallbacks at the top; attempts per day by verdict and a verdict mix; a per-model scoreboard; tokens per day by model (top 8 models plus "Other"); attempts by task type; and recent failures and fallbacks. Filters for host, model and task type apply to every panel, and the default range is 90 days.

Import it in Grafana with Dashboards > New > Import and upload the file. It reads a datasource variable named `ds`, which defaults to a PostgreSQL datasource whose UID is `ringer-evidence` (name it that when you create or provision the datasource, or pick yours from the "Data source" dropdown at the top of the dashboard). The datasource must be the read-only role above.

Verdicts use fixed status colors (PASS green, FAIL red, TIMEOUT amber, ERROR orange) and always show their names. Models listed in the dashboard's color overrides keep a fixed color; add an override for a new model that becomes common, otherwise it falls back to Grafana's by-name palette. The queries read `ringer.attempts` only and need no extra grants.

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

Generate new values with `openssl rand -hex 24`.

**Writer and reader.** Put the new values in the protected `.env` file, apply the schema again using the command in "Provision" (re-running it is safe and resets those two role passwords), then update each client's protected env file with the new writer password, or each reporting tool with the new reader password.

**Owner.** Changing `RINGER_OWNER_PW` in `.env` and restarting the container does **not** change the stored password: `POSTGRES_PASSWORD` is only read when the data volume is first created. Change it inside the database instead, sending the value on standard input:

```sh
NEW_OWNER_PW=$(openssl rand -hex 24)
printf "ALTER ROLE ringer_owner PASSWORD '%s';\n" "$NEW_OWNER_PW" \
  | docker exec -i ringer-postgres psql -U ringer_owner -d ringer -v ON_ERROR_STOP=1
```

Then update `RINGER_OWNER_PW` in `.env` to match, and confirm the new password works and the old one is rejected (passwords are checked over TCP, not the container's local socket):

```sh
export PGPASSWORD="$NEW_OWNER_PW"
docker exec -e PGPASSWORD ringer-postgres psql -h 127.0.0.1 -U ringer_owner -d ringer -c 'select 1'
```

Do the same with the old value; it should fail with "password authentication failed". Update clients before you retire an old writer or reader password.

## Troubleshooting

The writer cannot `SELECT` from the attempts table; this is expected for the insert-only role. Use the reader credential for queries. An `INSERT ... ON CONFLICT` that names a conflict target such as `(attempt_uid)` needs `SELECT` privilege on that column, so clients must use `ON CONFLICT DO NOTHING` without a named target.
