# Runbook: Supabase + Fly.io

The API process moves to Fly.io. The database moves to Supabase Postgres.
This machine keeps working as it does now: `.\run.ps1` still builds the
container, still stores the game in `data/app.db`, and still reads the admin
password from `secrets/admin.json`.

`.\run.ps1` does **not** read `DATABASE_URL`. A connection string sitting in
your shell will not point the local container at Supabase.

Do the hosted cutover in this order:

1. Turn admin-request logging off (already the Fly setting; optional locally).
2. Create the Supabase project and copy its **session pooler** URI.
3. Stop the local stack and copy `data/app.db` into Postgres.
4. Set the Fly secrets and deploy.
5. Point the game client at the Fly hostname.

Deploy only after the copy. The first boot creates tables if they are missing,
and a health check does not insert rows, but a live game would write users
while the copy is running.

## What talks to what

```
Godot / itch.io  --HTTPS-->  Fly.io (this API)  --TLS-->  Supabase Postgres
                                    |
                                    +-- admin dashboard, same host
```

Fly runs the same image as local docker. Supabase holds the rows. The app
does not use the Supabase client libraries or the anon key. It opens a normal
Postgres connection with SQLAlchemy.

| | Local (`.\run.ps1`) | Fly + Supabase |
|---|---|---|
| Process | Docker, port 8000 inside the container | Fly proxy to port 8000 |
| Database | `data/app.db` (SQLite) | Supabase, schema `app` |
| Admin password | `secrets/admin.json` | `ADMIN_USERNAME` / `ADMIN_PASSWORD` secrets |
| Admin request log | on, unless `LOG_ADMIN_REQUESTS=0` | off (`fly.toml`) |
| Client IP | the TCP peer | `X-Forwarded-For` from Fly's proxy |

Startup prints two lines into the logs:

```
Database: sqlite /app/data/app.db
Admin request log: on
```

On Fly those lines should read `postgres <host>:5432/postgres schema=app` and
`Admin request log: off`.

## 1. Admin request logging

`LOG_ADMIN_REQUESTS` controls whether `/api/admin/*` is stored in
`admin_requests`. Anything else under `/api` still goes to `users_requests`.

| Value | Meaning |
|---|---|
| unset, `1`, `true`, `yes`, `on` | collect admin requests |
| `0`, `false`, `no`, `off` | do not collect them |

The request is still served. Bodies are not read and no row is written.
`users_requests`, `health`, and `suspicious_events` are unchanged. The
dashboard's Health button still inserts a `health` row; that is not the
request log.

Local docker, from this directory:

```powershell
$env:LOG_ADMIN_REQUESTS = "0"
.\run.ps1
```

Leave the variable unset, or set it to `1`, to keep today's behavior.
`fly.toml` sets `LOG_ADMIN_REQUESTS=0` because `admin_requests` is the table
that has been growing the fastest. Change that line to `"1"` before
`fly deploy` if the hosted dashboard should keep the log.

`users_requests` will then be the table that grows. It stores every player
request body (first 8 KB). Nothing in this cutover deletes old rows.

## 2. How the database is chosen

| Variable | Effect |
|---|---|
| `DATABASE_URL` unset or blank | SQLite at `DB_PATH` (default `data/app.db`) |
| `DATABASE_URL` set | Postgres. The SQLite file is not opened |
| `DB_SCHEMA` | Postgres schema. Default `app` |

`postgres://` and `postgresql://` URLs are rewritten to the psycopg driver.
A `supabase.co` host without `sslmode` gets `sslmode=require`.

Tables are created in schema `app`, not `public`. Supabase exposes `public`
through its Data API to anyone holding the anon key. `app` is not in that
list. Do not add `app` under Supabase → Project Settings → API → Exposed
schemas. You can also turn the Data API off; the Fly app will not notice.

The Supabase table editor opens on `public`. Switch the schema dropdown to
`app`, or query `app.users` in the SQL editor. An empty `public` schema does
not mean the copy failed.

On Postgres the app still creates missing tables and runs
`backend/migrations.py` at startup. `DATETIME` columns added for old SQLite
files are created as `TIMESTAMP` on Postgres. Fresh databases already have
the columns, so that path does nothing.

Use the **session pooler, port 5432**. Port 6543 is transaction mode: the
app supports it (no prepared statements, no connection pool of its own), but
the copy script wants one transaction and session state. Use the same
5432 URI for the script and for Fly.

Region: create the Supabase project in **Southeast Asia (Singapore)**,
`ap-southeast-1`. `fly.toml` uses Fly region `sin` so the API sits next to
the database.

## 3. Create the Supabase project

1. Create a project in `ap-southeast-1`. Keep the database password.
2. Connect → **Session pooler** → copy the URI. It looks like:

   ```
   postgresql://postgres.<project-ref>:<password>@aws-0-ap-southeast-1.pooler.supabase.com:5432/postgres
   ```

   Use the URI from the dashboard. It is already URL-encoded. Do not use the
   anon key or the service-role key.
3. Leave network restrictions off. Fly's outbound address is not a stable IP
   you can paste into an allow list. The password is what keeps the database
   closed.
4. Do not turn on Row Level Security for these tables, and do not add schema
   `app` to the exposed Data API schemas.

A free Supabase project pauses after a stretch of inactivity. The API cannot
answer while it is paused. Use a plan that does not pause if the game has to
stay up.

## 4. Copy `data/app.db` into Postgres

From this directory, with the venv that has the app dependencies (it needs
`psycopg`, which is in `backend/requirements.txt`):

```powershell
.\run.ps1 -Down
Copy-Item data\app.db data\app.db.bak

$env:DATABASE_URL = 'postgresql://postgres.<project-ref>:<password>@aws-0-ap-southeast-1.pooler.supabase.com:5432/postgres'
.\.venv\Scripts\python.exe tools\migrate_sqlite_to_postgres.py --sqlite data\app.db --dry-run
.\.venv\Scripts\python.exe tools\migrate_sqlite_to_postgres.py --sqlite data\app.db
```

Single quotes so PowerShell does not eat `$` or `&` in the password.
Keep that URI in `secrets/hosted.json` (gitignored). The app does not read that file.

The script:

- opens the SQLite file read-only and does not modify it
- creates schema `app` and the tables
- copies rows in foreign-key order, keeping `user_id` and every other id
- sets each Postgres sequence so the next insert is `max(id) + 1`
  (deleted user ids stay unused, same as SQLite)
- prints sqlite vs postgres counts and exits non-zero if they differ
- skips the leftover `requests` table (nothing reads it; see `database_schema.md`)

`--dry-run` creates the empty schema if it is missing, then prints counts.
It does not create tables and does not copy rows.

If the destination already has rows, the script stops. That includes a
second run after a successful copy. `--replace` truncates **only this app's
tables in schema `app`** and loads the snapshot again. It does not drop the
Supabase project or anything in `public`.

```powershell
.\.venv\Scripts\python.exe tools\migrate_sqlite_to_postgres.py --sqlite data\app.db --replace
```

Check in the Supabase SQL editor:

```sql
select 'users' as t, count(*) from app.users
union all select 'users_requests', count(*) from app.users_requests
union all select 'admin_requests', count(*) from app.admin_requests
union all select 'submitted_for_type_one', count(*) from app.submitted_for_type_one;
```

Those counts should match the script output. `public.users` should not exist.

Then start the local stack again if you still want it. It keeps using
`data/app.db`, including any rows created after the copy. The hosted
database will not see those rows unless you copy again with `--replace`
while the game is stopped.

## 5. Deploy the API to Fly

Install `flyctl` and sign in (`fly auth login`).

`fly.toml` is set to:

- app name `20seconds-api` (change it if Fly says the name is taken)
- region `sin`
- one shared-cpu-1x machine, 512 MB, always on
- `LOG_ADMIN_REQUESTS=0`
- `TRUST_PROXY_HEADERS=1` so bans and the request log see the player, not Fly's proxy
- health check `GET /` (the admin dashboard's index, no database write)

Rate limits live in process memory. Keep a single machine (`fly scale count 1`).
IP bans are in the database and reload within a few seconds; rate limits
would not be shared if you scaled out.

```powershell
fly apps create 20seconds-api
fly secrets set `
  DATABASE_URL="$env:DATABASE_URL" `
  ADMIN_USERNAME="admin" `
  ADMIN_PASSWORD="choose-a-long-password"
fly deploy
```

`DATABASE_URL` and the admin password are Fly secrets. They are not in
`fly.toml` and they are not baked into the image. Write the same values in
`secrets/hosted.json` so you have a local copy; that file is gitignored and
nothing in the app loads it. `.dockerignore` also keeps `data/` and
`secrets/` out of the build context, so `app.db` and the secret JSON files
are not uploaded.

`fly secrets set` restarts the machine. The first boot creates nothing if
the copy in step 4 already created the tables.

```powershell
fly status
fly logs
```

You want `Database: postgres ... schema=app` and `Admin request log: off`,
then a passing health check.

Open `https://20seconds-api.fly.dev/`. Log in with the username and password
you just set. `/docs` uses the same basic auth.

There is no volume and no SQLite file on the machine. Replacing the image
does not touch the database.

## 6. Point the game at Fly

The admin dashboard is same-origin: the browser calls `/api/...` on the Fly
host. Nothing in the frontend is hardcoded to localhost.

The Godot client (and the itch.io build) must call `https://20seconds-api.fly.dev`
instead of the local server. CORS already allows any origin, including
`html-classic.itch.zone`.

Player accounts, tiles, and holds are the rows you copied. A token issued
locally still works on Fly, because it is the `users.uuid` value in Postgres.

## 7. Check after deploy

- `https://<app>.fly.dev/` loads the dashboard.
- Login works with the Fly secret, not with `secrets/admin.json`.
- `fly logs` shows schema `app` and the admin request log off.
- Supabase → `app.users` row count matches the copy.
- Register a throwaway user from the game. A new row appears, and its
  `user_id` is higher than every copied id.
- Submit a tile. `app.submitted_for_type_one` or `app.submitted_for_type_two`
  gains a row. `app.admin_requests` stays still while you click around the
  dashboard.
- `app.users_requests` grows with each player call. That is expected.
- Ban an IP in the dashboard and call `/api/user/...` from that address.
  The refusal is `403` `IP_BANNED`.

## 8. Later deploys

Code changes:

```powershell
fly deploy
```

Schema changes ride along: startup runs `create_all` and `backend/migrations.py`.
Do not run the copy script as part of deploy. It would refuse, or with
`--replace` it would wipe the live game and load whatever is in the local
SQLite file.

To load a fresh SQLite snapshot over the live database, stop writers first:

```powershell
fly scale count 0
.\.venv\Scripts\python.exe tools\migrate_sqlite_to_postgres.py --sqlite data\app.db --replace
fly scale count 1
```

To rotate the database password, update it in Supabase, then:

```powershell
fly secrets set DATABASE_URL="postgresql://..."
```

## 9. When something goes wrong

**Fly logs say the schema could not be created, or the connection timed out.**
The URI is wrong, the project is paused, or you used the direct IPv6 host
(`db.<ref>.supabase.co`) from a network that only has IPv4. Use the session
pooler URI, port 5432, and wake the project in the Supabase dashboard.

**The dashboard lists no tables, but the SQL editor shows `app.users`.**
The API is pointed at a different database or schema than the one you are
viewing. Compare the host in `fly logs` (`Database: postgres ...`) with the
pooler host.

**The copy stops on a duplicate key.**
SQLite did not enforce a length or a uniqueness rule that Postgres does.
The transaction rolls back; the destination is left empty or as it was
before `--replace`. The error names the table and row. Fix or delete that
SQLite row and run again. Do not "fix" it by dropping the unique index.

**`user_id` collides after the copy.**
The sequence step did not run (the script would have exited). Run the copy
again with `--replace` rather than inserting around it.

**Every player shows the same IP, or nobody can be banned.**
`TRUST_PROXY_HEADERS` is off, so the app sees Fly's proxy. It is `1` in
`fly.toml`. Local docker leaves it off on purpose.

**Local docker suddenly has an empty database.**
The container should still be on `data/app.db`. `.\run.ps1 -Down` does not
delete `./data`. Confirm `fly` was not given that file: the image does not
contain it. If `DATABASE_URL` was passed into the container by hand, remove
it. Compose does not forward that variable.

**You need the old server back.**
`data/app.db.bak` plus `.\run.ps1` is the local stack. Fly can be scaled to
0 (`fly scale count 0`) without deleting the Supabase data.
