# 20Seconds — Backend Design Document

**Status:** Draft for implementation  
**Scope:** The five public user endpoints, plus the proposed `fetch_tiles` endpoint (section 10.1). Admin endpoints are excluded.  
**Existing implementation references:** OpenAPI schema supplied with the project and `database_schema.md`.

## 1. Goals and responsibilities

The backend owns identity, tile allocation, expiring holds, validation, submission persistence, iteration selection, accuracy calculation, progress aggregation, and leaderboard ranking. The frontend owns drawing and its 20-second UX timer, but cannot decide whether a submission is accepted or whether a tile is settled.

The database currently uses SQLite at `data/app.db` by default. SQLAlchemy models are the source of truth, startup creates tables, and migrations add columns to existing tables. Stored timestamps are naive UTC.

## 2. Game model

- Original image: 30 × 24 tiles, each 32 × 32 pixels (960 × 768 total).
- Tile coordinates: `x ∈ [0,29]`, `y ∈ [0,23]`, origin at top-left.
- Game types `1` and `2` have independent iteration state, holds, submissions, progress, and leaderboards.
- Iteration `0` is the original image and is always settled.
- **Iterations are unbounded.** There is no final iteration, so a free tile always exists (a new iteration can always be opened).
- **Iterations open sequentially but overlap.** Iteration `n+1` is opened only when every tile still unaccepted in all open iterations is currently held. Because a hold lasts up to 40 seconds, near the end of iteration `n` some players are still finishing `n` tiles while others already receive `n+1` tiles. With many concurrent players, or after abandoned holds, older iterations (`n-1`, `n-2`, …) can also remain open while `n` is running.
- An iteration is **open** until all 720 tile coordinates have an accepted submission; after that it is **closed**. A tile `(game_type, iteration, x, y)` is **settled** once a valid submission has been accepted for it.
- A submission at iteration `n` is judged against its **reference**: the same tile from the nearest lower iteration in which it is settled (section 5). Normally that is `n-1`, but fallback is decided per tile.
- Each iteration has 720 tile coordinates.

### Terminology (shared with the frontend document)

| Term | Meaning |
|---|---|
| `iteration` | The layer a tile assignment, submission, or settled tile belongs to. |
| `source_iteration` | In responses: the layer that supplied the pixels in that payload (the reference shown). The pixels are the settled tile of that layer, so this is also the `iteration` part of the cache key for those pixels. |
| `reference_iteration` | Persisted form of `source_iteration` for a submission or hold, so scoring is reproducible. |
| `cache_version` | One server-provided string covering image, palette symbol table, and rules versions. |
| cache key | `(game_type, iteration, x, y)` within a `cache_version`; canonical string `v{cache_version}:g{game_type}:i{iteration}:x{x}:y{y}`. `iteration` is always the tile's own layer. |

### 2.1 Palette

The API does not communicate colors as hex strings. Each pixel is one character: one symbol per palette color (12) plus one symbol for "not painted" (13 total). The hex values are for client rendering and for any server-side tooling that needs actual colors.

| Symbol | Palette name | Hex (client-side rendering only) |
|---|---|---|
| `1` | 1 | `#cdd4d0` |
| `2` | 2 | `#87a2bf` |
| `3` | 3 | `#2f4f93` |
| `4` | 4 | `#0e2948` |
| `5` | 5 | `#85934e` |
| `6` | 6 | `#5a732b` |
| `7` | 7 | `#27412d` |
| `8` | 8 | `#0e1612` |
| `9` | 9 | `#c4b257` |
| `a` | 10 | `#ba9511` |
| `b` | 11 | `#6c392c` |
| `c` | 12 | `#2b1f12` |
| `.` | unpainted (`null`) | none — not a color |

`.` is allowed only as a drawing/submission pixel meaning "not painted". It is not a thirteenth color. Before persistence and scoring, replace every `.` with `1` (`#cdd4d0`). The server never emits `.` in responses.

*The symbol assignment is proposed; any 13 distinct JSON-safe characters work if the frontend uses the same table. Version the table (palette version) and tie it to the cache-versioning decision in section 14.*

## 3. API pixel format

A canvas is a **string of exactly 1,024 characters**, row-major, over the 13-symbol alphabet in section 2.1:
- character at index `py * 32 + px` identifies pixel `(px, py)`;
- reject strings with a different length or any character outside the alphabet;
- context regions and reference tiles use the same encoding, with length `width * height`.

This matches the existing OpenAPI schema, which already defines `SubmitIn.canvas` as a 1,024-character string, so the earlier schema conflict no longer exists. Persist the normalized canvas (all `.` replaced by `1`) as-is: it fits the current `submitted_string VARCHAR(1024)` column, so no column change is needed for the pixel format. Hex strings are never sent, received, or stored.

## 4. `request_square`: assignment and hold

### Request
`POST /api/user/request_square`
```json
{ "token": "uuid", "game_type": 1 }
```

### Proposed success response
```json
{
  "success": true,
  "x": 4,
  "y": 7,
  "game_type": 1,
  "iteration": 3,
  "level_information": {
    "kind": "canvas",
    "width": 32,
    "height": 32,
    "pixels": "1111…(1,024 characters)",
    "source_iteration": 2
  },
  "error": null
}
```
The pixel string is abbreviated in examples and must be the full length (1,024 characters for a tile; `width * height` for a context region). `source_iteration` is the actual layer used as reference, not necessarily `iteration - 1`.

### Allocation algorithm

1. Resolve `token` to a registered user. Reject unknown tokens (`INVALID_TOKEN`) and banned users (`USER_BANNED`).
2. Validate `game_type` is exactly `1` or `2`.
3. Apply the hold limit (section 4.1).
4. Choose the iteration: the **lowest open iteration** for that game type that still has a free tile (not accepted, not under an unexpired hold). If no open iteration has a free tile, open the next iteration (highest opened + 1). Preferring the lowest open iteration lets older layers finish first and re-serves tiles whose holds expired.
5. Within the chosen iteration, take the next coordinate from its shuffled candidate order.
6. Resolve the reference for this assignment (section 5) and, in one atomic operation, create the hold for `(game_type, iteration, x, y)` for this user, storing the reference metadata with it.
7. Return the coordinate, iteration, and level information.

There is no "no tile available" outcome: iterations are unbounded, so step 4 can always open a new iteration with 720 free tiles. Any failure to allocate is an `INTERNAL_ERROR`.

The coordinate order can be held in memory for efficient sampling, but persistent submission and hold records are the source of truth. Allocation and hold acquisition must be atomic so two concurrent requests never receive the same tile.

### 4.1 Hold lifetime, tracking, and limit

A hold expires 40 seconds after acquisition unless the user successfully submits that same tile for the same game type and iteration. A successful submission consumes the hold. When a hold expires the tile becomes free again **in the same iteration** and is eligible for allocation (lowest open iteration first).

**Track holds per open iteration, not just for the newest one.** A hold on an older iteration stays valid after newer iterations have started, and the submission must still be accepted against it. Candidate state (free tiles and holds) for an iteration is kept until the iteration is closed (all 720 tiles accepted); only then may that iteration's hold records and candidate state be discarded.

The current database has no hold table. Add a persistent `tile_holds` table (section 8.2). A process-local dictionary alone is insufficient with multiple workers and loses active holds on restart.

**Ban rule.** Count the user's unexpired holds across both game types. If a `request_square` arrives when the user already has 5 active holds (so a new one would make it more than 5), the user is banned immediately: set `users.banned_at`, release all of their holds, and return `USER_BANNED` (HTTP 403) without creating a hold. A banned token receives `USER_BANNED` on every user endpoint.

### 4.2 Level information — game type 1

Return the 32×32 reference tile from the selected source iteration:
```json
{
  "kind": "canvas",
  "width": 32,
  "height": 32,
  "pixels": "1111…(1,024 characters)",
  "source_iteration": 2
}
```
The `pixels` string contains exactly 1,024 characters, row-major, and never contains `.`. The example is abbreviated.

### 4.3 Level information — game type 2

The player sees a **64×64 pixel window** of the image that contains the empty 32×32 target tile. The server returns the exact geometry so the client does not reproduce this logic.

**Window rule.** For target tile `(x, y)`:
```
wx = clamp(32*x - 16, 0, 960 - 64)
wy = clamp(32*y - 16, 0, 768 - 64)
window = [wx, wx+64) × [wy, wy+64)      (image pixel coordinates)
context = window minus the target tile (32*x, 32*y, 32, 32)
```
The window is never shrunk: when the target is near an image edge it is shifted inward, so the player sees more context on the side facing the image interior. The context always contains exactly 64×64 − 32×32 = **3,072 pixels**.

| Target tile | Window `(x, y, w, h)` | Context pieces |
|---|---|---|
| Middle `(4,7)` | `(112, 208, 64, 64)` | 8 pieces: four 16×16 corners, two 32×16 (above, below), two 16×32 (left, right) |
| Left edge `(0,7)` | `(0, 208, 64, 64)` | 5 pieces: 32×16 above, 32×16 below, full 32×32 right neighbour, 32×16 above-right, 32×16 below-right |
| Corner `(0,0)` | `(0, 0, 64, 64)` | 3 pieces, all three neighbours shown in full: right, below, below-right (each 32×32) |

The same shifting applies at the right edge (`x = 29`), top edge (`y = 0`), and bottom edge (`y = 23`), and to the other three corners.

**Decomposition.** Cut the context along tile boundaries (multiples of 32 in image coordinates) so that every region lies inside a single source tile and has a single `source_iteration`. Each region carries its own `source_iteration`, which may differ between regions.

Example for target `(4, 7)` (abbreviated; the full response has 8 regions):
```json
{
  "kind": "context",
  "target": { "x": 128, "y": 224, "width": 32, "height": 32 },
  "window": { "x": 112, "y": 208, "width": 64, "height": 64 },
  "regions": [
    { "x": 112, "y": 208, "width": 16, "height": 16, "pixels": "22…(256 characters)", "source_iteration": 2 },
    { "x": 128, "y": 208, "width": 32, "height": 16, "pixels": "33…(512 characters)", "source_iteration": 1 }
  ]
}
```
Coordinates are in full-image pixel coordinates. Each region's `pixels` is a row-major string whose length equals `width * height` and which never contains `.`. Return only context outside the target tile; never send the target's reference pixels.

## 5. Reference selection and fallback

For a tile at iteration `n`, the reference is the same tile from the **nearest lower iteration in which it is settled**: walk down from `n-1` and use the first layer where that tile is settled. Iteration `0` is always settled, so a reference always exists. Fallback is **per tile**: never fall back an entire image or context window.

- Game type 1: the target tile's reference is chosen this way; `source_iteration` is that layer.
- Game type 2: every tile that a context region touches is resolved independently. One response can contain regions from different source iterations. The target tile's own reference (used for scoring) is resolved the same way.
- Normally the reference is `n-1`. Fallback happens near the end of an iteration (the `n-1` tile is still held by another player) and, under heavy load, can reach several layers down.

**Freeze the reference at allocation time.** Store it with the hold: `reference_iteration` for the target tile and, for game type 2, the per-region `source_iteration` list. If the lower layer's tile settles later, the in-flight assignment keeps its original reference, so what the player saw is what is scored.

For scoring a submitted tile at iteration `n`, compare the normalized submission against the exact reference pixels shown to the player (the stored reference). Do not compare iteration `n` directly with iteration `0`, and do not recompute accuracy against a different layer than the one displayed. Persist enough metadata to reproduce the score (section 8.1).

## 6. `submit_result`: validation and persistence

### Request
`POST /api/user/submit_result`

Fields: `token`, `x`, `y`, `canvas` (symbol string, section 3), `time_taken`, `game_type`, `iteration`, and **`submission_id`** (client-generated UUID, new). The client creates one `submission_id` per assignment and reuses it on every retry of that submission.

### Validation sequence

1. Resolve the user token. Reject banned users (`USER_BANNED`).
2. **Idempotency check:** if a submission with the same `(user, submission_id)` already exists, return that submission's original success response (HTTP 200) and do nothing else. If the stored submission has a different game type, iteration, `x`, or `y`, reject with `SUBMISSION_ID_REUSED`. This lets a client safely retry after a lost response, even though the hold has been consumed.
3. Validate `submission_id` is a UUID (`INVALID_SUBMISSION_ID`), game type, tile coordinates, non-negative iteration, and canvas (string of exactly 1,024 characters, all within the 13-symbol alphabet).
4. If a different submission already exists for `(game_type, iteration, x, y)`, reject with `TILE_ALREADY_SUBMITTED`.
5. Find an active, unexpired hold matching **all** of user, game type, iteration, `x`, and `y`. This works for any open iteration, not only the newest. If there is none, reject (`HOLD_NOT_FOUND` / `HOLD_EXPIRED`).
6. Count painted pixels (characters other than `.`). Reject if coverage is **below 60%**. For 1,024 pixels, 615 painted pixels is the minimum accepted count because `ceil(0.60 × 1024) = 615`; 614 is rejected.
7. Replace every `.` with `1` (`#cdd4d0`).
8. Calculate accuracy as `matching_pixels / 1024` against the **reference stored on the hold**. Accuracy is a ratio in `[0,1]`; multiply by 100 only for presentation.
9. In one transaction, persist the normalized canvas, accuracy, iteration, coordinate, game type, user, `submission_id`, reference metadata, and submission time; consume the hold; if this acceptance brings the iteration to 720 accepted tiles, mark the iteration closed; increment `state_version`; update any leaderboard/progress aggregates or mark them dirty.
10. Return success only after the transaction commits.

### Response

```json
{
  "success": true,
  "x": 4,
  "y": 7,
  "game_type": 1,
  "iteration": 3,
  "accuracy": 0.713,
  "normalized_canvas": "1111…(1,024 characters)",
  "error": null
}
```
`normalized_canvas` is the server-confirmed settled tile (all `.` replaced), which the client may cache under `(game_type, iteration, x, y)`. A replay of an accepted `submission_id` returns this same body.

## 7. Restart recovery and iteration state

On startup, reconstruct iteration state independently for game types 1 and 2 from persisted submissions and holds:

1. For each game type, group accepted submissions by `(iteration, x, y)`. Duplicate coordinate records must not inflate counts.
2. An iteration is **open** if it is at or below the highest iteration that has any accepted submission or hold, and has fewer than 720 accepted coordinates. There can be several open iterations. Iterations above the highest known one are opened lazily by the allocation algorithm.
3. For each open iteration, rebuild the candidate stack from coordinates that are neither accepted nor under an unexpired hold.
4. Restore holds from `tile_holds` using the persisted `expires_at`; expired holds free their tile. Keep restored holds (with their stored reference metadata) for every open iteration.
5. Ban state is read from `users.banned_at`.

Do not use an in-memory counter as the only source of truth. The current submission tables do not store `x` and `y`, so they are insufficient to determine which coordinates are filled. **Add `x` and `y` columns** to both submission tables (or create a normalized submissions table with equivalent uniqueness constraints) before implementing restart recovery. Existing historical rows without coordinates cannot be reconstructed reliably; define a one-time migration/backfill policy for them.

## 8. Database changes

The current database has:
- `users` with a case-insensitive unique username and UUID token;
- `submitted_for_type_one` and `submitted_for_type_two` with `user_id`, `submitted_string`, `accuracy`, `iteration`, `created_at`;
- request logs and health tables, which are outside this feature's game-state logic.

Recommended minimum changes:

### 8.1 Submission tables

Add:
- `x INTEGER NOT NULL`, `y INTEGER NOT NULL` (coordinate);
- `submission_id` (UUID text) with a uniqueness constraint on `(user_id, submission_id)`;
- `reference_iteration INTEGER` (the layer the target tile was scored against);
- for game type 2, `context_sources` (JSON listing each region's `source_iteration`), if needed to reproduce what was shown;
- optional `time_taken FLOAT`;
- optional `accepted_at` if distinct from `created_at` is useful.

`submitted_string` stores the normalized 1,024-character symbol string and fits the existing `VARCHAR(1024)` column.

Add a uniqueness constraint on `(iteration, x, y)` within each game type table, so a tile is only accepted once per iteration. Add indexes supporting `(iteration, y, x)`, `user_id`, and leaderboard aggregation. For historical records, nullable columns during migration may be necessary before the new constraints can be enforced.

Consider consolidating both tables into one `submissions` table with a `game_type` column. Keeping two tables is also valid and aligns with the existing schema; if kept separate, ensure all logic explicitly selects the correct table.

### 8.2 Holds

Add `tile_holds` with at least:
- `game_type`, `iteration`, `x`, `y`, `user_id`;
- `acquired_at`, `expires_at`;
- `reference_iteration` and, for game type 2, `context_sources` (frozen at allocation, section 5);
- uniqueness on `(game_type, iteration, x, y)` so only one active hold exists per tile and iteration.

Make hold expiry and acquisition transactional. Expired rows may be overwritten by the next allocation of the same tile. Rows of an open iteration must not be dropped wholesale; an iteration's hold records and candidate state may be discarded only once the iteration is closed.

### 8.3 Leaderboard cache

A leaderboard table is optional initially. Start with indexed aggregate queries if performance is adequate. If query cost becomes significant, add a derived `leaderboard_stats` table keyed by `(game_type, user_id)` containing contribution count, aggregate accuracy, and update timestamp. It is a cache, not the source of truth; rebuild it from accepted submissions and update it transactionally or mark it dirty after submissions.

### 8.4 Progress/cache metadata

Consider a `game_state` table keyed by `game_type` for state version and the `cache_version` string. It can speed reads, but open iterations must be reconcilable from submission and hold records.

### 8.5 Users

Add `banned_at DATETIME NULL` (and optionally a short `ban_reason`) to `users`. Include it in every token resolution.

## 9. `request_leaderboard`

### Request
```json
{ "token": "uuid", "game_type": 1 }
```

### Rules
- Separate leaderboard for game type 1 and game type 2.
- A player qualifies only with at least **10 accepted contributions in that game type**.
- Return top 10 by contribution count and top 10 by accuracy, plus one additional row for the requesting player if they qualify but are not already represented in that list. Thus each list may contain up to 11 rows.
- A contribution in game type 1 does not count toward game type 2's threshold.
- Define tie-breaking deterministically, e.g. primary metric descending, then username case-insensitively ascending, then `user_id` ascending.
- Define accuracy leaderboard metric explicitly. Recommended: mean per-submission accuracy for that game type, calculated from accepted submissions; avoid using a raw sum because users with more submissions would be favored.
- For accuracy leaderboard, include only users meeting the 10-contribution threshold.
- If the requesting user has fewer than 10 contributions, do not append them as a special row.

The existing schema returns one `leaderboard` array. The requirements call for two lists. **Revise the response** to:
```json
{
  "success": true,
  "top_contributors": [],
  "top_accuracy": [],
  "error": null
}
```
Each entry should include `rank`, `username`, `count`, and `accuracy`; optionally add `is_current_user` so the frontend can highlight the appended personal row. Keep ranking semantics stable when the current player is outside the top 10.

## 10. `fetch_current_progress` — proposed redesign

The existing `ProgressEntry` shape (`accuracy_to_original`, `accuracy_to_prev_iteration`, `progress_percentage`, `current_image`, `your_contribution`) mixes global, per-iteration, and user-specific data and sends an image string. Replace it with a compact state response. It carries status only; tile pixels come from `fetch_tiles` (section 10.1).

### Request
Keep the existing request shape:
```json
{ "token": "uuid", "game_type": 1 }
```

### Proposed response
```json
{
  "success": true,
  "game_type": 1,
  "state_version": 42,
  "cache_version": "1",
  "grid": { "columns": 30, "rows": 24, "tile_size": 32 },
  "open_iterations": [
    { "iteration": 2, "filled_tiles": 719, "total_tiles": 720 },
    { "iteration": 3, "filled_tiles": 12, "total_tiles": 720 }
  ],
  "latest_iteration": [3, 3, 2, 0, "…720 integers…"],
  "player": { "contribution_count": 14, "mean_accuracy": 0.713 },
  "error": null
}
```

- `latest_iteration` is a flat row-major array of exactly 720 integers (`index = y * 30 + x`). Each value is the highest iteration in which that tile is settled; `0` means only the original image exists. The client builds the current image by taking, for each tile, `(game_type, latest_iteration[i], x, y)` from its cache or from `fetch_tiles` (iteration `0` comes from the bundled image).
- `open_iterations` lists every open iteration with its filled count. Overall progress for an iteration is `filled_tiles / total_tiles`; the highest open iteration is the frontier.
- `state_version` increments whenever a submission changes visible game state, so clients can cheaply tell whether to refresh. Consider supporting `since_version` later.
- `cache_version` identifies the image, palette symbol table, and rules versions. When it changes, clients must drop cached tiles.
- Do not include the original image or tile pixels.

For game type 2, `latest_iteration` is only a status map. The client must not reconstruct server-selected context from it; it uses `request_square`'s returned regions and source metadata.

### 10.1 `fetch_tiles` — proposed new endpoint

Returns the pixels of settled tiles so clients can render the current image and fill their cache.

`POST /api/user/fetch_tiles`
```json
{
  "token": "uuid",
  "game_type": 1,
  "tiles": [ { "iteration": 2, "x": 0, "y": 0 }, { "iteration": 3, "x": 1, "y": 0 } ]
}
```
Response:
```json
{
  "success": true,
  "game_type": 1,
  "cache_version": "1",
  "tiles": [ { "iteration": 2, "x": 0, "y": 0, "pixels": "1111…(1,024 characters)" } ],
  "missing": [ { "iteration": 3, "x": 1, "y": 0 } ],
  "error": null
}
```
- Only settled tiles are returned; a requested tile that is not settled (or is iteration `0`, which the client bundles) goes in `missing`.
- At most 100 tiles per request; more returns `TOO_MANY_TILES`.
- Settled tiles are immutable within a `cache_version`, so responses can carry long-lived HTTP cache headers.
- Banned users are rejected like on every other endpoint.

## 11. Endpoint summary

| Endpoint | Responsibility |
|---|---|
| `POST /api/user/register` | Create a user and issue UUID token |
| `POST /api/user/request_square` | Atomically assign a tile (lowest open iteration with a free tile), create a 40-second hold, return reference/context pixels, enforce the hold limit |
| `POST /api/user/submit_result` | Idempotently validate hold and canvas, normalize `.`, calculate accuracy, persist submission, consume hold, return the normalized canvas |
| `POST /api/user/request_leaderboard` | Return per-mode contribution and accuracy rankings, with personal row when eligible |
| `POST /api/user/fetch_current_progress` | Return open iterations, player stats, per-tile latest settled iteration, and version metadata |
| `POST /api/user/fetch_tiles` | Return pixels for requested settled tiles (proposed) |

## 12. Error semantics

Keep the current `{success, error}` pattern if desired, but make errors machine-readable. Recommended error codes include:
- `INVALID_TOKEN`
- `USER_BANNED` (HTTP 403; terminal for that token)
- `INVALID_GAME_TYPE`
- `INVALID_COORDINATE`
- `HOLD_NOT_FOUND`
- `HOLD_EXPIRED`
- `CANVAS_INVALID`
- `INSUFFICIENT_COVERAGE`
- `INVALID_SUBMISSION_ID`
- `SUBMISSION_ID_REUSED`
- `TILE_ALREADY_SUBMITTED`
- `TOO_MANY_TILES`
- `INTERNAL_ERROR`

There is no `NO_TILE_AVAILABLE` code (section 4). Return a suitable HTTP status as well as the JSON error. Do not report `success: true` for a rejected submission. Ensure request logging does not accidentally expose tokens to public logs; the current `users_requests` table deliberately stores request tokens, so access to it should remain restricted and retention should be considered.

## 13. Acceptance criteria

- Tile allocation is unique under concurrent requests and holds expire after 40 seconds.
- Allocation picks the lowest open iteration with a free tile and opens the next iteration only when none has one.
- Holds are tracked for every open iteration; a submission against an older open iteration is accepted if its hold is valid.
- An iteration's hold records and candidate state are discarded only after all 720 tiles of that iteration are accepted.
- A submission is accepted only with a valid hold for the same user, mode, iteration, and coordinate.
- Replaying an accepted `submission_id` returns the original success response and creates no duplicate.
- A user with more than 5 active holds is banned immediately and receives `USER_BANNED` afterwards.
- Canvas validation accepts only a 1,024-character string over the 12 palette symbols plus `.`; fewer than 615 painted pixels is rejected.
- Accepted canvases have every `.` normalized to `1` (`#cdd4d0`). No endpoint sends or accepts hex strings.
- Reference selection uses the nearest lower settled layer per tile, is frozen on the hold, and accuracy is computed against it, never directly against iteration 0.
- Game type 2 responses contain a 64×64 window, a target rectangle, and regions totaling exactly 3,072 context pixels, with edge/corner shifting as in section 4.3.
- Both modes have independent iteration state, progress, and leaderboards.
- Restart recovery can reconstruct open iterations, filled coordinates, candidate stacks, and holds from persistent data.
- Progress responses avoid transmitting the original image or tile pixels; `fetch_tiles` serves settled tiles.
- Leaderboard results implement the two-list / top-10-plus-current-player requirement.
- All database migrations are safe for existing SQLite databases, and historical rows without coordinates have an explicit migration policy.

## 14. Open decisions to resolve before implementation

Resolved: canvas wire format (section 3), game type 2 geometry (section 4.3), iteration overlap and hold tracking (sections 2, 4.1), fallback depth (section 5), cache key and versioning (terminology table, section 10).

1. **Symbol table:** confirm the exact 13-symbol assignment with the frontend (section 2.1).
2. **Fallback wording:** this draft walks down from `n-1` to the first layer where the tile is settled. Confirm this is the intended meaning of "earliest available layer".
3. **Hold-limit false positives:** abandoned rounds keep a hold alive for up to 40 seconds. A player who abandons rounds quickly, especially while switching between modes, could reach the 5-hold limit without abusing anything. Consider releasing a user's previous hold for the same game type when they request a new square, which caps normal players at 2 holds.
4. **Ban consequences:** are banned users' accepted submissions kept, and are they excluded from leaderboards? Can an administrator lift a ban? Re-registering with a new username creates a fresh token, so consider registration rate limiting.
5. **Accuracy for game type 2:** confirm that accuracy is measured against the hidden target tile from the selected reference layer, not against surrounding context pixels.
6. **Leaderboard accuracy:** confirm mean accuracy across all accepted submissions, and whether accuracy is stored as a ratio or percentage. This draft recommends ratio `[0,1]`.
7. **Submission identity:** existing tables lack `x` and `y`; adding them is required for correct tile completion and restart reconstruction.
8. **`cache_version` source:** define how the version string is stored and bumped (image, palette table, or rules change).
