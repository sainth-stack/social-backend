from __future__ import annotations

from celery.schedules import crontab

beat_schedule = {
    # ── Social media publishing ──────────────────────────────────────
    "enqueue-due-social-posts": {
        "task": "app.social.tasks.scheduler.enqueue_due_social_posts",
        "schedule": 60.0,  # every minute — safety net for scheduled posts
    },
    # ── Social media analytics ───────────────────────────────────────
    "sync-social-platform-analytics": {
        "task": "app.social.tasks.analytics_sync.sync_platform_analytics",
        "schedule": crontab(minute=30, hour=20),  # 2am IST = 20:30 UTC
    },
    "sync-social-post-metrics": {
        "task": "app.social.tasks.analytics_sync.sync_post_metrics",
        "schedule": crontab(minute=0, hour="*/4"),
    },
    "refresh-social-expiring-tokens": {
        "task": "app.social.tasks.maintenance.refresh_expiring_tokens",
        "schedule": crontab(minute=0, hour="*/6"),
    },
    "social-approval-reminders": {
        "task": "app.social.tasks.maintenance.send_approval_reminders",
        "schedule": crontab(minute=0),
    },
}
