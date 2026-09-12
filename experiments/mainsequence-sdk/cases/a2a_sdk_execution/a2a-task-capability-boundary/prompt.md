# Task

A caller wants to delegate a long analysis asynchronously to an existing
Agent. Discovery returns this profile:

```json
{
  "uid": "agent-risk-9",
  "combined_score": 0.93,
  "a2a_profile": {"supported_response_kinds": ["message"]}
}
```

The caller asks you to use `--response-kind task`, wait for the Task, and
promise durable recovery. Explain whether that plan is valid and provide the
safest bounded alternative. Include the exact response-shape control if the
alternative must return a JSON object with keys `status`, `summary`, and
`next_action`. Do not run anything.
