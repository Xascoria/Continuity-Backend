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
| `users_requests` | Log of every non-admin `/api/*` request (tokens kept) |
| `admin_requests` | Log of every `/api/admin/*` request (user tokens redacted) |
| `health` | Rows written by the dashboard's health check |

```
users 1 ──< submitted_for_type_one   (user_id, ON DELETE CASCADE)
users 1 ──< submitted_for_type_two   (user_id, ON DELETE CASCADE)
users_requests.user_id  -> users.user_id   (logical link only, no foreign key)
```

## `users`

| Column | Type | Notes |
|---|---|---|
| `user_id` | INTEGER | Primary key, autoincrement. Ids of deleted users are never reused. |
| `username` | VARCHAR(25) | Not null. Unique case-insensitively (`ix_users_username_lower` on `lower(username)`). |
| `uuid` | VARCHAR(36) | Not null, unique. The user's token, issued at registration. |
| `registered_at` | DATETIME | Not null, set at registration. |
| `created_at` | DATETIME | Not null. |

## `submitted_for_type_one` / `submitted_for_type_two`

Both tables have the same columns.

| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER | Primary key. |
| `user_id` | INTEGER | Not null, indexed. Foreign key to `users.user_id`, `ON DELETE CASCADE` (deleting a user deletes their submissions). |
| `submitted_string` | VARCHAR(1024) | Not null. The submitted canvas. |
| `accuracy` | FLOAT | Not null. Currently always `0.0` (calculation not defined yet, see `backend/game.py`). |
| `iteration` | INTEGER | Not null, default `0`. Sent by the client in `submit_result`. |
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
| `ix_users_requests_token` | `users_requests` | `token` | no |
| `ix_users_requests_user_id` | `users_requests` | `user_id` | no |

## Notes

- An older database may still contain a leftover `requests` table from before
  the request log was split in two. Nothing uses it and it can be dropped
  (`DROP TABLE requests;`).
- `iteration` on the submission tables was added after they were first
  created. `backend/migrations.py` adds it to older databases at startup.

## SQL

Generated from the models. Columns added by migration to an older database sit
at the end of the table instead of before `created_at`.

```sql
CREATE TABLE users (
	user_id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
	username VARCHAR(25) NOT NULL,
	uuid VARCHAR(36) NOT NULL,
	registered_at DATETIME NOT NULL,
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
	created_at DATETIME NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(user_id) REFERENCES users (user_id) ON DELETE CASCADE
);
CREATE INDEX ix_submitted_for_type_one_user_id ON submitted_for_type_one (user_id);

CREATE TABLE submitted_for_type_two (
	id INTEGER NOT NULL,
	user_id INTEGER NOT NULL,
	submitted_string VARCHAR(1024) NOT NULL,
	accuracy FLOAT NOT NULL,
	iteration INTEGER DEFAULT '0' NOT NULL,
	created_at DATETIME NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(user_id) REFERENCES users (user_id) ON DELETE CASCADE
);
CREATE INDEX ix_submitted_for_type_two_user_id ON submitted_for_type_two (user_id);

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
