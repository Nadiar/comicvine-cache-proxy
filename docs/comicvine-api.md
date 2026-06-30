# ComicVine API Documentation

Source: <https://comicvine.gamespot.com/api/documentation>

---

## Overview

The ComicVine API provides programmatic access to the ComicVine database of comics, volumes, issues, publishers, characters, story arcs, and more. All requests require a valid API key.

**Base URL:** `https://comicvine.gamespot.com/api/`

---

## Authentication

All requests must include an `api_key` query parameter.

```
https://comicvine.gamespot.com/api/volumes/?api_key=YOUR_KEY&format=json
```

API keys are free and can be obtained by creating an account at <https://comicvine.gamespot.com>.

---

## Rate Limiting

- **Per-minute limit:** 200 requests per minute
- **Per-hour limit:** 500 requests per hour (per endpoint per API key)

Exceeding the limit returns a response with `status_code: 107` and `error: "Rate Limit Exceeded"`. The limit resets at the top of each clock hour.

---

## Response Format

### Supported Formats

Specify via the `format` query parameter:

| Value  | Content-Type               |
|--------|----------------------------|
| `json` | `application/json` (default) |
| `xml`  | `application/xml`           |
| `jsonp`| with `json_callback` param  |

### Envelope

Every response is wrapped in a standard envelope:

```json
{
  "status_code": 1,
  "error": "OK",
  "number_of_total_results": 150,
  "number_of_page_results": 10,
  "limit": 10,
  "offset": 0,
  "results": [ ... ]
}
```

### Status Codes

| Code | Meaning |
|------|---------|
| 1    | OK |
| 100  | Invalid API Key |
| 101  | Object Not Found |
| 102  | Error in URL Format |
| 103  | `jsonp` format requires a `json_callback` argument |
| 104  | Filter Error |
| 105  | Subscriber only video is for subscribers only |
| 107  | Rate Limit Exceeded |

---

## Common Query Parameters

These parameters apply to all list endpoints.

| Parameter    | Description |
|--------------|-------------|
| `api_key`    | Required. Your API key. |
| `format`     | Response format: `json` (default), `xml`, `jsonp`. |
| `field_list` | Comma-separated list of fields to return. Reduces payload size. |
| `limit`      | Number of results per page. Max `100`. Default `100`. |
| `offset`     | Zero-based offset into the result set for pagination. |
| `filter`     | Comma-separated `field:value` pairs to filter results (see [Filtering](#filtering)). |
| `sort`       | Sort expression: `field:asc` or `field:desc` (see [Sorting](#sorting)). |

---

## Filtering

Filters are expressed as `field:value` pairs separated by commas. Multiple filters are ANDed together.

```
filter=name:Batman,publisher:10,start_year:2011
```

**Date range filter** (for date fields):

```
filter=cover_date:2024-01-01|2024-12-31
filter=store_date:2024-06-01|2024-06-30
filter=date_last_updated:2026-01-01 00:00:00|2026-01-02 00:00:00
```

**ID list filter:**

```
filter=id:12345|67890|11111
```

---

## Sorting

```
sort=name:asc
sort=cover_date:desc
sort=date_last_updated:asc
```

Only one sort field is supported per request.

---

## Pagination

Use `limit` and `offset` together to page through results.

```
/api/issues/?api_key=KEY&format=json&limit=100&offset=0   # page 1
/api/issues/?api_key=KEY&format=json&limit=100&offset=100 # page 2
```

`number_of_total_results` in the response tells you the total count.

---

## Resources

### Volume

A comic book series or run.

#### Detail — `GET /api/volume/{id}/`

```
GET /api/volume/4050-160294/?api_key=KEY&format=json
```

**CV type prefix:** `4050-`

#### List — `GET /api/volumes/`

```
GET /api/volumes/?api_key=KEY&format=json&filter=name:Batman&limit=10
```

#### Fields

| Field | Type | Description |
|-------|------|-------------|
| `id` | integer | Unique CV identifier |
| `name` | string | Series title |
| `aliases` | string | Newline-separated list of alternate names |
| `api_detail_url` | string | URL to this resource via the API |
| `site_detail_url` | string | URL to this resource on the website |
| `count_of_issues` | integer | Total number of issues in the series |
| `start_year` | string | Year the series began |
| `publisher` | object | `{id, name, api_detail_url}` |
| `image` | object | Image URLs (see [Image Object](#image-object)) |
| `deck` | string | Short summary |
| `description` | string | Full HTML description |
| `date_added` | datetime | When the record was added to CV |
| `date_last_updated` | datetime | When the record was last updated |
| `first_issue` | object | `{id, issue_number, name, api_detail_url}` |
| `last_issue` | object | `{id, issue_number, name, api_detail_url}` |
| `issues` | list | List of issues (detail only) |
| `characters` | list | Characters appearing in this series |
| `concepts` | list | Concepts covered |
| `locations` | list | Locations featured |
| `objects` | list | Objects featured |
| `people` | list | Creators involved |
| `story_arcs` | list | Story arcs |
| `teams` | list | Teams appearing |

---

### Issue

A single comic book issue.

#### Detail — `GET /api/issue/{id}/`

```
GET /api/issue/4000-1073108/?api_key=KEY&format=json
```

**CV type prefix:** `4000-`

#### List — `GET /api/issues/`

```
GET /api/issues/?api_key=KEY&format=json&filter=volume:160294
```

#### Fields

| Field | Type | Description |
|-------|------|-------------|
| `id` | integer | Unique CV identifier |
| `name` | string | Issue title (may be null) |
| `issue_number` | string | Issue number within the series |
| `volume` | object | Parent volume `{id, name, api_detail_url}` |
| `cover_date` | date | Official cover date (`YYYY-MM-DD`) |
| `store_date` | date | Actual in-store release date (`YYYY-MM-DD`) |
| `aliases` | string | Alternate names |
| `api_detail_url` | string | URL to this resource via the API |
| `site_detail_url` | string | URL to this resource on the website |
| `image` | object | Image URLs (see [Image Object](#image-object)) |
| `associated_images` | list | Additional images |
| `deck` | string | Short summary |
| `description` | string | Full HTML description |
| `has_staff_review` | boolean | Whether there is a staff review |
| `date_added` | datetime | When the record was added to CV |
| `date_last_updated` | datetime | When the record was last updated |
| `character_credits` | list | Characters appearing (detail only) |
| `character_died_in` | list | Characters who die in this issue (detail only) |
| `concept_credits` | list | Concepts covered (detail only) |
| `location_credits` | list | Locations featured (detail only) |
| `object_credits` | list | Objects featured (detail only) |
| `person_credits` | list | Creators credited (detail only). Each entry has `name` and `role`; `role` is a **comma-separated string** of roles (e.g. `"writer,editor"`) — split on `,` to get individual roles. |
| `story_arc_credits` | list | Story arcs this issue belongs to (detail only) |
| `team_credits` | list | Teams appearing (detail only) |
| `team_disbanded_in` | list | Teams disbanded in this issue (detail only) |
| `first_appearance_characters` | list | Characters with first appearance here (detail only) |
| `first_appearance_concepts` | list | Concepts with first appearance here (detail only) |
| `first_appearance_locations` | list | Locations with first appearance here (detail only) |
| `first_appearance_objects` | list | Objects with first appearance here (detail only) |
| `first_appearance_storyarcs` | list | Story arcs introduced here (detail only) |
| `first_appearance_teams` | list | Teams introduced here (detail only) |

---

### Publisher

A comic book publisher.

#### Detail — `GET /api/publisher/{id}/`

```
GET /api/publisher/4010-10/?api_key=KEY&format=json
```

**CV type prefix:** `4010-`

#### List — `GET /api/publishers/`

```
GET /api/publishers/?api_key=KEY&format=json&filter=name:Marvel
```

#### Fields

| Field | Type | Description |
|-------|------|-------------|
| `id` | integer | Unique CV identifier |
| `name` | string | Publisher name |
| `aliases` | string | Alternate names |
| `api_detail_url` | string | URL to this resource via the API |
| `site_detail_url` | string | URL to this resource on the website |
| `image` | object | Image URLs (see [Image Object](#image-object)) |
| `deck` | string | Short summary |
| `description` | string | Full HTML description |
| `location_address` | string | Street address |
| `location_city` | string | City |
| `location_state` | string | State/province |
| `date_added` | datetime | When the record was added to CV |
| `date_last_updated` | datetime | When the record was last updated |
| `characters` | list | Characters from this publisher (detail only) |
| `story_arcs` | list | Story arcs from this publisher (detail only) |
| `teams` | list | Teams from this publisher (detail only) |
| `volumes` | list | Volumes published (detail only) |

---

### Character

A character appearing in comics.

#### Detail — `GET /api/character/{id}/`

```
GET /api/character/4005-1021/?api_key=KEY&format=json
```

**CV type prefix:** `4005-`

#### List — `GET /api/characters/`

```
GET /api/characters/?api_key=KEY&format=json&filter=name:Batman
```

#### Fields

| Field | Type | Description |
|-------|------|-------------|
| `id` | integer | Unique CV identifier |
| `name` | string | Character name |
| `aliases` | string | Alternate names/identities |
| `api_detail_url` | string | API URL |
| `site_detail_url` | string | Website URL |
| `image` | object | Image URLs |
| `deck` | string | Short summary |
| `description` | string | Full HTML description |
| `real_name` | string | Real/secret identity name |
| `gender` | integer | `1` = Male, `2` = Female, `3` = Other |
| `birth` | date | Date of birth |
| `publisher` | object | Publisher reference |
| `origin` | object | Origin type (e.g. "Mutation", "Human") |
| `date_added` | datetime | Record creation date |
| `date_last_updated` | datetime | Last update date |
| `first_appeared_in_issue` | object | First appearance issue (detail only) |
| `creators` | list | Creators who created this character (detail only) |
| `enemies` | list | Enemy characters (detail only) |
| `friends` | list | Ally characters (detail only) |
| `issue_credits` | list | Issues this character appears in (detail only) |
| `issues_died_in` | list | Issues where this character dies (detail only) |
| `movies` | list | Movie appearances (detail only) |
| `powers` | list | Powers/abilities (detail only) |
| `story_arc_credits` | list | Story arcs (detail only) |
| `team_enemies` | list | Enemy teams (detail only) |
| `team_friends` | list | Allied teams (detail only) |
| `teams` | list | Teams the character belongs to (detail only) |
| `volume_credits` | list | Volumes this character appears in (detail only) |

---

### Team

A team, group, or organisation.

#### Detail — `GET /api/team/{id}/`

```
GET /api/team/4060-35/?api_key=KEY&format=json
```

**CV type prefix:** `4060-`

#### List — `GET /api/teams/`

#### Fields

| Field | Type | Description |
|-------|------|-------------|
| `id` | integer | Unique CV identifier |
| `name` | string | Team name |
| `aliases` | string | Alternate names |
| `api_detail_url` | string | API URL |
| `site_detail_url` | string | Website URL |
| `image` | object | Image URLs |
| `deck` | string | Short summary |
| `description` | string | Full HTML description |
| `publisher` | object | Publisher reference |
| `date_added` | datetime | Record creation date |
| `date_last_updated` | datetime | Last update date |
| `character_enemies` | list | Enemy characters (detail only) |
| `character_friends` | list | Allied characters (detail only) |
| `characters` | list | Team members (detail only) |
| `count_of_team_members` | integer | Number of members |
| `disbanded_in_issues` | list | Issues where team disbands (detail only) |
| `first_appeared_in_issue` | object | First appearance (detail only) |
| `issue_credits` | list | Issues this team appears in (detail only) |
| `issues_disbanded_in` | list | Issues where disbanded (detail only) |
| `story_arc_credits` | list | Story arcs (detail only) |
| `volume_credits` | list | Volumes (detail only) |

---

### Location

A fictional or real location referenced in comics.

#### Detail — `GET /api/location/{id}/`

```
GET /api/location/4020-40765/?api_key=KEY&format=json
```

**CV type prefix:** `4020-`

#### List — `GET /api/locations/`

#### Fields

| Field | Type | Description |
|-------|------|-------------|
| `id` | integer | Unique CV identifier |
| `name` | string | Location name |
| `aliases` | string | Alternate names |
| `api_detail_url` | string | API URL |
| `site_detail_url` | string | Website URL |
| `image` | object | Image URLs |
| `deck` | string | Short summary |
| `description` | string | Full HTML description |
| `start_year` | string | First appearance year |
| `date_added` | datetime | Record creation date |
| `date_last_updated` | datetime | Last update date |
| `first_appeared_in_issue` | object | First appearance issue (detail only) |
| `issue_credits` | list | Issues this location appears in (detail only) |
| `story_arc_credits` | list | Story arcs (detail only) |
| `volume_credits` | list | Volumes (detail only) |

---

### Concept

An abstract concept or theme in comics.

#### Detail — `GET /api/concept/{id}/`

```
GET /api/concept/4015-40669/?api_key=KEY&format=json
```

**CV type prefix:** `4015-`

#### List — `GET /api/concepts/`

#### Fields

| Field | Type | Description |
|-------|------|-------------|
| `id` | integer | Unique CV identifier |
| `name` | string | Concept name |
| `aliases` | string | Alternate names |
| `api_detail_url` | string | API URL |
| `site_detail_url` | string | Website URL |
| `image` | object | Image URLs |
| `deck` | string | Short summary |
| `description` | string | Full HTML description |
| `start_year` | string | First appearance year |
| `date_added` | datetime | Record creation date |
| `date_last_updated` | datetime | Last update date |
| `first_appeared_in_issue` | object | First appearance (detail only) |
| `issue_credits` | list | Issues (detail only) |
| `movies` | list | Movie appearances (detail only) |
| `story_arc_credits` | list | Story arcs (detail only) |
| `volume_credits` | list | Volumes (detail only) |

---

### Object

A notable object, artefact, or item.

#### Detail — `GET /api/object/{id}/`

```
GET /api/object/4055-40526/?api_key=KEY&format=json
```

**CV type prefix:** `4055-`

#### List — `GET /api/objects/`

#### Fields

| Field | Type | Description |
|-------|------|-------------|
| `id` | integer | Unique CV identifier |
| `name` | string | Object name |
| `aliases` | string | Alternate names |
| `api_detail_url` | string | API URL |
| `site_detail_url` | string | Website URL |
| `image` | object | Image URLs |
| `deck` | string | Short summary |
| `description` | string | Full HTML description |
| `start_year` | string | First appearance year |
| `date_added` | datetime | Record creation date |
| `date_last_updated` | datetime | Last update date |
| `first_appeared_in_issue` | object | First appearance (detail only) |
| `issue_credits` | list | Issues (detail only) |
| `story_arc_credits` | list | Story arcs (detail only) |
| `volume_credits` | list | Volumes (detail only) |

---

### Person

A real-world creator (writer, artist, editor, etc.).

#### Detail — `GET /api/person/{id}/`

```
GET /api/person/4040-40439/?api_key=KEY&format=json
```

**CV type prefix:** `4040-`

#### List — `GET /api/people/`

#### Fields

| Field | Type | Description |
|-------|------|-------------|
| `id` | integer | Unique CV identifier |
| `name` | string | Creator's name |
| `aliases` | string | Alternate names |
| `api_detail_url` | string | API URL |
| `site_detail_url` | string | Website URL |
| `image` | object | Image URLs |
| `deck` | string | Short summary |
| `description` | string | Full HTML description |
| `birth` | date | Date of birth |
| `death` | object | Date of death (if applicable) |
| `gender` | integer | `1` = Male, `2` = Female, `3` = Other |
| `hometown` | string | Hometown |
| `country` | string | Country |
| `website` | string | Personal website URL |
| `date_added` | datetime | Record creation date |
| `date_last_updated` | datetime | Last update date |
| `created_characters` | list | Characters this person created (detail only) |
| `issue_credits` | list | Issues credited in (detail only). Each entry has a `role` field that may be comma-separated. |
| `story_arc_credits` | list | Story arcs (detail only) |
| `volume_credits` | list | Volumes credited in (detail only) |

---

### Story Arc

A multi-issue story arc or event.

#### Detail — `GET /api/story_arc/{id}/`

```
GET /api/story_arc/4045-56414/?api_key=KEY&format=json
```

**CV type prefix:** `4045-`

#### List — `GET /api/story_arcs/`

#### Fields

| Field | Type | Description |
|-------|------|-------------|
| `id` | integer | Unique CV identifier |
| `name` | string | Story arc title |
| `aliases` | string | Alternate titles |
| `api_detail_url` | string | API URL |
| `site_detail_url` | string | Website URL |
| `image` | object | Image URLs |
| `deck` | string | Short summary |
| `description` | string | Full HTML description |
| `publisher` | object | Publisher reference |
| `date_added` | datetime | Record creation date |
| `date_last_updated` | datetime | Last update date |
| `first_appeared_in_issue` | object | First issue of the arc (detail only) |
| `issues` | list | All issues in this arc (detail only) |
| `movies` | list | Related movies (detail only) |

---

### Search — `GET /api/search/`

Searches across multiple resource types simultaneously.

```
GET /api/search/?api_key=KEY&format=json&query=Batman&resources=volume,character
```

#### Parameters

| Parameter   | Description |
|-------------|-------------|
| `query`     | Required. Search string. |
| `resources` | Comma-separated list of resource types to search. Defaults to all. Supported: `character`, `concept`, `issue`, `location`, `object`, `person`, `publisher`, `story_arc`, `team`, `video`, `volume` |
| `limit`     | Max results per resource type. |
| `page`      | 1-based page number for pagination (search uses `page`, not `offset`). |
| `field_list`| Fields to return. |

Each result includes a `resource_type` field indicating which type it belongs to.

> **Note (behaviour change Dec 2018):** Search query terms are **ORed** together (not ANDed). This produces thousands of results for multi-word queries, but results are sorted by relevance. Client-side filtering is required to discard results that do not contain all search terms.

---

## Resource Type ID Prefixes

CV-style IDs combine a type prefix with the numeric ID:

| Resource    | Prefix | Example |
|-------------|--------|---------|
| Volume      | 4050   | `4050-160294` |
| Issue       | 4000   | `4000-1073108` |
| Publisher   | 4010   | `4010-10` |
| Character   | 4005   | `4005-1021` |
| Team        | 4060   | `4060-35` |
| Location    | 4020   | `4020-40765` |
| Concept     | 4015   | `4015-40669` |
| Object      | 4055   | `4055-40526` |
| Person      | 4040   | `4040-40439` |
| Story Arc   | 4045   | `4045-56414` |
| Video       | 4070   | `4070-123` |

---

## Image Object

All `image` fields return an object with multiple resolution URLs. **`image` can be `null`** when no cover art is available — always null-check before accessing sub-fields.

```json
{
  "icon_url":        "https://comicvine.gamespot.com/a/uploads/square_avatar/...",
  "medium_url":      "https://comicvine.gamespot.com/a/uploads/scale_medium/...",
  "screen_url":      "https://comicvine.gamespot.com/a/uploads/screen_medium/...",
  "screen_large_url":"https://comicvine.gamespot.com/a/uploads/screen_kubrick/...",
  "small_url":       "https://comicvine.gamespot.com/a/uploads/scale_small/...",
  "super_url":       "https://comicvine.gamespot.com/a/uploads/scale_large/...",
  "thumb_url":       "https://comicvine.gamespot.com/a/uploads/scale_avatar/...",
  "tiny_url":        "https://comicvine.gamespot.com/a/uploads/square_mini/...",
  "original_url":    "https://comicvine.gamespot.com/a/uploads/original/...",
  "image_tags":      "All Images"
}
```

| Key | Approximate size |
|-----|-----------------|
| `tiny_url` | 40 × 40 |
| `icon_url` | 40 × 40 (square crop) |
| `thumb_url` | 100 × 100 |
| `small_url` | 100 × n |
| `medium_url` | 320 × n |
| `screen_url` | 640 × n |
| `screen_large_url` | 960 × n |
| `super_url` | 1280 × n |
| `original_url` | original dimensions |

---

## Filtering Reference

### Date Range Filter — Partial Dates

Date range filters accept partial dates (year and month/day components may be `1` rather than zero-padded). Both forms are accepted:

```
filter=cover_date:2024-01-01|2024-12-31   # zero-padded (preferred)
filter=cover_date:2024-1-1|2024-12-31     # partial ok
filter=cover_date:2011-1-1|2012-1-1       # year boundary scan
```

### Filterable Fields by Resource

**Volumes:**  `id`, `name`, `start_year`, `publisher`, `date_last_updated`, `date_added`

**Issues:**  `id`, `name`, `issue_number`, `volume`, `cover_date`, `store_date`, `date_last_updated`, `date_added`

**Publishers:**  `id`, `name`, `date_last_updated`, `date_added`

**Characters:**  `id`, `name`, `publisher`, `gender`, `date_last_updated`, `date_added`

**Teams:**  `id`, `name`, `publisher`, `date_last_updated`, `date_added`

**People:**  `id`, `name`, `gender`, `date_last_updated`, `date_added`

**Story Arcs:**  `id`, `name`, `publisher`, `date_last_updated`, `date_added`

**Locations / Concepts / Objects:**  `id`, `name`, `start_year`, `date_last_updated`, `date_added`

---

## Pagination Example

```python
import requests

API_KEY = "your_api_key"
all_results = []
offset = 0
limit = 100

while True:
    resp = requests.get(
        "https://comicvine.gamespot.com/api/issues/",
        params={
            "api_key": API_KEY,
            "format": "json",
            "filter": "volume:160294",
            "limit": limit,
            "offset": offset,
        },
    )
    data = resp.json()
    results = data.get("results", [])
    all_results.extend(results)

    if offset + limit >= data["number_of_total_results"] or not results:
        break
    offset += limit
```

---

## field_list Example

Request only specific fields to reduce payload size:

```
GET /api/volumes/?api_key=KEY&format=json&filter=name:Batman
    &field_list=id,name,start_year,publisher,count_of_issues
```

Response `results` will only contain the requested fields.

---

## Default Fields by Endpoint Type

When no `field_list` is specified, CV list endpoints return a standard subset of fields (not all detail fields). Detail endpoints return the full field set.

**List endpoints omit** deep relational arrays like `characters`, `person_credits`, `concept_credits`, etc.

**Detail endpoints** return the complete field set including all relational arrays.

---

## Description Field — HTML Content

The `description` field on all resource types is **raw HTML**, not plain text. It may include `<br>`, `<p>`, `<h4>`, `<table>`, and other tags, as well as HTML entities (`&nbsp;`, `&amp;`, etc.). Callers should sanitise or convert this field before displaying or storing it.

---

## Server Behaviour and Error Handling

### HTTP 500 Transient Errors

The ComicVine server occasionally returns HTTP 500. These are transient — a short retry loop (3 attempts with a 1-second delay) is sufficient:

```python
for attempt in range(3):
    resp = requests.get(url, params=params)
    if resp.status_code != 500:
        break
    time.sleep(1)
```

### Rate Limit — Retry Strategy

When `status_code: 107` is returned, the request should not be retried immediately. A practical backoff sequence is to wait **1, 2, 3, 4 minutes** between retries. The rate limit resets at the top of each clock hour, so a maximum total wait of ~20 minutes is sufficient.

### User-Agent Header

Some clients set a custom `User-Agent` header on all requests to avoid being blocked by generic bot filters:

```python
headers = {"User-Agent": "MyApp/1.0 (ComicVine API client)"}
requests.get(url, params=params, headers=headers)
```

The ComicVine API does not formally require a `User-Agent`, but it is good practice.
