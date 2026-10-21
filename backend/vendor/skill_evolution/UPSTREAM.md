# Skill evolution engines

Vendored from the private repository `zssocrates/skill_improve`, main commit
`61f8870e61c37f59781178f49247f7581aeddba7` (2026-10-21).

- `opt/src`: `skill_improve_opt/src`
- `enhanced/src`: `skill_improve_yzy/skill_improve_direct/src`

Upstream Python sources are retained verbatim. iCAP adapters are in
`app/skill_evolution`. Each job imports only its selected engine in a separate
process. User datasets and credentials from the upstream repository are excluded.
