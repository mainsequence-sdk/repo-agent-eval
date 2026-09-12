# Task

Review this proposed FastAPI route for a Command Center-facing preferences API:

```python
@app.put("/preferences/{user_id}")
def update_preferences(user_id: str, body: Preferences):
    logged_user = User.get_logged_user()
    return preferences_service.save(user_id=user_id, actor=logged_user, body=body)
```

The Main Sequence platform injects the authenticated human into FastAPI request
state. Produce a corrected route sketch and a short review explaining the
caller identity and authorization boundary. Keep the route thin and do not
invent authentication-header parsing or local/deployed branches.
