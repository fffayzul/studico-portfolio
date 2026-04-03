# Studico — Backend API

Django backend for **Studico**, a social platform built exclusively for university students. Students verify their identity with an institutional email address, then connect with peers, communities, and events at their university.

Live at [teamstudico.com](https://www.teamstudico.com)

---

## Tech Stack

| Layer | Technology |
|-------|-----------|
| Framework | Django 5.1 (async views throughout) |
| API | Django REST Framework |
| Real-time | Django Channels + WebSockets (Redis channel layer) |
| Task Queue | Celery + Redis |
| Database | PostgreSQL (via Railway) |
| File Storage | Google Cloud Storage |
| Auth | Kinde OAuth2 (JWT validation) |
| Push Notifications | Firebase Cloud Messaging |
| Error Tracking | Sentry |
| Deployment | Railway |

---

## Architecture Overview

```
studifyfinal/
├── Users/
│   ├── models.py          # 61 models
│   ├── views.py           # 167 async view functions
│   ├── serializers.py     # DRF serializers
│   ├── consumers.py       # 24 WebSocket consumers
│   ├── tasks.py           # 26 Celery background tasks
│   ├── admin.py           # Custom Django admin with bulk actions
│   ├── kinde_functions.py # OAuth token validation & revocation
│   ├── cache_utils.py     # Redis caching layer
│   ├── scoring.py         # Content ranking algorithm
│   ├── signals.py         # Django signals for async side effects
│   └── management/
│       └── commands/      # Seed data management commands
├── studifyfinal/
│   ├── settings.py
│   ├── asgi.py            # ASGI config for Channels
│   ├── celery.py
│   └── middleware.py      # Custom request middleware
```

---

## Key Features

### Authentication & Identity
- OAuth2 via Kinde — JWT validation on every request with Redis-cached ban checks
- Students verify ownership of an institutional email address with a time-limited OTP before gaining full access
- Country-aware email domain validation (`.ac.uk` for UK, `.edu` for US, `.ca`/`.edu` for Canada)
- Refresh token rotation stored in DB with revocation on ban/logout

### Real-time (Django Channels)
24 WebSocket consumers covering:
- Direct messaging with typing indicators and read receipts (sent → delivered → read)
- Group chat (WhatsApp-style, independent of communities)
- Community-wide chat channels
- Live feed updates — posts, events, comments, likes pushed to connected clients without polling
- Notification streaming

### Background Tasks (Celery)
26 shared tasks including:
- Push notification delivery (FCM) for every interaction type — messages, likes, comments, friend requests, RSVPs, mentions
- Transactional emails — welcome, OTP verification, ban notification, GDPR deletion pipeline
- Mention processing — parses `@username` and `@community` tags from post/comment content
- GCS file cleanup — deletes orphaned media files from Google Cloud Storage
- Scheduled GDPR data deletion — 30-day deletion window with cancellation support

### Social Graph
- Bidirectional friend requests (send / accept / decline / ignore)
- Block and mute at user and community level
- Mutual friends calculation
- Suggestions algorithm — ranks users and communities by shared interests, university, location, course, and mutual friend count

### Content
- Posts and events with image/video attachments (multi-media support)
- Threaded comments with recursive parent/reply structure
- Likes, bookmarks, RSVPs (going / interested / not going)
- `@mention` support for users and communities in posts, comments, and bios
- Community posts and events scoped to community membership
- Content ranking score (`scoring.py`) for feed ordering

### Moderation & Safety
- Report system with status tracking (pending → resolved / dismissed) and action types (warn / suspend / ban / remove content)
- Admin ban action — revokes all tokens, clears Redis ban cache, triggers notification email, all in one admin action
- Community-level blocking — community admins can block individual users
- Role hierarchy within communities — admin / secondary admin / member

### Multi-Country Support
- `Country` model with per-country allowed email domains
- Universities, regions, and locations all scoped by country
- Management commands to seed UK, US, and Canadian universities, states/provinces, and major cities
- Backward-compatible `?country=` query param filtering on all reference data endpoints

### Admin Panel
Custom Django admin with:
- Inline views across all relationships (student's posts, comments, friendships, events, memberships, notifications in one page)
- Bulk ban action with automatic token revocation and email notification
- Email broadcast tool — send to all verified users via Celery (with confirmation step)
- Annotated list views with engagement metrics (post count, comment count, friend count)

---

## API Scale

- **167** view functions
- **170+** URL endpoints
- **61** database models
- **24** WebSocket consumers
- **26** Celery background tasks

---

## Data Model Highlights

```python
# Country-aware university scoping
class University(models.Model):
    university = models.CharField(max_length=50)
    country = models.ForeignKey(Country, on_delete=models.CASCADE)
    class Meta:
        unique_together = [('university', 'country')]

# Rich notification system
class Notification(models.Model):
    recipient = models.ForeignKey(Student, related_name='notifications_received')
    sender = models.ForeignKey(Student, null=True, related_name='notifications_sent')
    notificationtype = models.ForeignKey(notificationType)
    content_type = models.ForeignKey(ContentType)   # Generic FK — any model
    object_id = models.PositiveIntegerField()
    content_object = GenericForeignKey()

# Group chat with per-message read tracking
class GroupChatMessage(models.Model):
    group = models.ForeignKey(GroupChat, related_name='messages')
    student = models.ForeignKey(Student)
    message = models.TextField(blank=True)
    read_by = models.ManyToManyField(Student, related_name='read_group_messages', blank=True)

# GDPR-compliant deletion pipeline
class DataDeletionRequest(models.Model):
    student = models.OneToOneField(Student)
    requested_at = models.DateTimeField(auto_now_add=True)
    scheduled_deletion_date = models.DateTimeField()
    is_cancelled = models.BooleanField(default=False)
    deleted_at = models.DateTimeField(null=True)
```

---

## Environment Variables

```
SECRET_KEY
DATABASE_URL
REDIS_URL
KINDE_DOMAIN
KINDE_CLIENT_ID
KINDE_ISSUER_URL
KINDE_USERINFO_URL
EMAIL_HOST_PASSWORD
FIREBASE_CREDENTIALS   # base64-encoded service account JSON
GCS_BUCKET_NAME
SENTRY_DSN
```
