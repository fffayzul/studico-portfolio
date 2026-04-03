import os
from celery import Celery
from celery.schedules import crontab
import logging

logger = logging.getLogger(__name__)

# Set the default Django settings module for the 'celery' program.
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'studifyfinal.settings')

app = Celery('studifyfinal')

# Using a string here means the worker doesn't have to serialize
# the configuration object to child processes.
# - namespace='CELERY' means all celery-related configuration keys
#   should have a `CELERY_` prefix.
app.config_from_object('django.conf:settings', namespace='CELERY')

# Load task modules from all registered Django apps.
app.autodiscover_tasks()

# Firebase is NOT initialized here. Doing so would import Users.firebase_utils (and thus
# Django models) before Django's app registry is ready, causing AppRegistryNotReady
# in serverless/worker startup. Firebase is initialized lazily on first use inside
# firebase_utils.ensure_firebase_initialized() when a task sends a push notification.

# Celery Beat schedule for periodic tasks
# COMMENTED OUT: Disabled to save resources while app is not in active use
# Uncomment when ready to process data deletion requests automatically
# app.conf.beat_schedule = {
#     'process-data-deletion-requests': {
#         'task': 'Users.tasks.process_data_deletion_requests',
#         'schedule': crontab(hour=2, minute=0),  # Run daily at 2 AM UTC
#     },
# }

