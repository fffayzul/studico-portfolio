import asyncio
import logging

import firebase_admin
from django.conf import settings
from firebase_admin import credentials, messaging

from .models import DeviceToken

logger = logging.getLogger(__name__)


def ensure_firebase_initialized() -> bool:
    """
    Make sure firebase_admin has been initialized in the current process.
    Returns True on success, False if credentials are missing or initialization fails.
    """
    try:
        app = firebase_admin.get_app()
        logger.debug(f"Firebase app already initialized with project: {app.project_id}")
        return True
    except ValueError:
        info = getattr(settings, "FIREBASE_CREDENTIALS_INFO", None)
        if not info:
            logger.error("Firebase credentials missing; cannot initialize firebase_admin. Check that FSA environment variable is set.")
            return False
        try:
            # Validate that info is a dict with required keys
            if not isinstance(info, dict):
                logger.error(f"FIREBASE_CREDENTIALS_INFO is not a dict, got {type(info)}")
                return False
            
            required_keys = ['type', 'project_id', 'private_key_id', 'private_key', 'client_email']
            missing_keys = [key for key in required_keys if key not in info]
            if missing_keys:
                logger.error(f"Firebase credentials missing required keys: {missing_keys}")
                return False
            
            cred = credentials.Certificate(info)
            app = firebase_admin.initialize_app(cred)
            logger.info(f"Firebase app initialized successfully with project: {app.project_id}")
            return True
        except Exception as exc:
            logger.error(f"Failed to initialize firebase_admin: {exc}", exc_info=True)
            return False
    except Exception as exc:
        logger.error(f"Unexpected error accessing firebase_admin app: {exc}", exc_info=True)
        return False


def send_push_notification(token, title, body, data=None):
    """
    Send push notification via FCM
    Returns True if successful, False otherwise
    """
    # Ensure Firebase is initialized before attempting to send
    if not ensure_firebase_initialized():
        logger.error("Firebase is not initialized; cannot send push notification.")
        return False
    
    try:
        # Debug: Log the data being sent
        if data:
            logger.info(f"FCM Data being sent: {data}")
            # Check for non-string values
            for key, value in data.items():
                if not isinstance(value, str):
                    logger.error(f"Non-string value found in FCM data: {key}={value} (type: {type(value)})")
        
        message = messaging.Message(
            notification=messaging.Notification(
                title=title,
                body=body,
            ),
            data=data or {},
            token=token,
        )
        
        response = messaging.send(message)
        logger.info(f"Successfully sent message: {response}")
        return True
        
    except Exception as e:
        logger.error(f"Failed to send FCM message: {str(e)}")
        
        # Handle invalid tokens
        error_str = str(e).lower()
        if ("registration-token-not-registered" in error_str or 
            "requested entity was not found" in error_str or
            "invalid-registration-token" in error_str or
            "unregistered" in error_str):
            # Mark token as inactive
            DeviceToken.objects.filter(token=token).update(is_active=False)
            logger.info(f"Marked token as inactive due to invalid token: {token[:10]}...")
        
        return False


def send_push_notifications_to_user(user, title, body, data):
    """
    Send a push notification to all of a user's active devices concurrently.
    """
    if not ensure_firebase_initialized():
        logger.error("Firebase is not initialized; skipping push notification dispatch.")
        return

    device_tokens = list(
        DeviceToken.objects.filter(user=user, is_active=True).values_list('token', flat=True)
    )

    if not device_tokens:
        logger.info(f"No active device tokens found for user {user.id}")
        return

    async def _dispatch():
        tasks = [
            asyncio.to_thread(send_push_notification, token, title, body, data)
            for token in device_tokens
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for token, result in zip(device_tokens, results):
            if isinstance(result, Exception):
                logger.error(f"Error sending notification to token {token[:10]}...: {result}")

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        asyncio.run(_dispatch())
    else:
        if loop.is_running():
            loop.create_task(_dispatch())
        else:
            loop.run_until_complete(_dispatch())
