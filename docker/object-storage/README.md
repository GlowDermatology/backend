# Local object storage (development only)

`s3.json` is the SeaweedFS identities file for the optional `object-storage` compose profile.
**Its keys are dummy development values, committed on purpose. Never use them, or this file, in production**
(production keys come from Vault in the infra repository).

Identities: `admin` (creates the bucket), `app` (read/write/list `glowderma-media`; the keys in `.env.example`),
`anonymous` (read-only, which is how unauthenticated media GETs work).
