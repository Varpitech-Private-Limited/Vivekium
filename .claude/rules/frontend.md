---
paths:
  - "frontend/**/*.{ts,tsx,css,mjs}"
---
# Frontend (digest; full rules in docs/engineering/frontend.md)

- Ask `can(...)` (`lib/permissions.ts`), never the role. The read-only sentence comes only from `components/permission-notice.tsx`.
- The browser decides nothing about grades, ranking or time. Show server words and sentences verbatim. No numbers beside an assessment.
- No em dash. Recruiter vocabulary: JD, SWOT, Skills, AI Match, Tatva Assessment, PRISM Report, Proctoring Report.
- Navy is structure, teal is evidence; teal text on white is `teal-700`; text is never grey; no two-hue gradient. Dark mode is black (`#000000`) with `#FFFFFF` text and white primary buttons, not a navy canvas. Design authority: root `DESIGN.md`.
- `proxy.ts` must name extensionless generated routes. Do not extend the coding Run or hidden-test UI (S-20).
- Do not hand-edit `components/ui/` primitives.
