The route must take `Request` and derive the human caller from platform-injected
request state. A safe sketch is:

```python
from fastapi import Request

@app.put("/preferences/me")
def update_preferences(request: Request, body: Preferences):
    return preferences_service.save(
        user_uid=request.state.user_uid,
        actor=request.state.user,
        body=body,
    )
```

If an administrator endpoint genuinely edits another user's record, its target
UID can remain a path field, but it must not be treated as the caller. The route
must separately authorize `request.state.user_uid` to act on that target.

`request.state.user_uid` is the canonical public UUID of the human making this
HTTP request, and `request.state.user` exposes the canonical user object and
optional username. `request.state.user_id` does not exist. `User.get_logged_user()`
describes SDK/runtime login context, not the per-request FastAPI human, so it is
not an entry point for this route. Identity is also not authorization: the
service still enforces the endpoint's policy. The route should validate
transport input, pass explicit identity to the service, and serialize the
declared response contract.
