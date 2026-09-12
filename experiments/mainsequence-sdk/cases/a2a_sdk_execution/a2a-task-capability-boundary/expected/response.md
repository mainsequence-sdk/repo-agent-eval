The requested Task plan is invalid for this Agent. Its authoritative discovery
profile advertises only `message`; Task support must never be inferred from the
length of the work or from client preference. Therefore the client must not use
`--response-kind task`, must not call a Task waiter, and cannot promise durable
Task recovery.

After resolving a target session, send a bounded message request instead:

```bash
mainsequence agent session a2a send \
  <target_agent_session_uid> \
  --message "Perform only the bounded analysis available in one response. Return a JSON object with keys status, summary, and next_action." \
  --strict-dictionary
```

The response kind remains the default `message`. If the analysis cannot be
bounded to a normal message, stop and report the capability mismatch; select a
different discovered Agent that explicitly advertises `task`, or ask for the
workflow to be changed. A JSON-shaped message is not a Task and should not be
described as durable asynchronous execution.
