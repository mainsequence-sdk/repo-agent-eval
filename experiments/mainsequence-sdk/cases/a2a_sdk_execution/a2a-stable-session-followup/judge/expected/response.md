Use discovery first and keep its result as the authority:

```bash
mainsequence agent search "Find an agent that can analyze portfolio risk and return JSON" \
  --environment-uid "env-7b1" \
  --limit 10 \
  --json

mainsequence agent session get_or_create \
  agent-42 \
  --handle-unique-id quarterly-portfolio-review \
  --name "Quarterly portfolio review" \
  --json
```

Take the `uid` returned by `get_or_create` as
`<target_agent_session_uid>`, then send both turns to that same UID:

```bash
mainsequence agent session a2a send \
  <target_agent_session_uid> \
  --message "Return a JSON object with keys summary, risk_level, and next_action." \
  --strict-dictionary

mainsequence agent session a2a send \
  <target_agent_session_uid> \
  --message "Return a JSON object identifying the highest-priority follow-up." \
  --strict-dictionary
```

The stable handle makes session creation/resolution idempotent for the repeated
workflow. Once resolved, the returned `AgentSession.uid` is the conversation
identity and must be reused for later turns; a new handle would create or select
a different conversation. The Environment UID scopes discovery only. Because
the Agent advertises only `message`, neither send requests `task` mode.
