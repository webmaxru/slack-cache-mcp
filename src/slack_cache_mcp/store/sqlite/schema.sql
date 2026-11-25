PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
PRAGMA busy_timeout=5000;

CREATE TABLE IF NOT EXISTS devices (
  device_id TEXT PRIMARY KEY,
  label TEXT NOT NULL,
  public_key TEXT,
  created_at TEXT NOT NULL
) STRICT;

CREATE TABLE IF NOT EXISTS sources (
  source_id TEXT PRIMARY KEY,
  device_id TEXT NOT NULL REFERENCES devices(device_id),
  kind TEXT NOT NULL,
  path_hint TEXT NOT NULL,
  slack_version TEXT,
  first_seen TEXT NOT NULL,
  last_seen TEXT NOT NULL
) STRICT;

CREATE TABLE IF NOT EXISTS snapshots (
  snapshot_id TEXT PRIMARY KEY,
  source_id TEXT NOT NULL REFERENCES sources(source_id),
  started_at TEXT NOT NULL,
  finished_at TEXT,
  trigger TEXT NOT NULL,
  files_copied INTEGER,
  bytes_copied INTEGER,
  states_decoded INTEGER,
  decode_errors INTEGER,
  stats_json TEXT NOT NULL
) STRICT;

CREATE TABLE IF NOT EXISTS state_generations (
  state_sha256 TEXT PRIMARY KEY,
  workspace_key TEXT NOT NULL,
  self_user_id TEXT,
  blob_ref TEXT,
  idb_seq INTEGER,
  first_seen TEXT NOT NULL,
  last_seen TEXT NOT NULL,
  first_snapshot_id TEXT NOT NULL REFERENCES snapshots(snapshot_id),
  vault_path TEXT
) STRICT;

CREATE TABLE IF NOT EXISTS workspaces (
  workspace_key TEXT PRIMARY KEY,
  team_id TEXT,
  enterprise_id TEXT,
  name TEXT,
  domain TEXT,
  self_user_id TEXT,
  self_is_guest INTEGER,
  raw_json TEXT NOT NULL,
  first_seen TEXT NOT NULL,
  last_seen TEXT NOT NULL,
  updated_hlc TEXT NOT NULL
) STRICT;

CREATE TABLE IF NOT EXISTS channels (
  channel_id TEXT PRIMARY KEY,
  workspace_key TEXT NOT NULL REFERENCES workspaces(workspace_key),
  context_team_id TEXT,
  name TEXT,
  kind TEXT NOT NULL,
  is_ext_shared INTEGER,
  is_archived INTEGER,
  is_member INTEGER,
  topic TEXT,
  purpose TEXT,
  im_user_id TEXT,
  raw_json TEXT NOT NULL,
  first_seen TEXT NOT NULL,
  last_seen TEXT NOT NULL,
  updated_hlc TEXT NOT NULL
) STRICT;

CREATE TABLE IF NOT EXISTS channel_workspaces (
  channel_id TEXT NOT NULL,
  workspace_key TEXT NOT NULL,
  PRIMARY KEY (channel_id, workspace_key)
) STRICT;

CREATE TABLE IF NOT EXISTS channel_aliases (
  channel_id TEXT NOT NULL,
  name TEXT NOT NULL,
  first_seen TEXT NOT NULL,
  PRIMARY KEY (channel_id, name)
) STRICT;

CREATE TABLE IF NOT EXISTS users (
  user_id TEXT PRIMARY KEY,
  team_id TEXT,
  name TEXT,
  real_name TEXT,
  display_name TEXT,
  title TEXT,
  email TEXT,
  is_bot INTEGER,
  is_deleted INTEGER,
  is_restricted INTEGER,
  is_ultra_restricted INTEGER,
  is_external INTEGER,
  tz TEXT,
  raw_json TEXT NOT NULL,
  first_seen TEXT NOT NULL,
  last_seen TEXT NOT NULL,
  updated_hlc TEXT NOT NULL
) STRICT;

CREATE TABLE IF NOT EXISTS user_workspaces (
  user_id TEXT NOT NULL,
  workspace_key TEXT NOT NULL,
  PRIMARY KEY (user_id, workspace_key)
) STRICT;

CREATE TABLE IF NOT EXISTS message_versions (
  channel_id TEXT NOT NULL,
  ts TEXT NOT NULL,
  content_sha256 TEXT NOT NULL,
  edited_ts TEXT,
  is_deletion INTEGER NOT NULL DEFAULT 0,
  is_partial INTEGER NOT NULL DEFAULT 0,
  source_rank INTEGER NOT NULL,
  user_id TEXT,
  thread_ts TEXT,
  subtype TEXT,
  text TEXT NOT NULL,
  search_text TEXT NOT NULL,
  raw_json TEXT NOT NULL,
  first_seen TEXT NOT NULL,
  last_seen TEXT NOT NULL,
  PRIMARY KEY (channel_id, ts, content_sha256)
) STRICT;

CREATE TABLE IF NOT EXISTS message_sightings (
  channel_id TEXT NOT NULL,
  ts TEXT NOT NULL,
  content_sha256 TEXT NOT NULL,
  device_id TEXT NOT NULL,
  source TEXT NOT NULL,
  first_seen TEXT NOT NULL,
  last_seen TEXT NOT NULL,
  sightings INTEGER NOT NULL,
  first_snapshot_id TEXT,
  last_snapshot_id TEXT,
  last_state_sha256 TEXT,
  PRIMARY KEY (channel_id, ts, content_sha256, device_id, source)
) STRICT;

CREATE TABLE IF NOT EXISTS messages (
  rowid INTEGER PRIMARY KEY,
  channel_id TEXT NOT NULL,
  ts TEXT NOT NULL,
  ts_num REAL NOT NULL,
  workspace_key TEXT NOT NULL,
  user_id TEXT,
  thread_ts TEXT,
  is_reply INTEGER NOT NULL,
  subtype TEXT,
  text TEXT NOT NULL,
  search_text TEXT NOT NULL,
  current_sha256 TEXT NOT NULL,
  version_count INTEGER NOT NULL,
  edited_ts TEXT,
  deleted_state TEXT NOT NULL DEFAULT 'none',
  reply_count INTEGER,
  latest_reply TEXT,
  reply_users_json TEXT,
  has_files INTEGER NOT NULL DEFAULT 0,
  has_links INTEGER NOT NULL DEFAULT 0,
  first_seen TEXT NOT NULL,
  last_seen TEXT NOT NULL,
  UNIQUE (channel_id, ts)
) STRICT;

CREATE INDEX IF NOT EXISTS messages_channel_ts ON messages(channel_id, ts_num);
CREATE INDEX IF NOT EXISTS messages_ws_ts ON messages(workspace_key, ts_num);
CREATE INDEX IF NOT EXISTS messages_user_ts ON messages(user_id, ts_num);
CREATE INDEX IF NOT EXISTS messages_thread ON messages(channel_id, thread_ts, ts_num);

CREATE TABLE IF NOT EXISTS threads (
  channel_id TEXT NOT NULL,
  thread_ts TEXT NOT NULL,
  reply_count_reported INTEGER,
  replies_captured INTEGER NOT NULL DEFAULT 0,
  subscribed INTEGER,
  last_reply_ts TEXT,
  updated_hlc TEXT NOT NULL,
  PRIMARY KEY (channel_id, thread_ts)
) STRICT;

CREATE TABLE IF NOT EXISTS reactions (
  channel_id TEXT NOT NULL,
  ts TEXT NOT NULL,
  emoji TEXT NOT NULL,
  count INTEGER NOT NULL,
  users_json TEXT,
  observed_hlc TEXT NOT NULL,
  PRIMARY KEY (channel_id, ts, emoji)
) STRICT;

CREATE TABLE IF NOT EXISTS files (
  file_id TEXT PRIMARY KEY,
  workspace_key TEXT,
  user_id TEXT,
  name TEXT,
  title TEXT,
  mimetype TEXT,
  filetype TEXT,
  size INTEGER,
  created REAL,
  preview_text TEXT,
  local_path TEXT,
  local_sha256 TEXT,
  thumb_vault_path TEXT,
  ocr_text TEXT,
  raw_json TEXT NOT NULL,
  first_seen TEXT NOT NULL,
  last_seen TEXT NOT NULL,
  updated_hlc TEXT NOT NULL
) STRICT;

CREATE TABLE IF NOT EXISTS message_files (
  channel_id TEXT NOT NULL,
  ts TEXT NOT NULL,
  file_id TEXT NOT NULL,
  PRIMARY KEY (channel_id, ts, file_id)
) STRICT;

CREATE TABLE IF NOT EXISTS saved_items (
  workspace_key TEXT NOT NULL,
  item_type TEXT NOT NULL,
  item_id TEXT NOT NULL,
  channel_id TEXT,
  ts TEXT,
  state TEXT,
  date_created REAL,
  date_due REAL,
  raw_json TEXT NOT NULL,
  first_seen TEXT NOT NULL,
  last_seen TEXT NOT NULL,
  PRIMARY KEY (workspace_key, item_type, item_id)
) STRICT;

CREATE TABLE IF NOT EXISTS canvases (
  canvas_id TEXT PRIMARY KEY,
  workspace_key TEXT,
  channel_id TEXT,
  title TEXT,
  text TEXT NOT NULL,
  raw_json TEXT NOT NULL,
  content_sha256 TEXT NOT NULL,
  first_seen TEXT NOT NULL,
  last_seen TEXT NOT NULL
) STRICT;

CREATE TABLE IF NOT EXISTS canvas_versions (
  canvas_id TEXT NOT NULL,
  content_sha256 TEXT NOT NULL,
  text TEXT NOT NULL,
  first_seen TEXT NOT NULL,
  PRIMARY KEY (canvas_id, content_sha256)
) STRICT;

CREATE TABLE IF NOT EXISTS drafts (
  workspace_key TEXT NOT NULL,
  draft_id TEXT NOT NULL,
  channel_id TEXT,
  thread_ts TEXT,
  text TEXT NOT NULL,
  last_updated REAL,
  raw_json TEXT NOT NULL,
  first_seen TEXT NOT NULL,
  last_seen TEXT NOT NULL,
  PRIMARY KEY (workspace_key, draft_id)
) STRICT;

CREATE TABLE IF NOT EXISTS source_yield (
  snapshot_id TEXT NOT NULL,
  source_name TEXT NOT NULL,
  messages INTEGER NOT NULL DEFAULT 0,
  files INTEGER NOT NULL DEFAULT 0,
  canvases INTEGER NOT NULL DEFAULT 0,
  reactions INTEGER NOT NULL DEFAULT 0,
  saved_items INTEGER NOT NULL DEFAULT 0,
  drafts INTEGER NOT NULL DEFAULT 0,
  cache_entries INTEGER NOT NULL DEFAULT 0,
  errors INTEGER NOT NULL DEFAULT 0,
  extras_json TEXT NOT NULL DEFAULT '{}',
  PRIMARY KEY (snapshot_id, source_name)
) STRICT;

CREATE TABLE IF NOT EXISTS coverage_ranges (
  channel_id TEXT NOT NULL,
  start_ts TEXT NOT NULL,
  end_ts TEXT NOT NULL,
  contiguous_to_latest INTEGER NOT NULL,
  first_seen TEXT NOT NULL,
  last_seen TEXT NOT NULL,
  PRIMARY KEY (channel_id, start_ts)
) STRICT;

CREATE TABLE IF NOT EXISTS channel_status (
  channel_id TEXT PRIMARY KEY,
  latest_ts_reported TEXT,
  newest_captured_ts TEXT,
  oldest_captured_ts TEXT,
  captured_count INTEGER NOT NULL,
  unread_count INTEGER,
  last_window_seen TEXT,
  updated_at TEXT NOT NULL
) STRICT;

-- Consecutive-generation counters for suspected deletion (PLAN §8.2)
CREATE TABLE IF NOT EXISTS suspected_streaks (
  channel_id TEXT NOT NULL,
  ts TEXT NOT NULL,
  streak INTEGER NOT NULL,
  last_state_sha256 TEXT,
  last_seen TEXT NOT NULL,
  PRIMARY KEY (channel_id, ts)
) STRICT;

CREATE TABLE IF NOT EXISTS change_log (
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  hlc TEXT NOT NULL,
  device_id TEXT NOT NULL,
  entity TEXT NOT NULL,
  entity_key TEXT NOT NULL,
  op TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  payload_sha256 TEXT NOT NULL,
  UNIQUE (device_id, entity, entity_key, payload_sha256)
) STRICT;

CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
) STRICT;

CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
  search_text,
  author,
  channel,
  content='',
  contentless_delete=1,
  tokenize = "unicode61 remove_diacritics 2 tokenchars '_-'"
);

CREATE VIRTUAL TABLE IF NOT EXISTS messages_trigram USING fts5(
  search_text,
  content='',
  contentless_delete=1,
  tokenize='trigram'
);

-- M4 search units (embeddings). docs_vec is created at runtime via sqlite-vec
-- once the extension loads (dimension comes from the configured model).
CREATE TABLE IF NOT EXISTS search_docs (
  doc_id INTEGER PRIMARY KEY,
  kind TEXT NOT NULL,
  workspace_key TEXT NOT NULL,
  channel_id TEXT,
  anchor_ts TEXT,
  start_ts TEXT,
  end_ts TEXT,
  text TEXT NOT NULL,
  text_sha256 TEXT NOT NULL,
  embedded_model TEXT,
  embedded_at TEXT,
  message_rowid INTEGER,
  UNIQUE (kind, workspace_key, channel_id, anchor_ts, start_ts, end_ts)
) STRICT;

CREATE INDEX IF NOT EXISTS search_docs_pending
  ON search_docs(embedded_model) WHERE embedded_model IS NULL;
CREATE INDEX IF NOT EXISTS search_docs_channel
  ON search_docs(channel_id, anchor_ts);
