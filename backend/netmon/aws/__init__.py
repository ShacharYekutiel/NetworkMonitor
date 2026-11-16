"""[4] Shared AWS access (PR 4-0a, architecture § 15.1): connections (base identities), one
optional role hop per consumer, the credential cache `rt.aws`, the chain Test with hints, and
the AWS plumbing moved from `netmon.assistant` (SigV4, credentials, transport, errors, certs).
Partition `aws` only; SigV4 is our own code over httpx (no boto3)."""
