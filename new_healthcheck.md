# User health check

`POST /api/user/health`

Public-style call on the user API. It does not assign a tile, take a hold, or change game state. It returns one already accepted **game type 1** square so the client can show a live sample.

## Request

`Content-Type: application/json`

```json
{ "token": "" }
```

| Field | Type | Required | Notes |
|---|---|---|---|
| `token` | string | no | The player's uuid from `POST /api/user/register`. Omit the field, send `""`, or send only whitespace to call with no user. |

Send the real token whenever the client has one.

A non-empty token is checked the same way as the other user endpoints:

| HTTP | `error` | When |
|---|---|---|
| 401 | `INVALID_TOKEN` | Token is non-empty and matches no user. |
| 403 | `USER_BANNED` | Token belongs to a banned user. |

Those two cases do not return a square.

### Anonymous pings

Calls with no token are allowed. The same IP sending more than **10** of them in **60 seconds** is recorded as suspicious event `ANONYMOUS_HEALTH_PING` (warning) on the admin dashboard. The check still returns its normal body; nothing is blocked. Calls that include a real token do not count toward that limit.

## Success

HTTP 200

```json
{
  "success": true,
  "square": "1111…(1024 characters)",
  "username": "Painter",
  "year": 2026,
  "error": null
}
```

| Field | Meaning |
|---|---|
| `square` | One randomly chosen accepted submission from game type 1. Normalized canvas: exactly 1024 characters, row-major, 32×32. Alphabet is `1-9` and `a-c` (same palette as `fetch_tiles` / `backend_design.md` section 2.1). `.` is never present. |
| `username` | Username of the player who submitted that square. |
| `year` | Calendar year of that submission's timestamp, UTC. Taken from the stored timestamp (expected to be 2026 for current play, but it is not hardcoded). |

## No submissions yet

HTTP 404, when game type 1 has no accepted squares:

```json
{
  "success": false,
  "square": null,
  "username": null,
  "year": null,
  "error": "NO_SUBMISSION"
}
```

## Example

```http
POST /api/user/health
Content-Type: application/json

{"token": "8f3c2a10-1111-4222-8333-444455556666"}
```

Anonymous:

```http
POST /api/user/health
Content-Type: application/json

{"token": ""}
```
