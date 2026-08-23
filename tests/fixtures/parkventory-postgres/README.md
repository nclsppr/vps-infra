# Parkventory migration contract fixture

These five SQL files are the reviewed Parkventory V1 to V5 catalog used by the
public-release candidate. The infrastructure test applies them in order to the
exact PostgreSQL 17.10 image from `applications/parkventory/postgres.json`.

This fixture is a consumer-side contract, not Flyway version discovery. A later
application migration does not fail because of its number. It must update this
fixture only when it changes the PostgreSQL catalog or runtime behavior covered
by the infrastructure proof.
