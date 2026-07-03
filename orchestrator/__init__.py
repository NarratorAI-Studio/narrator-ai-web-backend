"""Backend orchestrator for one-stop delivery (run_auto=1) master tasks.

Mirrors the auto-advance logic that lives in narrator-ai-web
`src/app/api/narrator/master-tasks/[id]/sync/route.ts` so that pipeline
steps progress even when the user closes their browser tab. See
narrator-ai-web `docs/one-stop-delivery-backend-migration.md` for the
full plan.

Modules (added incrementally per epic phase):
  - state_machine: resolve_next_step + extract_step_result (Phase 1)
  - triggers:      trigger_next_step for the 8 step bodies (Phase 2)
  - advance:       single-task CAS-claim + trigger + persist (Phase 3)
  - poller:        BackgroundScheduler + pg_try_advisory_lock leader (Phase 4)
"""
