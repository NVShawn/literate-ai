# Literate AI agent entry point

Read and follow `SKILL.md` before changing this framework or any project that uses its
canonical taxonomy. Treat that file as the provider-neutral onboarding authority.

Before any worker-backed work, run `litai worker align --all` and follow
`skills/agent/align-workers/SKILL.md`; do not hand-repair workers. For complex
projects where minimum turnaround matters, prefer user-supplied runners over hosted
CI/CD workers: the host running the coding CLI first, then aligned `workers.json`
workers, and CI/CD only for platform and action cells those cannot cover or when
the project marks CI mandatory.
