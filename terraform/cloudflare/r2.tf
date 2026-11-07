# Ziftbook service photos (ZIF-133): two R2 buckets per environment, EU jurisdiction (fixed at creation: moving a
# bucket means a new one plus a copy). The app's own R2 token per environment (made by hand, docs/runbooks.md "R2 for
# Ziftbook photos") reads and writes objects; this stack only shapes the buckets.
# - uploads: private. The browser PUTs the raw file with a presigned URL, the API checks and resizes it at finalize,
#   and anything left over expires after a day, so an unprocessed file is never public.
# - media: public read through its custom domain only. No CORS: plain <img>.
# The r2.dev URL stays off: the default for a new bucket. Not managed: provider 5.27's cloudflare_r2_managed_domain
# never reads it back, so it would neither enforce nor detect a change by hand.
locals {
  ziftbook_r2 = {
    staging = { origin = "https://dev.ziftbook.com", media_domain = "img-dev.ziftbook.com" }
    prod    = { origin = "https://app.ziftbook.com", media_domain = "img.ziftbook.com" }
  }
}

resource "cloudflare_r2_bucket" "ziftbook_uploads" {
  for_each     = local.ziftbook_r2
  account_id   = var.account_id
  name         = "ziftbook-uploads-${each.key}"
  jurisdiction = "eu"
}

resource "cloudflare_r2_bucket" "ziftbook_media" {
  for_each     = local.ziftbook_r2
  account_id   = var.account_id
  name         = "ziftbook-media-${each.key}"
  jurisdiction = "eu"
}

resource "cloudflare_r2_bucket_cors" "ziftbook_uploads" {
  for_each     = local.ziftbook_r2
  account_id   = var.account_id
  bucket_name  = cloudflare_r2_bucket.ziftbook_uploads[each.key].name
  jurisdiction = "eu"
  rules = [{
    id = "browser-put"
    allowed = {
      methods = ["PUT"]
      origins = [each.value.origin]
      headers = ["content-type"]
    }
    max_age_seconds = 3600
  }]
}

# The whole rule set of the bucket: it replaces R2's default rule (abort multipart uploads after 7 days), so that
# one is kept here, shortened to a day.
resource "cloudflare_r2_bucket_lifecycle" "ziftbook_uploads" {
  for_each     = local.ziftbook_r2
  account_id   = var.account_id
  bucket_name  = cloudflare_r2_bucket.ziftbook_uploads[each.key].name
  jurisdiction = "eu"
  rules = [{
    id         = "expire-after-1-day"
    enabled    = true
    conditions = { prefix = "" }
    delete_objects_transition = {
      condition = { type = "Age", max_age = 86400 }
    }
    abort_multipart_uploads_transition = {
      condition = { type = "Age", max_age = 86400 }
    }
  }]
}

resource "cloudflare_r2_custom_domain" "ziftbook_media" {
  for_each     = local.ziftbook_r2
  account_id   = var.account_id
  bucket_name  = cloudflare_r2_bucket.ziftbook_media[each.key].name
  jurisdiction = "eu"
  domain       = each.value.media_domain
  zone_id      = data.cloudflare_zone.ziftbook.id
  enabled      = true
  min_tls      = "1.2"
}

# Ziftbook erasure ledger (ZIF-58, INFRA-267): one private bucket per environment, EU jurisdiction, apart from the
# photo buckets. No CORS, no custom domain, r2.dev off (the default). Each erasure's entry under erasures/ is locked so
# a restored backup can be erased again: the lock must outlast the oldest restorable backup (deleted after 8 days,
# the backup bucket below; tests/test_erasure_ledger.py). If backup retention grows, raise this first.
# Tokens are made by hand (docs/runbooks.md "Ziftbook erasure ledger").
locals {
  ziftbook_ledger_lock_days = 14
}

resource "cloudflare_r2_bucket" "ziftbook_ledger" {
  for_each     = local.ziftbook_r2
  account_id   = var.account_id
  name         = "ziftbook-ledger-${each.key}"
  jurisdiction = "eu"
}

resource "cloudflare_r2_bucket_lock" "ziftbook_ledger" {
  for_each     = local.ziftbook_r2
  account_id   = var.account_id
  bucket_name  = cloudflare_r2_bucket.ziftbook_ledger[each.key].name
  jurisdiction = "eu"
  rules = [{
    id        = "lock-erasures"
    enabled   = true
    prefix    = "erasures/"
    condition = { type = "Age", max_age_seconds = local.ziftbook_ledger_lock_days * 86400 }
  }]
}

# A day after the lock ends. Like ziftbook_uploads, this replaces R2's default rule, so the multipart one is kept.
resource "cloudflare_r2_bucket_lifecycle" "ziftbook_ledger" {
  for_each     = local.ziftbook_r2
  account_id   = var.account_id
  bucket_name  = cloudflare_r2_bucket.ziftbook_ledger[each.key].name
  jurisdiction = "eu"
  rules = [
    {
      id         = "expire-erasures"
      enabled    = true
      conditions = { prefix = "erasures/" }
      delete_objects_transition = {
        condition = { type = "Age", max_age = (local.ziftbook_ledger_lock_days + 1) * 86400 }
      }
    },
    {
      id         = "abort-multipart-after-1-day"
      enabled    = true
      conditions = { prefix = "" }
      abort_multipart_uploads_transition = {
        condition = { type = "Age", max_age = 86400 }
      }
    },
  ]
}

# Nightly database backups (INFRA-203): every object locked for 7 days and deleted after 8. Private, EU jurisdiction,
# no CORS, no custom domain, r2.dev off (the default). The dumps arrive gpg-encrypted to the owner's backup key
# (clusters/hetzner/data/db-backup.yaml); the uploader's token is made by hand (docs/runbooks.md "Backups to R2").
locals {
  backup_lock_days = 7
}

resource "cloudflare_r2_bucket" "backups" {
  account_id   = var.account_id
  name         = "db-backups"
  jurisdiction = "eu"
}

resource "cloudflare_r2_bucket_lock" "backups" {
  account_id   = var.account_id
  bucket_name  = cloudflare_r2_bucket.backups.name
  jurisdiction = "eu"
  rules = [{
    id        = "lock-all"
    enabled   = true
    prefix    = ""
    condition = { type = "Age", max_age_seconds = local.backup_lock_days * 86400 }
  }]
}

# A day after the lock ends. Like ziftbook_uploads, this replaces R2's default rule, so the multipart one is kept.
resource "cloudflare_r2_bucket_lifecycle" "backups" {
  account_id   = var.account_id
  bucket_name  = cloudflare_r2_bucket.backups.name
  jurisdiction = "eu"
  rules = [{
    id         = "expire-after-8-days"
    enabled    = true
    conditions = { prefix = "" }
    delete_objects_transition = {
      condition = { type = "Age", max_age = (local.backup_lock_days + 1) * 86400 }
    }
    abort_multipart_uploads_transition = {
      condition = { type = "Age", max_age = 86400 }
    }
  }]
}
