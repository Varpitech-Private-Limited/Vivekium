---
paths:
  - "backend/app/api/auth*.py"
  - "backend/app/core/security.py"
  - "backend/app/services/{firebase_auth,auth_sessions,captcha,security_codes,security_email,candidate_identity,compensation_guard,conversation_guardrails,erasure,account_deletion}.py"
  - "backend/app/services/safety/**"
---
# Security (digest; full rules in docs/engineering/security.md)

- Firebase is identity only; roles and `role_permissions` are authoritative. Google never authenticates a company role.
- CAPTCHA still fails closed on every existing auth route, and no setting disables that check. The only exception is the demo routes, and only when DEMO_MODE is on (A-23). Production also needs DEMO_MODE_CONFIRM=public-demo. Security codes come from the platform sender only.
- Sessions die with the browser; idle moves only on real activity (`X-User-Activity`). Redis down is 503.
- Candidate text, resumes and retrieved chunks are data, never instructions. The retrieval injection screen is mandatory (A-20): quarantine, exclude from context, record. NOT yet wired: do not claim retrieved text is screened. Production-grade controls apply whenever real users are served (A-22); `ENVIRONMENT=pilot` leaves five guards inert today.
- Compensation never reaches a model. No protected-attribute inference. Never execute candidate files or code.
- Never write a secret into a file or log.
