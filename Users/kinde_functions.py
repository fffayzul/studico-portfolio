import jwt
import os
import logging
import requests
from django.conf import settings
from django.http import JsonResponse
from django.core.cache import cache
from django.db.models import Q
from django.utils import timezone
from functools import wraps
from jwt.algorithms import RSAAlgorithm
from channels.db import database_sync_to_async
import sys, traceback
from asgiref.sync import sync_to_async, async_to_sync
from inspect import iscoroutinefunction
import json
import hashlib
from datetime import datetime

logger = logging.getLogger(__name__)

# Process-level JWKS fallback cache — survives Redis outages within the same worker process.
# Structure: {'keys': [...], 'fetched_at': float}
_jwks_memory_cache: dict = {}
_JWKS_MEMORY_TTL = 3600  # 1 hour, same as Redis TTL

# --- Helper functions (assuming these are defined elsewhere or provided here) ---

def get_kinde_public_keys():
    """
    Fetches Kinde's public keys (JWKS) from the issuer URL.
    Primary cache: Redis (1-hour TTL, shared across workers).
    Fallback cache: process-level in-memory dict (guards against Redis being unavailable).
    """
    cache_key = 'kinde_jwks_keys'

    # 1. Try Redis first
    try:
        cached_jwks = cache.get(cache_key)
        if cached_jwks:
            logger.debug("Using cached JWKS keys (Redis)")
            return cached_jwks
    except Exception:
        pass  # Redis unavailable — fall through to memory cache

    # 2. Try process-level memory cache (survives Redis outages)
    if _jwks_memory_cache.get('keys'):
        age = datetime.now().timestamp() - _jwks_memory_cache.get('fetched_at', 0)
        if age < _JWKS_MEMORY_TTL:
            logger.debug("Using cached JWKS keys (memory fallback)")
            return _jwks_memory_cache

    # 3. Fetch from Kinde
    issuer_url = os.getenv('KINDE_ISSUER_URL') or os.getenv('KINDE_DOMAIN')
    if not issuer_url:
        logger.error("KINDE_ISSUER_URL or KINDE_DOMAIN not set")
        return None

    issuer_url = issuer_url.rstrip('/')
    jwks_url = f"{issuer_url}/.well-known/jwks.json"

    try:
        logger.info(f"Fetching JWKS from {jwks_url}")
        response = requests.get(jwks_url, timeout=10)
        response.raise_for_status()
        jwks_data = response.json()

        if not isinstance(jwks_data, dict) or 'keys' not in jwks_data:
            logger.error(f"Invalid JWKS structure received: {jwks_data}")
            return None

        # Store in both Redis and the process-level fallback
        try:
            cache.set(cache_key, jwks_data, 3600)
        except Exception:
            pass
        _jwks_memory_cache.update({'keys': jwks_data['keys'], 'fetched_at': datetime.now().timestamp()})
        # Keep the full dict shape for callers that check jwks['keys']
        _jwks_memory_cache['keys'] = jwks_data['keys']

        logger.info(f"Successfully fetched and cached JWKS from {jwks_url} with {len(jwks_data.get('keys', []))} keys")
        return jwks_data

    except requests.exceptions.RequestException as e:
        logger.error(f"Failed to fetch JWKS from {jwks_url}: {e}")
        # Return stale memory cache rather than None if we have anything
        if _jwks_memory_cache.get('keys'):
            logger.warning("Returning stale in-memory JWKS after fetch failure")
            return _jwks_memory_cache
        return None
    except (ValueError, json.JSONDecodeError) as e:
        logger.error(f"Failed to parse JWKS JSON from {jwks_url}: {e}")
        return None

# In the context of KindeAuthMiddleware, the raw_token is passed directly,
# so extract_token might not be needed if KindeAuthMiddleware extracts it.
# However, if it's used elsewhere, keep its definition.
def extract_token(auth_header: str, token_type: str) -> str:
    """
    Extracts the token string from the Authorization header.
    Assumes header format: "IDBearer <token_string>"
    """
    if token_type not in auth_header:
        return None
    try:
        # Splits "IDBearer <token>;other_stuff" to get "<token>"
        return auth_header.split(token_type)[1].split(';')[0].strip()
    except IndexError:
        return None
# --- End Helper functions ---

def _log_token_failure(reason: str, token_hash: str, payload: dict | None = None, extra_message: str = ""):
    """
    Centralized structured logging for token failures.
    Logs:
      - reason: short tag (expired / revoked / invalid_signature / jwks_fetch_failed / etc.)
      - hash_prefix: first 8 chars of sha256(token)
      - sub: Kinde user id from payload, when available
      - exp: raw exp timestamp from payload, when available
      - exp_delta_seconds: exp - now (negative if expired), when exp present
    """
    sub = None
    exp = None
    if isinstance(payload, dict):
        sub = payload.get("sub")
        exp = payload.get("exp")
    now_ts = datetime.now().timestamp()
    exp_delta = None
    if isinstance(exp, (int, float)):
        exp_delta = exp - now_ts

    logger.info(
        "kinde_token_failure reason=%s hash_prefix=%s sub=%s exp=%s exp_delta_seconds=%s %s",
        reason,
        (token_hash or "")[:8],
        sub,
        exp,
        exp_delta,
        extra_message or "",
    )


def _classify_token_error_message(err_msg: str | None) -> str:
    """
    Map human-readable error strings into coarse categories
    so views can log a stable 'result category'.
    """
    if not err_msg:
        return "other"
    m = err_msg.lower()
    if "expired" in m:
        return "expired"
    if "revoked" in m:
        return "revoked"
    if "signature" in m:
        return "invalid_signature"
    if "issuer" in m:
        return "invalid_issuer"
    if "invalid token format" in m or "format" in m:
        return "invalid_format"
    if "missing key id" in m or "missing key" in m:
        return "missing_kid"
    if "key id not found" in m:
        return "kid_not_found"
    if "public key format" in m or "public key" in m:
        return "invalid_public_key"
    if "fetch public keys" in m or "jwks" in m:
        return "jwks_fetch_failed"
    if "server configuration error" in m:
        return "server_config_error"
    if "authorization header missing" in m or "access token is empty" in m or "access token missing" in m:
        return "no_token"
    return "other"


# --- Token Cache Invalidation ---

# def invalidate_kinde_token_cache(raw_token: str):
#     """
#     Invalidate the cached verification result for a specific token.
#     Call this when:
#     - User logs out (invalidate their current token)
#     - User refreshes tokens (invalidate old token before new one is issued)
#     - User logs in (optional - if you want to force fresh verification)
    
#     Usage example in logout endpoint:
#         from .kinde_functions import invalidate_kinde_token_cache
        
#         @kinde_auth_required
#         async def logout(request, kinde_user_id=None):
#             auth_header = request.headers.get("Authorization", "")
#             if "IDBearer" in auth_header:
#                 token = auth_header.split("IDBearer")[1].split(';')[0].strip()
#                 invalidate_kinde_token_cache(token)
#             return JsonResponse({'status': 'success', 'message': 'Logged out'})
    
#     Args:
#         raw_token (str): The raw JWT token string to invalidate.
    
#     Returns:
#         bool: True if cache was deleted, False if token was invalid or cache didn't exist.
#     """
#     if not raw_token:
#         return False
    
#     try:
#         token_hash = hashlib.sha256(raw_token.encode()).hexdigest()[:32]
#         cache_key = f'kinde_verified_token_{token_hash}'
#         deleted = cache.delete(cache_key)
#         if deleted:
#             logger.info(f"Token cache invalidated: {token_hash[:8]}...")
#         return deleted
#     except Exception as e:
#         logger.error(f"Error invalidating token cache: {e}")
#         return False

def verify_kinde_token(raw_token: str) -> dict:
    """
    Fast Kinde JWT token validation with minimal overhead.
    Optimized for speed by removing fallback mechanisms.
    
    Args:
        raw_token (str): The raw JWT token string.

    Returns:
        dict: A dictionary with 'user' (containing decoded payload) on success,
              or 'error' (with a message) on failure.
    """
    if not raw_token:
        # Nothing to hash / decode; still log a structured reason.
        _log_token_failure("empty_token", token_hash="", payload=None, extra_message="Token string is empty")
        return {"error": "Unauthorized - Token is empty"}

    # Cache key from token hash
    token_hash = hashlib.sha256(raw_token.encode()).hexdigest()[:32]
    cache_key = _verification_cache_key(token_hash)
    # Reject revoked tokens (ban / single active session): we mark revoked when we call Kinde revoke
    if cache.get(_revoked_token_hash_key(token_hash)):
        # Best-effort decode without verification so we can log sub/exp.
        payload_for_log = None
        try:
            payload_for_log = jwt.decode(raw_token, options={"verify_signature": False, "verify_exp": False})
        except Exception:
            pass
        _log_token_failure("revoked", token_hash, payload_for_log)
        logger.warning("Rejected revoked token (single active session or ban), hash_prefix=%s", token_hash[:8])
        return {"error": "Unauthorized - Token has been revoked."}
    # Check cache first
    cached_result = cache.get(cache_key)
    if cached_result is not None:
        return cached_result

    # Decode once without verification to get header and check expiration
    try:
        unverified_payload = jwt.decode(raw_token, options={"verify_signature": False})
        header = jwt.get_unverified_header(raw_token)
    except jwt.DecodeError:
        # Invalid format - permanent error, safe to cache
        result = {"error": "Unauthorized - Invalid token format."}
        _log_token_failure("invalid_format", token_hash, payload=None)
        cache.set(cache_key, result, 300)  # Cache 5 min
        return result
    
    # Quick expiration check
    token_exp = unverified_payload.get('exp')
    if token_exp and token_exp < datetime.now().timestamp():
        # Expired token - permanent error, safe to cache
        result = {"error": "Unauthorized - Token has expired."}
        _log_token_failure("expired", token_hash, unverified_payload)
        cache.set(cache_key, result, 300)  # Cache 5 min
        return result

    # Get kid from header
    kid = header.get('kid')
    if not kid:
        # Missing KID - permanent error, safe to cache
        result = {"error": "Unauthorized - Token missing key ID."}
        _log_token_failure("missing_kid", token_hash, unverified_payload)
        cache.set(cache_key, result, 300)
        return result

    # Fetch JWKS (cached internally by get_kinde_public_keys)
    jwks = get_kinde_public_keys()
    if not jwks or 'keys' not in jwks:
        # JWKS fetch failure - transient error, DON'T cache (could be network issue)
        result = {"error": "Unauthorized - Unable to fetch public keys."}
        _log_token_failure("jwks_fetch_failed", token_hash, unverified_payload)
        return result

    # Find matching key (exact match only)
    key = next((k for k in jwks['keys'] if k.get('kid') == kid), None)
    
    if not key:
        # Key ID not found - could be temporary during key rotation, cache briefly
        result = {"error": "Unauthorized - Key ID not found."}
        _log_token_failure("kid_not_found", token_hash, unverified_payload)
        cache.set(cache_key, result, 60)  # Short cache (1 min) in case of key rotation
        return result

    # Convert JWK to RSA Public Key
    try:
        public_key = jwt.algorithms.RSAAlgorithm.from_jwk(key)
    except Exception:
        # Invalid key format - permanent error, safe to cache
        result = {"error": "Unauthorized - Invalid public key format."}
        _log_token_failure("invalid_public_key", token_hash, unverified_payload)
        cache.set(cache_key, result, 300)
        return result

    # Get issuer for verification
    issuer_url = os.getenv('KINDE_ISSUER_URL') or os.getenv('KINDE_DOMAIN')
    if not issuer_url:
        # Configuration error - don't cache (might be fixed)
        result = {"error": "Unauthorized - Server configuration error."}
        _log_token_failure("server_config_error", token_hash, unverified_payload)
        return result
    issuer_url = issuer_url.rstrip('/')
    
    # Verify token (no audience verification)
    try:
        payload = jwt.decode(
            raw_token,
            public_key,
            algorithms=["RS256"],
            options={"verify_signature": True, "verify_exp": True, 
                    "verify_iss": True, "verify_aud": False},
            issuer=issuer_url
        )
        # Success - cache until token expires (with 5 min buffer)
        success_result = {"message": "Token is valid", "user": payload}
        ttl = max(60, int(token_exp - datetime.now().timestamp() - 300)) if token_exp else 300
        cache.set(cache_key, success_result, ttl)
        return success_result
        
    except jwt.exceptions.ExpiredSignatureError:
        # Expired - permanent error, safe to cache
        result = {"error": "Unauthorized - Token has expired."}
        _log_token_failure("expired", token_hash, unverified_payload)
        cache.set(cache_key, result, 300)
        return result
    except jwt.exceptions.InvalidIssuerError:
        # Invalid issuer - permanent error, safe to cache
        result = {"error": "Unauthorized - Invalid token issuer."}
        _log_token_failure("invalid_issuer", token_hash, unverified_payload)
        cache.set(cache_key, result, 300)
        return result
    except jwt.exceptions.InvalidSignatureError:
        # Invalid signature - permanent error, safe to cache
        result = {"error": "Unauthorized - Invalid token signature."}
        _log_token_failure("invalid_signature", token_hash, unverified_payload)
        cache.set(cache_key, result, 300)
        return result
    except Exception as e:
        # Unexpected errors - transient, DON'T cache (could be temporary issue)
        logger.error(f"Unexpected token verification error: {e}", exc_info=True)
        result = {"error": "Unauthorized - Token verification failed."}
        _log_token_failure("verification_exception", token_hash, unverified_payload, extra_message=str(e))
        return result




# TTL for ban check cache (seconds). Only "banned" is cached; "not banned" is not cached so bans take effect immediately.
USER_BANNED_CACHE_TTL = 300  # 5 min
# TTL for caching current access token (so we can revoke it on ban for immediate kick-off).
ACCESS_TOKEN_CACHE_TTL = 600  # 10 min

def _ban_cache_key(kinde_user_id):
    """Cache key for 'is this user banned' result. Normalized so admin and decorator always match."""
    return f'user_banned_{str(kinde_user_id or "").strip()}'


def _access_token_cache_key(kinde_user_id):
    """Cache key for current access token (revoked on ban for immediate logout)."""
    return f'kinde_access_token_{kinde_user_id}'


def _verification_cache_key(token_hash):
    """Cache key for verified token result."""
    return f'kinde_verified_token_{token_hash}'


def _revoked_token_hash_key(token_hash):
    """Cache key for revoked token (so we reject it until it would have expired)."""
    return f'kinde_revoked_token_{token_hash}'


def invalidate_verification_cache_for_token(access_token_str):
    """
    After revoking an access token at Kinde: (1) mark this token as revoked so
    verify_kinde_token rejects it; (2) clear our verification cache. So the
    next API call with this token gets 401 and the app can show login again.
    """
    if not (access_token_str or '').strip():
        return
    token_str = access_token_str.strip()
    token_hash = hashlib.sha256(token_str.encode()).hexdigest()[:32]
    ttl = 3600
    try:
        unverified = jwt.decode(token_str, options={"verify_signature": False, "verify_exp": False})
        exp = unverified.get('exp')
        if exp and isinstance(exp, (int, float)):
            ttl = max(60, int(exp - datetime.now().timestamp()))
    except Exception:
        pass
    cache.set(_revoked_token_hash_key(token_hash), True, ttl)
    cache.delete(_verification_cache_key(token_hash))
    logger.debug("Marked token as revoked and invalidated verification cache")


def _extract_access_token(auth_header):
    """Extract access token from header 'IDBearer <id>; AccessBearer <access>'."""
    if not auth_header:
        return None
    for part in auth_header.split(';'):
        part = part.strip()
        if part.startswith('AccessBearer '):
            return part.split('AccessBearer ', 1)[1].strip()
    return None


def cache_access_token_for_revoke(kinde_user_id, access_token):
    """Store the user's current access token so we can revoke it on ban (immediate kick-off)."""
    token = (access_token or '').strip()
    if kinde_user_id and token:
        cache.set(_access_token_cache_key(kinde_user_id), token, ACCESS_TOKEN_CACHE_TTL)


def _is_banned_sync(kinde_user_id):
    """Return True if this user is currently banned (no frontend change needed; use 403). Cached to avoid DB hit every request."""
    kinde_user_id = (kinde_user_id or "").strip() if kinde_user_id else ""
    if not kinde_user_id:
        return False
    cache_key = _ban_cache_key(kinde_user_id)
    cached = cache.get(cache_key)
    if cached is not None:
        return cached
    from .models import Student, BannedStudents
    try:
        student = Student.objects.get(kinde_user_id=kinde_user_id)
    except Student.DoesNotExist:
        # Do not cache False: so after a ban the next request will hit DB and see the ban
        return False
    now = timezone.now()
    is_banned = BannedStudents.objects.filter(
        student=student
    ).filter(
        Q(banned_until__isnull=True) | Q(banned_until__gt=now)
    ).exists()
    if is_banned:
        cache.set(cache_key, True, USER_BANNED_CACHE_TTL)
    # Do not cache False so we never serve stale "not banned" after a ban
    return is_banned


def kinde_auth_required(view_func):
    if iscoroutinefunction(view_func):
        # --- Async View ---
        @wraps(view_func)
        async def async_wrapper(request, *args, **kwargs):
            auth_header = request.headers.get("Authorization", "")
            if not auth_header:
                logger.warning(
                    "auth_failure category=no_token path=%s method=%s detail=%s",
                    getattr(request, "path", ""),
                    getattr(request, "method", ""),
                    "Authorization header missing",
                )
                return JsonResponse({"error": "Unauthorized - Authorization header missing"}, status=401)
            
            if "AccessBearer" not in auth_header:
                logger.warning(
                    "auth_failure category=no_token path=%s method=%s detail=%s",
                    getattr(request, "path", ""),
                    getattr(request, "method", ""),
                    "AccessBearer missing in Authorization header",
                )
                return JsonResponse({"error": "Unauthorized - Access token missing. Expected format: 'IDBearer <token>; AccessBearer <token>'"}, status=401)

            try:
                # Verify the ACCESS token (same token we cache and revoke for ban/single session)
                raw_token = _extract_access_token(auth_header)
                if not raw_token:
                    logger.warning(
                        "auth_failure category=no_token path=%s method=%s detail=%s",
                        getattr(request, "path", ""),
                        getattr(request, "method", ""),
                        "Access token empty after extraction",
                    )
                    return JsonResponse({"error": "Unauthorized - Access token is empty"}, status=401)
                logger.debug(f"Extracted token (first 50 chars): {raw_token[:50]}...")
                token_data = await sync_to_async(verify_kinde_token)(raw_token)
            except Exception as e:
                logger.error(f"Token extraction/verification error: {e}")
                traceback.print_exc(file=sys.stderr)
                return JsonResponse({"error": f"Unauthorized - Token verification failed: {str(e)}"}, status=401)

            if "error" in token_data:
                err_msg = token_data.get("error", "")
                category = _classify_token_error_message(err_msg)
                token_hash_prefix = hashlib.sha256((raw_token or "").encode()).hexdigest()[:8] if raw_token else ""
                logger.info(
                    "auth_failure category=%s path=%s method=%s hash_prefix=%s error=%s",
                    category,
                    getattr(request, "path", ""),
                    getattr(request, "method", ""),
                    token_hash_prefix,
                    err_msg,
                )
                if err_msg == "Unauthorized - Token has been revoked.":
                    logger.info("Token revoked (single active session or ban), returning 401")
                else:
                    logger.error(f"Token verification returned error: {err_msg}")
                return JsonResponse(token_data, status=401)

            kinde_user_id = token_data.get("user", {}).get("sub")
            kwargs["kinde_user_id"] = kinde_user_id

            # Silent ban check: banned users get 403 so frontend can treat as "access denied" with no new UI
            if await sync_to_async(_is_banned_sync)(kinde_user_id):
                token_hash_prefix = hashlib.sha256((raw_token or "").encode()).hexdigest()[:8] if raw_token else ""
                logger.info(
                    "auth_forbidden category=banned path=%s method=%s sub=%s hash_prefix=%s",
                    getattr(request, "path", ""),
                    getattr(request, "method", ""),
                    kinde_user_id,
                    token_hash_prefix,
                )
                return JsonResponse({"error": "Access denied."}, status=403)

            # Cache current access token so we can revoke it on ban / single active session
            if raw_token:
                cache.set(_access_token_cache_key(kinde_user_id), raw_token, ACCESS_TOKEN_CACHE_TTL)

            return await view_func(request, *args, **kwargs)

        return async_wrapper

    else:
        # --- Sync View ---
        @wraps(view_func)
        def sync_wrapper(request, *args, **kwargs):
            auth_header = request.headers.get("Authorization", "")
            if not auth_header:
                logger.warning(
                    "auth_failure category=no_token path=%s method=%s detail=%s",
                    getattr(request, "path", ""),
                    getattr(request, "method", ""),
                    "Authorization header missing",
                )
                return JsonResponse({"error": "Unauthorized - Authorization header missing"}, status=401)
            
            if "AccessBearer" not in auth_header:
                logger.warning(
                    "auth_failure category=no_token path=%s method=%s detail=%s",
                    getattr(request, "path", ""),
                    getattr(request, "method", ""),
                    "AccessBearer missing in Authorization header",
                )
                return JsonResponse({"error": "Unauthorized - Access token missing. Expected format: 'IDBearer <token>; AccessBearer <token>'"}, status=401)

            try:
                # Verify the ACCESS token (same token we cache and revoke for ban/single session)
                raw_token = _extract_access_token(auth_header)
                if not raw_token:
                    logger.warning(
                        "auth_failure category=no_token path=%s method=%s detail=%s",
                        getattr(request, "path", ""),
                        getattr(request, "method", ""),
                        "Access token empty after extraction",
                    )
                    return JsonResponse({"error": "Unauthorized - Access token is empty"}, status=401)
                logger.debug(f"Extracted token (first 50 chars): {raw_token[:50]}...")
                token_data = verify_kinde_token(raw_token)
            except Exception as e:
                logger.error(f"Token extraction/verification error: {e}")
                traceback.print_exc(file=sys.stderr)
                return JsonResponse({"error": f"Unauthorized - Token verification failed: {str(e)}"}, status=401)

            if "error" in token_data:
                err_msg = token_data.get("error", "")
                category = _classify_token_error_message(err_msg)
                token_hash_prefix = hashlib.sha256((raw_token or "").encode()).hexdigest()[:8] if raw_token else ""
                logger.info(
                    "auth_failure category=%s path=%s method=%s hash_prefix=%s error=%s",
                    category,
                    getattr(request, "path", ""),
                    getattr(request, "method", ""),
                    token_hash_prefix,
                    err_msg,
                )
                if err_msg == "Unauthorized - Token has been revoked.":
                    logger.info("Token revoked (single active session or ban), returning 401")
                else:
                    logger.error(f"Token verification returned error: {err_msg}")
                return JsonResponse(token_data, status=401)

            kinde_user_id = token_data.get("user", {}).get("sub")
            kwargs["kinde_user_id"] = kinde_user_id

            # Silent ban check: banned users get 403 so frontend can treat as "access denied" with no new UI
            if _is_banned_sync(kinde_user_id):
                token_hash_prefix = hashlib.sha256((raw_token or "").encode()).hexdigest()[:8] if raw_token else ""
                logger.info(
                    "auth_forbidden category=banned path=%s method=%s sub=%s hash_prefix=%s",
                    getattr(request, "path", ""),
                    getattr(request, "method", ""),
                    kinde_user_id,
                    token_hash_prefix,
                )
                return JsonResponse({"error": "Access denied."}, status=403)

            # Cache current access token so we can revoke it on ban / single active session
            if raw_token:
                cache.set(_access_token_cache_key(kinde_user_id), raw_token, ACCESS_TOKEN_CACHE_TTL)

            return view_func(request, *args, **kwargs)

        return sync_wrapper


# --- Kinde Management API Functions (M2M) ---

# M2M token cache (tokens typically expire after 1 hour)
_m2m_token_cache = {
    'token': None,
    'expires_at': None,
}

def get_kinde_m2m_token():
    """
    Get M2M (Machine-to-Machine) access token from Kinde OAuth2 token endpoint.
    This token is used for server-to-server API calls to the Kinde Management API.
    Tokens are cached to reduce API calls (they typically expire after 1 hour).
    
    Returns:
        str: M2M access token
        
    Raises:
        Exception: If token request fails
    """
    # Check cache first
    now = datetime.now().timestamp()
    if (_m2m_token_cache['token'] and 
        _m2m_token_cache['expires_at'] and 
        _m2m_token_cache['expires_at'] > now + 300):  # 5 minute buffer before expiry
        logger.debug("Using cached M2M token")
        return _m2m_token_cache['token']
    
    kinde_domain = os.getenv('KINDE_AUTH_DOMAIN') or os.getenv('KINDE_ISSUER_URL') or os.getenv('KINDE_DOMAIN')
    m2m_client_id = os.getenv('KINDE_M2M_CLIENT_ID')
    m2m_client_secret = os.getenv('KINDE_M2M_CLIENT_SECRET')
    audience = os.getenv('KINDE_MANAGEMENT_API_AUDIENCE')
    
    if not all([kinde_domain, m2m_client_id, m2m_client_secret, audience]):
        missing = []
        if not kinde_domain:
            missing.append('KINDE_AUTH_DOMAIN or KINDE_ISSUER_URL or KINDE_DOMAIN')
        if not m2m_client_id:
            missing.append('KINDE_M2M_CLIENT_ID')
        if not m2m_client_secret:
            missing.append('KINDE_M2M_CLIENT_SECRET')
        if not audience:
            missing.append('KINDE_MANAGEMENT_API_AUDIENCE')
        raise Exception(f"M2M credentials not configured. Missing: {', '.join(missing)}")
    
    # Remove trailing slash
    kinde_domain = kinde_domain.rstrip('/')
    token_url = f"{kinde_domain}/oauth2/token"
    
    try:
        response = requests.post(
            token_url,
            data={
                'grant_type': 'client_credentials',
                'client_id': m2m_client_id,
                'client_secret': m2m_client_secret,
                'audience': audience,
                'scope': 'delete:users',  # Required scope for deleting users via Management API
            },
            headers={'Content-Type': 'application/x-www-form-urlencoded'},
            timeout=10
        )
        
        if response.status_code != 200:
            error_msg = response.text
            logger.error(f"Failed to get M2M token: {response.status_code} - {error_msg}")
            raise Exception(f"Failed to get M2M token: {response.status_code} - {error_msg}")
        
        token_data = response.json()
        access_token = token_data.get('access_token')
        expires_in = token_data.get('expires_in', 3600)  # Default to 1 hour if not provided
        
        if not access_token:
            raise Exception("M2M token response missing access_token")
        
        # Cache the token
        _m2m_token_cache['token'] = access_token
        _m2m_token_cache['expires_at'] = now + expires_in
        
        logger.info("Successfully obtained and cached M2M token from Kinde")
        return access_token
        
    except requests.exceptions.RequestException as e:
        logger.error(f"Network error getting M2M token: {e}")
        raise Exception(f"Network error getting M2M token: {e}")
    except Exception as e:
        logger.error(f"Error getting M2M token: {e}")
        raise


def revoke_kinde_token(token, token_type_hint='refresh_token'):
    """
    Revoke a single Kinde access or refresh token via Kinde OAuth2 revoke endpoint.
    Uses the same OAuth app (client_id; client_secret optional for public clients).

    Args:
        token (str): The access_token or refresh_token to revoke.
        token_type_hint (str): 'refresh_token' or 'access_token'.

    Returns:
        bool: True if revocation succeeded (200), False otherwise.
    """
    if not token or not token.strip():
        return False
    kinde_domain = os.getenv('KINDE_AUTH_DOMAIN') or os.getenv('KINDE_ISSUER_URL') or os.getenv('KINDE_DOMAIN')
    client_id = (os.getenv('KINDE_CLIENT_ID') or '').strip()
    client_secret = (os.getenv('KINDE_CLIENT_SECRET') or '').strip()
    if not kinde_domain or not client_id:
        logger.warning(
            "Kinde revoke skipped: set KINDE_CLIENT_ID and KINDE_ISSUER_URL or KINDE_DOMAIN. "
            "KINDE_CLIENT_SECRET is optional for public (no-secret) OAuth apps."
        )
        return False
    kinde_domain = kinde_domain.replace('https://', '').replace('http://', '').rstrip('/')
    revoke_url = f"https://{kinde_domain}/oauth2/revoke"
    payload = {
        'token': token.strip(),
        'client_id': client_id,
        'token_type_hint': token_type_hint,
    }
    if client_secret:
        payload['client_secret'] = client_secret
    try:
        response = requests.post(
            revoke_url,
            data=payload,
            headers={'Content-Type': 'application/x-www-form-urlencoded'},
            timeout=10,
        )
        if response.status_code == 200:
            logger.info("Kinde token revoked successfully")
            return True
        logger.warning(f"Kinde revoke returned {response.status_code}: {response.text}")
        return False
    except requests.exceptions.RequestException as e:
        logger.error(f"Kinde revoke request failed: {e}")
        return False


def revoke_previous_session_if_new_signin(kinde_user_id, student, new_refresh_token, new_access_token=None):
    """
    Single active session: when the same user signs in with *new* tokens (e.g. new device),
    revoke the old refresh token and old access token so the previous device is logged out.
    Call this before storing the new tokens.
    Only revoke the cached access token if it is different from new_access_token (so we revoke
    the old device's token, not the new device's token in case the new device already hit another endpoint).
    """
    if not (kinde_user_id and student and (new_refresh_token or '').strip()):
        logger.debug("Single active session skip: no refresh_token in request body for kinde_user_id=%s", kinde_user_id)
        return
    from .models import KindeRefreshToken
    existing = KindeRefreshToken.objects.filter(student=student).first()
    if not existing or existing.refresh_token == (new_refresh_token or '').strip():
        logger.debug("Single active session skip: no existing refresh or same refresh for student id=%s", student.id)
        return
    # Different session: revoke old refresh token so previous device cannot get new access tokens
    revoke_kinde_token(existing.refresh_token, token_type_hint='refresh_token')
    # Revoke old access token from cache only if it's different from the new one (old device's token)
    old_access = cache.get(_access_token_cache_key(kinde_user_id))
    new_access = (new_access_token or '').strip()
    if old_access:
        old_access = old_access.strip()
        if old_access != new_access:
            revoke_kinde_token(old_access, token_type_hint='access_token')
            invalidate_verification_cache_for_token(old_access)
            old_hash = hashlib.sha256(old_access.encode()).hexdigest()[:32]
            logger.info(
                "Revoked previous Kinde session for student id=%s (single active session), revoked_token_hash_prefix=%s",
                student.id, old_hash[:8],
            )
        else:
            logger.debug("Single active session: cached token same as new (new device may have hit another endpoint first); old device token not in cache")
        cache.delete(_access_token_cache_key(kinde_user_id))
    else:
        logger.debug("Single active session: no cached access token for kinde_user_id=%s (old device may not have made a request)", kinde_user_id)


def revoke_all_tokens_for_student(student):
    """
    Revoke current access token (from cache) and all stored refresh tokens for a student (e.g. on ban).
    Revoking the access token kicks them off immediately; revoking refresh tokens prevents new sessions.
    """
    kinde_user_id = getattr(student, 'kinde_user_id', None)
    if kinde_user_id:
        # Revoke cached access token first so they are kicked off immediately
        access_token = cache.get(_access_token_cache_key(kinde_user_id))
        if access_token:
            revoke_kinde_token(access_token, token_type_hint='access_token')
            invalidate_verification_cache_for_token(access_token)
            cache.delete(_access_token_cache_key(kinde_user_id))
            logger.info(f"Revoked access token for student id={student.id}")
    from .models import KindeRefreshToken
    rows = list(KindeRefreshToken.objects.filter(student=student))
    for row in rows:
        revoke_kinde_token(row.refresh_token, token_type_hint='refresh_token')
        row.delete()
    if rows:
        logger.info(f"Revoked {len(rows)} Kinde refresh token(s) for student id={student.id}")


def delete_kinde_user(kinde_user_id):
    """
    Delete user from Kinde using Management API.
    
    Args:
        kinde_user_id (str): The Kinde user ID (e.g., 'kp_c3143a4b50ad43c88e541d9077681782')
    
    Returns:
        bool: True if successful
        
    Raises:
        Exception: If deletion fails
    """
    if not kinde_user_id:
        raise Exception("kinde_user_id is required")
    
    kinde_domain = os.getenv('KINDE_AUTH_DOMAIN') or os.getenv('KINDE_ISSUER_URL') or os.getenv('KINDE_DOMAIN')
    
    if not kinde_domain:
        raise Exception("KINDE_AUTH_DOMAIN or KINDE_ISSUER_URL or KINDE_DOMAIN not configured")
    
    # Get M2M token
    try:
        m2m_token = get_kinde_m2m_token()
    except Exception as e:
        logger.error(f"Failed to get M2M token for Kinde user deletion: {e}")
        raise
    
    # Remove https:// if present and construct API URL
    clean_domain = kinde_domain.replace('https://', '').replace('http://', '').rstrip('/')
    api_url = f"https://{clean_domain}/api/v1/user"
    
    try:
        response = requests.delete(
            api_url,
            params={
                'id': kinde_user_id,
                'is_delete_profile': True,  # Delete all data and remove from subscriber list
            },
            headers={
                'Authorization': f'Bearer {m2m_token}',
                'Content-Type': 'application/json',
            },
            timeout=10
        )
        
        if response.status_code != 200:
            error_msg = response.text
            logger.error(f"Failed to delete Kinde user {kinde_user_id}: {response.status_code} - {error_msg}")
            raise Exception(f"Failed to delete Kinde user: {response.status_code} - {error_msg}")
        
        logger.info(f"Successfully deleted Kinde user {kinde_user_id}")
        return True
        
    except requests.exceptions.RequestException as e:
        logger.error(f"Network error deleting Kinde user {kinde_user_id}: {e}")
        raise Exception(f"Network error deleting Kinde user: {e}")
    except Exception as e:
        logger.error(f"Error deleting Kinde user {kinde_user_id}: {e}")
        raise