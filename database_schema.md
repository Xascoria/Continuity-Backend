# Database schema

SQLite database, stored at `data/app.db` (override with the `DB_PATH` env var).

The source of truth is the SQLAlchemy models in `backend/models.py`. Tables are
created at startup by `Base.metadata.create_all()` (`backend/main.py`), and
columns added to an existing table later are applied by `backend/migrations.py`.
The SQL at the bottom of this file is generated from the models.

## Conventions

- Every table has `created_at` (`DATETIME`, not null), set by the application
  to the current time in **UTC**. SQLite stores no timezone, so values are
  naive UTC (e.g. `2026-10-25T03:28:10.197181`).
- Foreign keys are enforced (`PRAGMA foreign_keys=ON` on every connection).
- `VARCHAR(n)` lengths are not enforced by SQLite itself. The API enforces
  them (usernames by `word_scanner`, canvas strings by request validation).

## Overview

| Table | Purpose |
|---|---|
| `users` | Registered users and their uuid token |
| `submitted_for_type_one` | Results submitted for game type 1 |
| `submitted_for_type_two` | Results submitted for game type 2 |
| `tile_holds` | 40-second claims on a tile of an iteration (see `backend_design.md`) |
| `game_state` | Per game type `state_version`, bumped by every accepted submission |
| `suspicious_events` | Suspicious activity shown on the admin dashboard |
| `banned_ips` | IPs refused (HTTP 403 `IP_BANNED`) on every non-admin `/api` request |
| `users_requests` | Log of every non-admin `/api/*` request (tokens kept) |
| `admin_requests` | Log of every `/api/admin/*` request (user tokens redacted) |
| `health` | Rows written by the dashboard's health check |

```
users 1 ──< submitted_for_type_one   (user_id, ON DELETE CASCADE)
users 1 ──< submitted_for_type_two   (user_id, ON DELETE CASCADE)
users 1 ──< tile_holds               (user_id, ON DELETE CASCADE)
users_requests.user_id  -> users.user_id   (logical link only, no foreign key)
```

## `users`

| Column | Type | Notes |
|---|---|---|
| `user_id` | INTEGER | Primary key, autoincrement. Ids of deleted users are never reused. |
| `username` | VARCHAR(12) | Not null. Unique case-insensitively (`ix_users_username_lower` on `lower(username)`). |
| `uuid` | VARCHAR(36) | Not null, unique. The user's token, issued at registration. |
| `registered_at` | DATETIME | Not null, set at registration. |
| `banned_at` | DATETIME | NULL unless banned (hold limit exceeded). Banned tokens get `USER_BANNED` on every user endpoint. Clear it to lift a ban. |
| `created_at` | DATETIME | Not null. |

## `submitted_for_type_one` / `submitted_for_type_two`

Both tables have the same columns.

| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER | Primary key. |
| `user_id` | INTEGER | Not null, indexed. Foreign key to `users.user_id`, `ON DELETE CASCADE` (deleting a user deletes their submissions). |
| `submitted_string` | VARCHAR(1024) | Not null. The normalized canvas (no `.`), 12-symbol alphabet. |
| `accuracy` | FLOAT | Not null. Ratio 0-1 against the reference frozen on the hold. |
| `iteration` | INTEGER | Not null, default `0`. The layer the tile belongs to. |
| `x`, `y` | INTEGER | Tile coordinate (`x` 0-14, `y` 0-11). Nullable only for pre-migration rows, which are ignored for tile state. |
| `submission_id` | VARCHAR(36) | Client-generated uuid; unique together with `user_id` (idempotent retries). |
| `reference_iteration` | INTEGER | Layer the tile was scored against. |
| `context_sources` | TEXT | Game type 2: JSON list of each context region's `source_iteration`. |
| `time_taken` | FLOAT | Seconds reported by the client. |
| `created_at` | DATETIME | Not null. |

`(iteration, x, y)` is unique: a tile is accepted once per iteration.

## `tile_holds`

| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER | Primary key. |
| `game_type` | INTEGER | Not null (1 or 2). |
| `iteration`, `x`, `y` | INTEGER | Not null. The held tile. Unique together with `game_type`. |
| `user_id` | INTEGER | Not null, indexed, foreign key to `users`, `ON DELETE CASCADE`. |
| `acquired_at`, `expires_at` | DATETIME | Not null. A hold lasts 40 seconds. |
| `reference_iteration` | INTEGER | Not null. Reference layer frozen at allocation. |
| `context_sources` | TEXT | Game type 2: frozen per-region source iterations (JSON). |
| `created_at` | DATETIME | Not null. |

A hold is deleted when its submission is accepted, when the user requests a new
square of the same game type, or when the user is banned. Expired rows are
overwritten when the tile is allocated again.

## `suspicious_events`

Written by `backend/security.py`; detection only logs, it never blocks.

| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER | Primary key. |
| `event_type` | VARCHAR | Not null, indexed. See the list in `security.py` (e.g. `RATE_LIMIT_EXCEEDED`, `TRAFFIC_SPIKE`, `REGISTER_FLOOD`, `FAST_SUBMISSIONS`, `AUTO_BAN`). |
| `severity` | VARCHAR | Not null: `info`, `warning` or `critical`. |
| `client_ip` | VARCHAR | Indexed. |
| `user_id` | INTEGER | Indexed. Not a foreign key, so deleting a user keeps the evidence. |
| `details` | TEXT | JSON object with event-specific data. |
| `created_at` | DATETIME | Not null. |

## `banned_ips`

Managed from the admin dashboard (Ban IP / Unban IP on the suspicious events
tab). The middleware reloads the list every few seconds. Admin endpoints are
never blocked.

| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER | Primary key. |
| `ip` | VARCHAR | Not null, unique. |
| `reason` | VARCHAR | Optional. |
| `created_at` | DATETIME | Not null. |

## `game_state`

| Column | Type | Notes |
|---|---|---|
| `game_type` | INTEGER | Primary key (1 or 2). |
| `state_version` | INTEGER | Not null. Incremented by every accepted submission. |
| `created_at` | DATETIME | Not null. |

## `users_requests`

One row per `/api/*` request that is not under `/api/admin`. Request and
response bodies are stored as-is (first 8 KB each). `Authorization` and cookie
headers are never stored.

| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER | Primary key. |
| `method` | VARCHAR(10) | Not null. |
| `path` | VARCHAR | Not null. |
| `query_string` | TEXT | |
| `client_ip` | VARCHAR | |
| `headers` | TEXT | JSON object of request headers. |
| `request_body` | TEXT | |
| `response_status` | INTEGER | |
| `response_body` | TEXT | |
| `duration_ms` | FLOAT | |
| `token` | VARCHAR | Indexed. The token the request carried (for `register`, the token it issued). Kept even if it matches no user. |
| `user_id` | INTEGER | Indexed. Set when the token resolved to a user. Not a foreign key, so deleting a user never affects the log. |
| `created_at` | DATETIME | Not null. |

## `admin_requests`

One row per `/api/admin/*` request, including failed logins. Same columns as
`users_requests` except `token` and `user_id`. User tokens (uuids) in headers,
query string and bodies are replaced with `[uuid]`.

| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER | Primary key. |
| `method` | VARCHAR(10) | Not null. |
| `path` | VARCHAR | Not null. |
| `query_string` | TEXT | |
| `client_ip` | VARCHAR | |
| `headers` | TEXT | JSON object. |
| `request_body` | TEXT | |
| `response_status` | INTEGER | |
| `response_body` | TEXT | |
| `duration_ms` | FLOAT | |
| `created_at` | DATETIME | Not null. |

## `health`

| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER | Primary key. |
| `status` | VARCHAR | Not null, default `ok`. |
| `created_at` | DATETIME | Not null. |

## Indexes

| Index | Table | Columns | Unique |
|---|---|---|---|
| `ix_users_username_lower` | `users` | `lower(username)` | yes |
| `ix_users_uuid` | `users` | `uuid` | yes |
| `ix_submitted_for_type_one_user_id` | `submitted_for_type_one` | `user_id` | no |
| `ix_submitted_for_type_two_user_id` | `submitted_for_type_two` | `user_id` | no |
| `ux_type_one_tile` / `ux_type_two_tile` | submission tables | `iteration, x, y` | yes |
| `ux_type_one_submission` / `ux_type_two_submission` | submission tables | `user_id, submission_id` | yes |
| `ix_type_one_xy_iteration` / `ix_type_two_xy_iteration` | submission tables | `x, y, iteration` | no |
| `ux_tile_holds_tile` | `tile_holds` | `game_type, iteration, x, y` | yes |
| `ix_tile_holds_user_id` | `tile_holds` | `user_id` | no |
| `ix_users_requests_token` | `users_requests` | `token` | no |
| `ix_users_requests_user_id` | `users_requests` | `user_id` | no |

## Notes

- An older database may still contain a leftover `requests` table from before
  the request log was split in two. Nothing uses it and it can be dropped
  (`DROP TABLE requests;`).
- `iteration`, the coordinate/submission columns on the submission tables, and
  `users.banned_at` were added after the tables were first created.
  `backend/migrations.py` adds them (and the indexes) to older databases at
  startup. Old submission rows keep `x`/`y` NULL.

## SQL

Generated from the models. Columns added by migration to an older database sit
at the end of the table instead of before `created_at`.

```sql
CREATE TABLE users (
	user_id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT, 
	username VARCHAR(12) NOT NULL, 
	uuid VARCHAR(36) NOT NULL, 
	registered_at DATETIME NOT NULL, 
	banned_at DATETIME, 
	created_at DATETIME NOT NULL
);
CREATE UNIQUE INDEX ix_users_username_lower ON users (lower(username));
CREATE UNIQUE INDEX ix_users_uuid ON users (uuid);

CREATE TABLE submitted_for_type_one (
	id INTEGER NOT NULL, 
	user_id INTEGER NOT NULL, 
	submitted_string VARCHAR(1024) NOT NULL, 
	accuracy FLOAT NOT NULL, 
	iteration INTEGER DEFAULT '0' NOT NULL, 
	x INTEGER, 
	y INTEGER, 
	submission_id VARCHAR(36), 
	reference_iteration INTEGER, 
	context_sources TEXT, 
	time_taken FLOAT, 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(user_id) REFERENCES users (user_id) ON DELETE CASCADE
);
CREATE INDEX ix_submitted_for_type_one_user_id ON submitted_for_type_one (user_id);
CREATE INDEX ix_type_one_xy_iteration ON submitted_for_type_one (x, y, iteration);
CREATE UNIQUE INDEX ux_type_one_submission ON submitted_for_type_one (user_id, submission_id);
CREATE UNIQUE INDEX ux_type_one_tile ON submitted_for_type_one (iteration, x, y);

CREATE TABLE submitted_for_type_two (
	id INTEGER NOT NULL, 
	user_id INTEGER NOT NULL, 
	submitted_string VARCHAR(1024) NOT NULL, 
	accuracy FLOAT NOT NULL, 
	iteration INTEGER DEFAULT '0' NOT NULL, 
	x INTEGER, 
	y INTEGER, 
	submission_id VARCHAR(36), 
	reference_iteration INTEGER, 
	context_sources TEXT, 
	time_taken FLOAT, 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(user_id) REFERENCES users (user_id) ON DELETE CASCADE
);
CREATE INDEX ix_submitted_for_type_two_user_id ON submitted_for_type_two (user_id);
CREATE INDEX ix_type_two_xy_iteration ON submitted_for_type_two (x, y, iteration);
CREATE UNIQUE INDEX ux_type_two_submission ON submitted_for_type_two (user_id, submission_id);
CREATE UNIQUE INDEX ux_type_two_tile ON submitted_for_type_two (iteration, x, y);

CREATE TABLE tile_holds (
	id INTEGER NOT NULL, 
	game_type INTEGER NOT NULL, 
	iteration INTEGER NOT NULL, 
	x INTEGER NOT NULL, 
	y INTEGER NOT NULL, 
	user_id INTEGER NOT NULL, 
	acquired_at DATETIME NOT NULL, 
	expires_at DATETIME NOT NULL, 
	reference_iteration INTEGER NOT NULL, 
	context_sources TEXT, 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(user_id) REFERENCES users (user_id) ON DELETE CASCADE
);
CREATE INDEX ix_tile_holds_user_id ON tile_holds (user_id);
CREATE UNIQUE INDEX ux_tile_holds_tile ON tile_holds (game_type, iteration, x, y);

CREATE TABLE game_state (
	game_type INTEGER NOT NULL, 
	state_version INTEGER NOT NULL, 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (game_type)
);

CREATE TABLE suspicious_events (
	id INTEGER NOT NULL, 
	event_type VARCHAR NOT NULL, 
	severity VARCHAR NOT NULL, 
	client_ip VARCHAR, 
	user_id INTEGER, 
	details TEXT, 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id)
);
CREATE INDEX ix_suspicious_events_client_ip ON suspicious_events (client_ip);
CREATE INDEX ix_suspicious_events_event_type ON suspicious_events (event_type);
CREATE INDEX ix_suspicious_events_user_id ON suspicious_events (user_id);

CREATE TABLE banned_ips (
	id INTEGER NOT NULL,
	ip VARCHAR NOT NULL,
	reason VARCHAR,
	created_at DATETIME NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (ip)
);

CREATE TABLE users_requests (
	id INTEGER NOT NULL, 
	method VARCHAR(10) NOT NULL, 
	path VARCHAR NOT NULL, 
	query_string TEXT, 
	client_ip VARCHAR, 
	headers TEXT, 
	request_body TEXT, 
	response_status INTEGER, 
	response_body TEXT, 
	duration_ms FLOAT, 
	token VARCHAR, 
	user_id INTEGER, 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id)
);
CREATE INDEX ix_users_requests_token ON users_requests (token);
CREATE INDEX ix_users_requests_user_id ON users_requests (user_id);

CREATE TABLE admin_requests (
	id INTEGER NOT NULL, 
	method VARCHAR(10) NOT NULL, 
	path VARCHAR NOT NULL, 
	query_string TEXT, 
	client_ip VARCHAR, 
	headers TEXT, 
	request_body TEXT, 
	response_status INTEGER, 
	response_body TEXT, 
	duration_ms FLOAT, 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id)
);

CREATE TABLE health (
	id INTEGER NOT NULL, 
	status VARCHAR NOT NULL, 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id)
);
```
