# Parkventory provider ingress

This runbook covers the one-time Parkventory SMTP and Auth0 credential ingress.
It does not deploy Parkventory, change a public route, send an email, or change
DNS.

## Preconditions

Run the workflow only from `main`. The workflow uses the protected
`application-production` environment and the shared `production-vps`
concurrency group. It refuses the operation unless the environment variable
`VPS_APPLICATION_DEPLOY_ENABLED` equals `false`.

The environment must contain these Parkventory provider secrets:

- `AUTH0_MANAGEMENT_TOKEN` with `read:email_provider`,
  `create:email_provider`, `update:email_provider`, and `read:clients`
  permissions;
- `PARKVENTORY_OIDC_CLIENT_SECRET`;
- `PARKVENTORY_SMTP_USERNAME`;
- `PARKVENTORY_SMTP_PASSWORD`.

Create a temporary SSH key only for provider ingress. Its public key must be
different from every desired and installed deploy key. Supply it as
`vps_parkventory_provider_ingress_authorized_key`. Converge the same `main` SHA
twice and require the second convergence to have no change. The two revision
markers and `/srv/vps/repository` HEAD must equal that SHA. The dedicated key
must refuse `deploy <sha>`. The exact provider command with empty standard
input must fail payload validation without a mutation. Then store the private
key only as `VPS_PARKVENTORY_PROVIDER_INGRESS_SSH_PRIVATE_KEY` in the protected
environment. Do not use an ordinary application deployment private key.

The public provider contract is
`applications/parkventory/provider.env`. It fixes the Auth0 EU tenant, client
identifier, Scaleway TEM relay, sender, JDBC address, and public URL. Review a
change to that file before provider ingress.

## Run the workflow

Dispatch `Materialize Parkventory providers` on `main`. It has no schedule,
push trigger, or input field.

The Atlas operation runs first. It creates one canonical JSON payload with the
three provider values and the public 40-character `INFRA_REVISION`. It sends
that payload only on SSH standard input with the forced command
`materialize-parkventory-providers-v1`. Secret values do not enter the remote
command, a systemd property, a process environment, or the journal.

The worker requires `INFRA_REVISION` to equal the installed controller marker,
the final provider-ingress convergence marker, and the repository HEAD. It
also requires the checked-out `applications/parkventory/provider.env` file to
equal the blob at that HEAD. Auth0 uses the same file from the workflow
checkout. Auth0 configuration starts only after both Atlas checks succeed.

Before an Auth0 mutation, the script retrieves at most three clients with
totals. It requires exactly `Default App`
(`XtyJ6DUNbXNoGnysWWcqY2XfBlq9GakA`) and `Parkventory`
(`BVDpIAxZVZWQhPlziqsQCExYjVeil4YY`). A read-only audit on 2026-08-23 found
that exact set, no Surplasse client, and no email provider. The script repeats
the client check on each run. It creates SMTP only after a provider `404`. It
updates an existing SMTP provider only when its readable host, port, username,
sender, and settings already equal the contract. It refuses SendGrid and every
foreign or different provider. A final `GET` verifies the result. The workflow
never calls the Auth0 test-email endpoint.

The root gate starts one transient root worker. Its systemd runtime directory
has mode `0700`. The unit has memory, task, file-size, and runtime limits. It
has no network access. It can write only the Parkventory secret directory, the
application configuration directory, and the shared lock.

The worker holds `/run/lock/vps-static.lock` until source cleanup completes. It
refuses an active Parkventory Compose tuple, a current Compose link, any static
or application transaction, any application handoff, and the Parkventory
public-edge base transaction. It then runs these operations in order:

1. validate the Parkventory provider source;
2. install Parkventory SMTP generation 1;
3. install Parkventory provider generation 1;
4. check Parkventory SMTP generation 1;
5. check the Parkventory provider bundle.

Both installation helpers verify the pre-held lock by file identity and
metadata. The SMTP helper accepts this mode only for the Parkventory profile.
Other SMTP profiles keep their existing lock path.

## Failure and retry

The workflow prints no response body or provider value. A failure reports only
the operation boundary that refused the request. Inspect Auth0 audit logs and
Atlas metadata separately. Do not add command tracing.

The two generation-1 materializers are idempotent for identical input. Retry
the same manual workflow after the blocking transaction has recovered or the
provider error has been corrected. A different credential value requires a
reviewed target-generation change. Do not delete a marker to force rotation.

After a successful run, perform the read-only marker audit. Update observed
generation and provider state in `secrets/registry.json` only in a later
reviewed commit. Set `vps_parkventory_provider_ingress_authorized_key` back to
an empty string, converge, verify that the entry is absent, and delete the
dedicated private-key secret. Keep `VPS_APPLICATION_DEPLOY_ENABLED=false` until
the separate Parkventory activation gates pass.
