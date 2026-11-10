# MongoDB schema design

Produced by Stage 3 (`python -m pipeline schema`). This file is GENERATED; the self-review is in [self_review.md](self_review.md).

Designed by `openai/gpt-oss-120b`, criticized by `qwen/qwen3.8-27b`, and linted by deterministic code (`pipeline/schema_dsl.py`).

## 1. Conceptual schema

| Relationship | Cardinality | Decision | Rules | Why |
|---|---|---|---|---|
| Article‑User (author) | N:1 | **reference** | R1, R5 | author data needed on hot article reads, so store authorId and copy username/image (rarely change) |
| Article‑Comment | 1:N | **reference** | R2 | comments are unbounded per article, must stay in separate collection |
| Article‑Tag (TagList) | N:M | **embed array** | R1, R2 | each article has a small bounded list of tags, read together on article endpoints |
| Article‑User (Favorites) | N:M | **junction collection** | R3, R6 | favorites per article are unbounded; keep separate collection and denormalize a favoritesCount on Article |
| User‑User (Followers) | N:M self‑referential | **junction collection** | R4, R3, R6 | followers/following are unbounded; use a Followers collection and counters on User |
| Comment‑User (author) | N:1 | **reference** | R1, R5 | author info shown on comment reads, copy username/image for hot reads |

Collections:

- **articles** (from `Articles`): authorId + authorUsername/image (extended), tags array, favoritesCount, commentsCount
- **comments** (from `Comments`): authorId + authorUsername/image (extended)
- **tags** (from `Tags`): tag name only
- **users** (from `Users`): followersCount, followingCount
- **favorites** (from `Favorites`): compound _id {articleId,userId}
- 
- **followers** (from `Followers`): compound _id {userId,followerId}

Denormalized counters:

- `articles.favoritesCount` counts `Favorites`; maintained by POST/DELETE /api/articles/:slug/favorite
- `articles.commentsCount` counts `Comments`; maintained by POST/DELETE /api/articles/:slug/comments
- `users.followersCount` counts `Followers`; maintained by POST/DELETE /api/profiles/:username/follow (as follower)
- 
- `users.followingCount` counts `Followers`; maintained by POST/DELETE /api/profiles/:username/follow (as following)

Risks named by the designer:

- Extended author fields can become stale if username or image changes; must update all embedded copies on user update
- Embedded tag array assumes a small bound; a surge in tags per article could approach document size limits
- Denormalized counters require transactional updates; race conditions could cause off‑by‑one errors

## 2. Logical schema (field mapping)

### `articles`  (source table `Articles`, `_id` from ['id'], est. max doc 3504 B)

| Field | From | Type |
|---|---|---|
| `slug` | `Articles.slug` | string |
| `title` | `Articles.title` | string |
| `description` | `Articles.description` | string |
| `body` | `Articles.body` | string |
| `createdAt` | `Articles.createdAt` | date |
| `updatedAt` | `Articles.updatedAt` | date |
| `authorId` | `Articles.userId` | int |
| `tagList[]` | `TagList.tagName` (embedded, maxItems 20) | array | 
| `favoritesCount` | count of `Favorites` by `articleId` | int (computed) |

Indexes: `{"slug": 1}`, `{"createdAt": -1}`, `{"authorId": 1, "createdAt": -1}`, `{"tagList": 1, "createdAt": -1}`

- *tagList*: each article has a small bounded list of tags, read together on article endpoints
- *favoritesCount*: favorites per article are unbounded; keep separate collection and denormalize a favoritesCount on Article

### `comments`  (source table `Comments`, `_id` from ['id'], est. max doc 528 B)

| Field | From | Type |
|---|---|---|
| `body` | `Comments.body` | string |
| `createdAt` | `Comments.createdAt` | date |
| `updatedAt` | `Comments.updatedAt` | date |
| `articleId` | `Comments.articleId` | int |
| `authorId` | `Comments.userId` | int |

Indexes: `{"articleId": 1}`


### `tags`  (source table `Tags`, `_id` from ['name'], est. max doc 70 B)

| Field | From | Type |
|---|---|---|

Indexes: 


### `users`  (source table `Users`, `_id` from ['id'], est. max doc 480 B)

| Field | From | Type |
|---|---|---|
| `email` | `Users.email` | string |
| `username` | `Users.username` | string |
| `bio` | `Users.bio` | string |
| `image` | `Users.image` | string |
| `password` | `Users.password` | string |
| `createdAt` | `Users.createdAt` | date |
| `updatedAt` | `Users.updatedAt` | date |
| `followersCount` | count of `Followers` by `userId` | int (computed) |

Indexes: `{"username": 1}`, `{"email": 1}`

- *followersCount*: followers/following are unbounded; use a Followers collection and counters on User

### `favorites`  (source table `Favorites`, `_id` from ['articleId', 'userId'], est. max doc 64 B)

| Field | From | Type |
|---|---|---|

Indexes: `{"_id.articleId": 1}`, `{"_id.userId": 1}`


### `followers`  (source table `Followers`, `_id` from ['userId', 'followerId'], est. max doc 64 B)

| Field | From | Type |
|---|---|---|

Indexes: `{"_id.followerId": 1}`, `{"_id.userId": 1}`


## 3. Deterministic lint

- initial round 0: 9 errors
  - no index on articles for query [] sort createdAt (source Articles[]; used by controllers/articles.js:54 findAndCountAll order) (R10)
  - no index on articles for query ['author.id'] sort createdAt (source Articles['userId']; used by controllers/articles.js:130 findAndCountAll) (R10)
  - no index on comments for query ['articleId'] (source Comments['articleId']; used by controllers/comments.js:19 getComments) (R10)
  - no index on favorites for query ['_id.articleId'] (source Favorites['articleId']; used by controllers/favorites.js:34 addUser) (R10)
  - no index on favorites for query ['_id.userId'] (source Favorites['userId']; used by controllers/articles.js:51 getFavorites) (R10)
  - no index on followers for query ['_id.followerId'] (source Followers['followerId']; used by controllers/articles.js:128 getFollowing) (R10)
  - no index on followers for query ['_id.userId'] (source Followers['userId']; used by controllers/profiles.js:40 addFollower) (R10)
  - no index on articles for query ['tagList'] (source TagList['tagName']; used by controllers/articles.js:51 include filter) (R10)
  - no index on users for query ['email'] (source Users['email']; used by controllers/users.js:19 findOne) (R10)
- initial round 1: 10 errors
  - articles: index {'userId': 1, 'createdAt': 1} uses unknown fields ['userId']
  - favorites: index {'articleId': 1} uses unknown fields ['articleId']
  - favorites: index {'userId': 1} uses unknown fields ['userId']
  - followers: index {'followerId': 1} uses unknown fields ['followerId']
  - followers: index {'userId': 1} uses unknown fields ['userId']
  - no index on articles for query ['author.id'] sort createdAt (source Articles['userId']; used by controllers/articles.js:130 findAndCountAll) (R10)
  - no index on favorites for query ['_id.articleId'] (source Favorites['articleId']; used by controllers/favorites.js:34 addUser) (R10)
  - no index on favorites for query ['_id.userId'] (source Favorites['userId']; used by controllers/articles.js:51 getFavorites) (R10)
  - no index on followers for query ['_id.followerId'] (source Followers['followerId']; used by controllers/articles.js:128 getFollowing) (R10)
  - no index on followers for query ['_id.userId'] (source Followers['userId']; used by controllers/profiles.js:40 addFollower) (R10)
- initial round 2: 1 errors
  - no index on articles for query ['author.id'] sort createdAt (source Articles['userId']; used by controllers/articles.js:130 findAndCountAll) (R10)
- initial round 3: 0 errors - PASS
- after-critic round 0: 1 errors
  - comments: index {'userId': 1} uses unknown fields ['userId']
- after-critic round 1: 0 errors - PASS
- after-self-review round 0: 0 errors - PASS

For comparison, the naive one-to-one schema (`schema_naive.json`) fails 14 index-coverage checks (R10). It is kept unindexed on purpose, as the paper's one-to-one baseline.

## 4. Automated review gate (critic model)

Verdict: **revise**
- [blocking] Missing index on comments.userId prevents efficient retrieval of user comments if needed, and lacks composite index for articleId+createdAt for sorting. → *Add index {userId: 1} and {articleId: 1, createdAt: -1} to comments collection.*
- [minor] articles.indexes includes redundant {author.id: 1, createdAt: 1} alongside {authorId: 1, createdAt: 1}. → *Remove the redundant index on author.id to reduce write overhead.*
- [minor] favorites and followers use compound _id but lack explicit indexes on individual fields for standalone queries. → *Ensure _id subfields are indexed or add separate indexes if queries filter by single field.*

## 5. Data profile used by the lint (validation dataset)

```json
{
 "maxRowBytes": {
  "Users": 240,
  "Articles": 972,
  "Comments": 264,
  "Tags": 35,
  "TagList": 39,
  "Favorites": 32,
  "Followers": 32
 },
 "fanout": {
  "TagList.articleId": {
   "max": 4,
   "avg": 2.37
  },
  "Favorites.articleId": {
   "max": 20,
   "avg": 1.7
  },
  "Favorites.userId": {
   "max": 15,
   "avg": 5.72
  },
  "Followers.userId": {
   "max": 43,
   "avg": 3.94
  },
  "Followers.followerId": {
   "max": 10,
   "avg": 4.04
  },
  "Comments.articleId": {
   "max": 6,
   "avg": 3.0
  },
  "Articles.userId": {
   "max": 95,
   "avg": 5.56
  }
 }
}
```
