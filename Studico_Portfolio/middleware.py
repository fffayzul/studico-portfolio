from channels.db import database_sync_to_async
from urllib.parse import parse_qs
from Users.kinde_functions import verify_kinde_token 
import sys # Import the sys module

# REMOVE all top-level model imports here. We'll defer them.
# from Users.models import Student 

# --- Helper function for the database lookup ---
# This is where we will now put the deferred model import.
@database_sync_to_async
def get_student_from_kinde_id_sync(kinde_user_id):
    # This import happens when the function is called, which is after Django is set up.
    from Users.models import Student 
    try:
        return Student.objects.get(kinde_user_id=kinde_user_id)
    except Student.DoesNotExist:
        return None

class KindeAuthMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        # We need to defer this import as well to prevent the crash
        try:
            from django.contrib.auth.models import AnonymousUser # <--- DEFERRED IMPORT
        except Exception as e:
            # --- FORCE THE ERROR TO LOG ---
            # If this import fails, we will print the error and re-raise it.
            print("ERROR: Failed to import AnonymousUser inside middleware's __call__ method.")
            print(f"EXCEPTION: {e}")
            import traceback
            traceback.print_exc(file=sys.stdout)
            # -----------------------------
            await self.close(code=1011) # Internal Error
            return

        # ... rest of your __call__ method ...
        # The logic here is where the problem lies.
        # It's trying to access the user object before authentication is complete.
        
        # Only process WebSocket connections (HTTP requests are handled by Django's middleware)
        if scope['type'] == 'websocket':
            query_string = scope.get('query_string', b'').decode()
            query_params = parse_qs(query_string)
            
            kinde_id_token = query_params.get('kinde_token', [None])[0] 

            if kinde_id_token:
                try:
                    token_data = verify_kinde_token(kinde_id_token)
                    if "error" in token_data:
                        scope['user'] = AnonymousUser()
                    else:
                        kinde_user_id = token_data.get("user", {}).get("sub")
                        if kinde_user_id:
                            student = await get_student_from_kinde_id_sync(kinde_user_id)
                            scope['user'] = student if student else AnonymousUser()
                        else:
                            scope['user'] = AnonymousUser()
                except Exception:
                    scope['user'] = AnonymousUser()
            else:
                scope['user'] = AnonymousUser()
        else:
            pass

        return await self.app(scope, receive, send)